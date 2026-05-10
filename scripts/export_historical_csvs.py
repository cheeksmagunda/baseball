"""Export the historical SQLite store to the legacy CSV/JSON file shapes.

After Step 2 of the migration plan, `data/historical.db` is the canonical
store and the five files in `/data/` are derived exports refreshed by every
writer (scraper, backfills) so calibration tooling that still reads CSVs
keeps working during the transition.

Column orders are pinned to the on-disk CSV headers as of Step 1 so an export
into a fresh tree reproduces the existing format byte-for-byte (modulo row
order, which is canonicalised — see `_canonical_sort_*` helpers).

The byte-stable contract:
  - Column order = the explicit list below, matching the current header.
  - Row order = canonical sort: by (date, mlb_id) for player files, by
    (date, winner_rank, slot_index) for winning-drafts, by (slate_date,
    mlb_id, game_date) for game logs.  Envelopes in slate_results.json
    sorted by date; per-envelope games sorted by game_pk.
  - Quoting = csv.QUOTE_MINIMAL, lineterminator="\n", UTF-8, no BOM.
  - JSON = `json.dumps(..., indent=2, sort_keys=False)` to match
    scrape_realsports_daily.py's existing JSON writer.

API:
    from scripts.export_historical_csvs import export_all
    export_all(out_dir=Path("data"), db_path=None)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "export-historical-csvs-stub")

from app.core import historical_db  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("export_historical_csvs")


# ---------------------------------------------------------------------------
# Canonical column orders — matches the on-disk CSV headers as of Step 1.
# When a new column lands (e.g. via a Step-3 backfill), append it to the END
# of the relevant tuple so existing readers don't shift.
# ---------------------------------------------------------------------------
HISTORICAL_PLAYERS_COLS = (
    "date", "player_name", "team", "position",
    "real_score",
    "is_highest_value", "is_most_popular", "is_most_drafted_3x",
    "ops_at_slate", "iso_at_slate",
    "era_at_slate", "whip_at_slate", "k9_at_slate",
    "x_woba", "x_ba", "x_slg",
    "avg_ev", "hard_hit_pct", "barrel_pct", "max_ev",
    "x_era", "x_woba_against",
    "fb_velo", "whiff_pct", "chase_pct",
    "fb_ivb", "fb_extension",
    "ops_vs_lhp_at_slate", "ops_vs_rhp_at_slate",
    "batting_order_at_slate",
    "card_boost", "drafts",
    "draft_count",
    "injury_status",
)

WINNING_DRAFTS_COLS = (
    "date", "winner_rank", "slot_index",
    "player_name", "team", "position",
    "real_score", "slot_mult", "card_boost", "total_mult",
)

HV_STATS_COLS = (
    "date", "player_name", "team_actual", "position",
    "real_score", "game_result",
    "ab", "r", "h", "hr", "rbi", "bb", "so",
    "ip", "er", "k_pitching", "decision",
    "notes",
    "ops_at_slate", "iso_at_slate",
)

PLAYER_GAME_LOGS_COLS = (
    "slate_date", "player_name", "team", "mlb_id", "position",
    "game_date", "opponent", "is_home",
    "ab", "runs", "hits", "hr", "rbi", "bb", "so", "sb",
    "ip", "er", "k_pitching", "decision",
)


# ---------------------------------------------------------------------------
# Numeric formatting helpers — preserve the rounding conventions of the
# original parsers (scrape_realsports_daily.py + per-backfill f-strings).
# Values stored as REAL in SQLite must round-trip to the same string
# representation the CSV had.
# ---------------------------------------------------------------------------
def _fmt_int(v) -> str:
    if v is None:
        return ""
    return str(int(v))


def _fmt_real(v, dp: int | None = None) -> str:
    """Format a REAL value with explicit decimal places.

    `dp=None` means "Python's default str(float)" which strips trailing zeros
    (e.g. 0.0 → "0.0", 1.4 → "1.4").  This matches scrape_realsports_daily.py's
    csv.DictWriter behavior for fields it produces (real_score is `:.1f` via
    _round_dp, total_value via _round_dp at 2dp + str()).

    For backfilled fields the per-column dp matches the f-string the backfill
    script used:
      - ops_at_slate, iso_at_slate, x_woba, x_ba, x_slg, x_woba_against,
        ops_vs_lhp_at_slate, ops_vs_rhp_at_slate: 3dp
      - era_at_slate, whip_at_slate, k9_at_slate, x_era: 2dp
      - avg_ev, hard_hit_pct, barrel_pct, max_ev, fb_velo, whiff_pct,
        chase_pct, fb_ivb, fb_extension: 1dp
      - avg_draft_slot: 3dp
      - avg_draft_mult, avg_draft_tv, highest_draft_tv: 4dp
    """
    if v is None:
        return ""
    if dp is not None:
        return f"{round(float(v), dp):.{dp}f}"
    # Default: Python's str(float) — strips trailing zeros
    f = float(v)
    return str(f)


# Per-column formatting style for export.  "fixed" applies f"{v:.<dp>f}";
# "round_str" applies round(v, dp) then Python's str() (strips trailing zeros).
# The choice mirrors which writer populated the column originally:
#   - backfill scripts that use `f"{v:.Nf}"` → fixed
#   - scrape_realsports_daily.py's _safe_round() → round_str
PLAYER_SLATE_FMT = {
    # backfill_player_season_stats_at_slate.py uses explicit `f"{x:.Nf}"` —
    # fixed-precision, trailing zeros preserved.
    "ops_at_slate": ("fixed", 3), "iso_at_slate": ("fixed", 3),
    "era_at_slate": ("fixed", 2), "whip_at_slate": ("fixed", 2),
    "k9_at_slate": ("fixed", 2),
    # backfill_player_platoon_splits.py: f"{x:.3f}" → fixed.
    "ops_vs_lhp_at_slate": ("fixed", 3), "ops_vs_rhp_at_slate": ("fixed", 3),
    # backfill_statcast_at_slate.py: every Statcast column is written via
    # `round(float(x), N)` then csv.DictWriter applies str() → trailing zeros
    # are stripped (round_str mode).
    "x_woba": ("round_str", 3), "x_ba": ("round_str", 3),
    "x_slg": ("round_str", 3), "x_woba_against": ("round_str", 3),
    "x_era": ("round_str", 2),
    "avg_ev": ("round_str", 1), "max_ev": ("round_str", 1),
    "hard_hit_pct": ("round_str", 1), "barrel_pct": ("round_str", 1),
    "fb_velo": ("round_str", 1),
    "whiff_pct": ("round_str", 1), "chase_pct": ("round_str", 1),
    "fb_ivb": ("round_str", 2), "fb_extension": ("round_str", 2),
}

LABEL_FMT = {
    # scraper _round_dp(value, 1) → float → str() → "0.7"
    "real_score": ("round_str", 1),
    # scraper _round_dp(value, 1) → "0.0" / "1.5"
    "card_boost": ("round_str", 1),
}


def _fmt_with_style(value, style: tuple[str, int] | None) -> str:
    if value is None:
        return ""
    if style is None:
        return _fmt_real(value)
    mode, dp = style
    if mode == "fixed":
        return f"{round(float(value), dp):.{dp}f}"
    if mode == "round_str":
        return str(round(float(value), dp))
    raise ValueError(f"unknown format style {style!r}")


def _fmt_player_slate_col(col: str, value) -> str:
    return _fmt_with_style(value, PLAYER_SLATE_FMT.get(col))


def _fmt_label_col(col: str, value) -> str:
    return _fmt_with_style(value, LABEL_FMT.get(col))


def _fmt_str(v) -> str:
    if v is None:
        return ""
    return str(v)


def _write_csv(path: Path, columns: list[str] | tuple[str, ...], rows: list[dict]) -> None:
    """Write a CSV with explicit column order and the byte-stable formatting
    convention.  rows are dicts keyed by column name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=list(columns),
            extrasaction="ignore",
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


# ---------------------------------------------------------------------------
# Per-file exporters
# ---------------------------------------------------------------------------
def _label_index(conn, label_type: str) -> dict[tuple[str, int], tuple[float | None, str | None]]:
    """Build {(slate_date, mlb_id) -> (label_value, label_text)} for one
    label_type.  Used by the players export to attach scalar / boolean labels
    back onto each player_slate row."""
    cur = conn.execute(
        "SELECT slate_date, mlb_id, label_value, label_text "
        "FROM label_event WHERE label_type = ?",
        (label_type,),
    )
    out: dict[tuple[str, int], tuple[float | None, str | None]] = {}
    for r in cur.fetchall():
        out[(r["slate_date"], r["mlb_id"])] = (r["label_value"], r["label_text"])
    return out


def export_historical_players(conn, path: Path) -> int:
    """Reconstruct historical_players.csv from player_slate + label_event.

    For each (slate_date, mlb_id) row in player_slate we attach the matching
    real_score / 3 boolean flags / card_boost / drafts / draft_count +
    injury_status from label_event.  Flag rows present in label_event become
    "1"; absent rows become "0" (or empty for non-flag fields).

    total_value, avg_draft_slot, avg_draft_mult, avg_draft_tv, highest_draft_tv,
    most_common_slot used to be exported but were all derivations of fields
    we already keep (real_score × (2 + card_boost) for total_value; reductions
    over winning_lineup_slot rows for the rest).  Dropped here to shrink the
    CSV; recompute on the fly if a downstream consumer needs them.
    """
    rs_idx = _label_index(conn, "real_score")
    hv_idx = _label_index(conn, "highest_value")
    mp_idx = _label_index(conn, "most_popular")
    md_idx = _label_index(conn, "most_drafted_3x")
    cb_idx = _label_index(conn, "card_boost")
    dr_idx = _label_index(conn, "drafts")
    dc_idx = _label_index(conn, "draft_count")
    in_idx = _label_index(conn, "injury_status")

    # Canonical sort: (date, player_name).  Matches the natural alphabetical
    # ordering visible in the existing on-disk historical_players.csv.
    cur = conn.execute(
        "SELECT * FROM player_slate ORDER BY slate_date, player_name"
    )
    rows: list[dict] = []
    for r in cur.fetchall():
        key = (r["slate_date"], r["mlb_id"])
        rs = rs_idx.get(key, (None, None))[0]
        cb = cb_idx.get(key, (None, None))[0]
        dr = dr_idx.get(key, (None, None))[0]
        dc = dc_idx.get(key, (None, None))[0]
        inj = in_idx.get(key, (None, None))[1]

        out = {
            "date": r["slate_date"],
            "player_name": r["player_name"],
            "team": r["team"],
            "position": r["position"],
            "real_score": _fmt_label_col("real_score", rs),
            "is_highest_value": "1" if (key in hv_idx) else "0",
            "is_most_popular": "1" if (key in mp_idx) else "0",
            "is_most_drafted_3x": "1" if (key in md_idx) else "0",
            "batting_order_at_slate": _fmt_int(r["batting_order_at_slate"]),
            "card_boost": _fmt_label_col("card_boost", cb),
            "drafts": _fmt_int(dr),
            "draft_count": _fmt_int(dc),
            "injury_status": inj or "",
        }
        for col in PLAYER_SLATE_FMT:
            out[col] = _fmt_player_slate_col(col, r[col])
        rows.append(out)

    _write_csv(path, HISTORICAL_PLAYERS_COLS, rows)
    return len(rows)


def _fmt_natural(value_str: str) -> str:
    """Reproduce the platform-native formatting for `card_boost` / `total_mult`:
    whole numbers without trailing ".0", fractions with their natural repr.
    The original CSV values came from the API verbatim — int 0 → "0", float
    0.5 → "0.5".  We round-trip through float and re-emit to match."""
    if value_str == "" or value_str is None:
        return ""
    try:
        f = float(value_str)
    except ValueError:
        return value_str
    if f.is_integer():
        return str(int(f))
    return str(f)


def export_winning_drafts(conn, path: Path) -> int:
    """Reconstruct historical_winning_drafts.csv from
    label_event WHERE label_type='winning_lineup_slot'.
    """
    cur = conn.execute(
        "SELECT le.slate_date, le.mlb_id, le.label_value, le.label_text, le.source, "
        "       ps.player_name, ps.team, ps.position "
        "FROM label_event le "
        "LEFT JOIN player_slate ps "
        "  ON ps.slate_date = le.slate_date AND ps.mlb_id = le.mlb_id "
        "WHERE le.label_type = 'winning_lineup_slot' "
        # Sort by source string puts row=N|rank=R|slot=S in lexicographic
        # order, which preserves the CSV's original line ordering for
        # byte-identical reproduction (including exact-duplicate rows).
        "ORDER BY le.slate_date, le.source"
    )
    rows: list[dict] = []
    for r in cur.fetchall():
        text = r["label_text"] or "{}"
        try:
            parts = json.loads(text)
        except json.JSONDecodeError:
            parts = {}
        rs = r["label_value"]
        rank = int(parts.get("rank", 0))
        slot_index = int(parts.get("slot", 0))
        slot_mult = float(parts.get("slot_mult", 0.0))
        cb = parts.get("card_boost", "")
        tm = parts.get("total_mult", "")
        # Identity columns: prefer the values captured at ingest time (in
        # label_text) over the player_slate join — winning_drafts captures
        # don't always agree with the day's player_slate row, and we want
        # byte-identical reproduction.
        name = parts.get("name") or r["player_name"] or ""
        team = parts.get("team") or r["team"] or ""
        position = parts.get("position") or r["position"] or ""
        rows.append({
            "date": r["slate_date"],
            "winner_rank": rank,
            "slot_index": slot_index,
            "player_name": name,
            "team": team,
            "position": position,
            "real_score": _fmt_label_col("real_score", rs),
            # slot_mult is always written as a float by scrape_realsports_daily
            # (e.g. "2.0" / "1.8" / "1.6" / "1.4" / "1.2"); keep that shape.
            "slot_mult": f"{slot_mult:.1f}",
            # card_boost / total_mult come from the API's raw value — int 0
            # writes "0", float 1.5 writes "1.5".  _fmt_natural reproduces this.
            "card_boost": _fmt_natural(cb),
            "total_mult": _fmt_natural(tm),
        })
    rows.sort(key=lambda r: (r["date"], r["winner_rank"], r["slot_index"]))
    _write_csv(path, WINNING_DRAFTS_COLS, rows)
    return len(rows)


def export_player_game_logs(conn, path: Path) -> int:
    # Preserve the original CSV row order via insertion sequence (rowid_seq).
    # This is the order rows landed during the initial CSV ingest, which is
    # the order calibration scripts saw under the CSV-era harness.
    cur = conn.execute(
        "SELECT * FROM player_game_log ORDER BY rowid_seq"
    )
    rows: list[dict] = []
    for r in cur.fetchall():
        rows.append({
            "slate_date": r["slate_date"],
            "player_name": r["player_name"] or "",
            "team": r["team"] or "",
            "mlb_id": _fmt_int(r["mlb_id"]),
            "position": r["position"] or "",
            "game_date": r["game_date"],
            "opponent": r["opponent"] or "",
            "is_home": _fmt_int(r["is_home"]) if r["is_home"] is not None else "",
            "ab": _fmt_int(r["ab"]) if r["ab"] is not None else "",
            "runs": _fmt_int(r["runs"]) if r["runs"] is not None else "",
            "hits": _fmt_int(r["hits"]) if r["hits"] is not None else "",
            "hr": _fmt_int(r["hr"]) if r["hr"] is not None else "",
            "rbi": _fmt_int(r["rbi"]) if r["rbi"] is not None else "",
            "bb": _fmt_int(r["bb"]) if r["bb"] is not None else "",
            "so": _fmt_int(r["so"]) if r["so"] is not None else "",
            "sb": _fmt_int(r["sb"]) if r["sb"] is not None else "",
            "ip": _fmt_real(r["ip"]) if r["ip"] is not None else "",
            "er": _fmt_int(r["er"]) if r["er"] is not None else "",
            "k_pitching": _fmt_int(r["k_pitching"]) if r["k_pitching"] is not None else "",
            "decision": r["decision"] or "",
        })
    _write_csv(path, PLAYER_GAME_LOGS_COLS, rows)
    return len(rows)


def export_hv_player_stats(conn, path: Path) -> int:
    """Reconstruct hv_player_game_stats.csv from label_event(box_score) +
    player_slate (for identity columns)."""
    cur = conn.execute(
        "SELECT le.slate_date, le.mlb_id, le.label_value, le.label_text, "
        "       ps.player_name, ps.team, ps.position, "
        "       ps.ops_at_slate, ps.iso_at_slate "
        "FROM label_event le "
        "LEFT JOIN player_slate ps "
        "  ON ps.slate_date = le.slate_date AND ps.mlb_id = le.mlb_id "
        "WHERE le.label_type = 'box_score' "
        "ORDER BY le.slate_date, ps.player_name"
    )
    rows: list[dict] = []
    for r in cur.fetchall():
        try:
            payload = json.loads(r["label_text"]) if r["label_text"] else {}
        except json.JSONDecodeError:
            payload = {}
        rs = r["label_value"]
        rows.append({
            "date": r["slate_date"],
            "player_name": r["player_name"] or "",
            "team_actual": r["team"] or "",
            "position": r["position"] or "",
            "real_score": _fmt_real(rs, dp=1) if rs is not None else "",
            "game_result": payload.get("game_result", ""),
            "ab": payload.get("ab", ""),
            "r": payload.get("r", ""),
            "h": payload.get("h", ""),
            "hr": payload.get("hr", ""),
            "rbi": payload.get("rbi", ""),
            "bb": payload.get("bb", ""),
            "so": payload.get("so", ""),
            "ip": payload.get("ip", ""),
            "er": payload.get("er", ""),
            "k_pitching": payload.get("k_pitching", ""),
            "decision": payload.get("decision", ""),
            "notes": payload.get("notes", ""),
            "ops_at_slate": _fmt_player_slate_col("ops_at_slate", r["ops_at_slate"]),
            "iso_at_slate": _fmt_player_slate_col("iso_at_slate", r["iso_at_slate"]),
        })
    _write_csv(path, HV_STATS_COLS, rows)
    return len(rows)


def export_slate_results(conn, path: Path) -> int:
    """Reconstruct historical_slate_results.json from `slate` + `slate_game`.

    Column rename on export: slate_game uses `home_team` / `away_team` for
    schema clarity; the JSON envelope uses `home` / `away` (matching what
    scrape_realsports_daily.py wrote and what audit scripts read).

    On-the-fly derivations (not stored in the DB):
      - winner / loser / winner_score / loser_score  ←  home_team / away_team
        / home_score / away_score
    """
    envelopes: list[dict] = []
    cur_slates = conn.execute(
        "SELECT * FROM slate ORDER BY slate_date"
    )
    for s in cur_slates.fetchall():
        slate_date = s["slate_date"]
        # game_number is internal disambiguator for doubleheaders sharing a
        # single game_pk; keep doubleheader rows in their original order via
        # `ORDER BY game_pk, game_number` then drop game_number from output.
        games_cur = conn.execute(
            "SELECT * FROM slate_game WHERE slate_date = ? "
            "ORDER BY game_pk, game_number",
            (slate_date,),
        )
        games_out = []
        for g in games_cur.fetchall():
            row: dict = {}
            for k in g.keys():
                if k in ("slate_date", "game_number"):
                    continue
                if k == "home_team":
                    row["home"] = g[k]
                elif k == "away_team":
                    row["away"] = g[k]
                else:
                    row[k] = g[k]
            # Derive winner/loser/winner_score/loser_score on the fly so the
            # JSON shape matches what scrape_realsports_daily.py historically
            # wrote — even though the DB no longer stores these as columns.
            home_team = row.get("home")
            away_team = row.get("away")
            hs = row.get("home_score")
            as_ = row.get("away_score")
            if hs is not None and as_ is not None and home_team and away_team:
                if hs > as_:
                    row["winner"], row["loser"] = home_team, away_team
                    row["winner_score"], row["loser_score"] = hs, as_
                elif as_ > hs:
                    row["winner"], row["loser"] = away_team, home_team
                    row["winner_score"], row["loser_score"] = as_, hs
                else:  # tie — vanishingly rare; mirror the legacy null-out.
                    row["winner"] = row["loser"] = None
                    row["winner_score"] = row["loser_score"] = hs
            else:
                row["winner"] = row["loser"] = None
                row["winner_score"] = row["loser_score"] = None
            games_out.append(row)
        env = {
            "date": slate_date,
            "game_count": s["game_count"],
            "games": games_out,
            "num_brawlers": s["num_brawlers"],
            "season_stage": s["season_stage"] or "regular-season",
            "source": s["source"] or "",
            "saved_at": s["saved_at"] or "",
        }
        if s["notes"]:
            env["notes"] = s["notes"]
        envelopes.append(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelopes, indent=2))
    return len(envelopes)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def export_all(out_dir: Path | None = None, db_path: str | None = None) -> dict[str, int]:
    """Run every exporter.  Returns {filename: row count} for verification."""
    if out_dir is None:
        out_dir = REPO_ROOT / "data"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = historical_db.connect_readonly(db_path)
    try:
        results = {
            "historical_players.csv": export_historical_players(
                conn, out_dir / "historical_players.csv"),
            "historical_winning_drafts.csv": export_winning_drafts(
                conn, out_dir / "historical_winning_drafts.csv"),
            "historical_player_game_logs.csv": export_player_game_logs(
                conn, out_dir / "historical_player_game_logs.csv"),
            "hv_player_game_stats.csv": export_hv_player_stats(
                conn, out_dir / "hv_player_game_stats.csv"),
            "historical_slate_results.json": export_slate_results(
                conn, out_dir / "historical_slate_results.json"),
        }
    finally:
        conn.close()
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="DB path (default from historical_db)")
    ap.add_argument("--out-dir", default=None,
                    help="Output directory (default: data/)")
    args = ap.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else (REPO_ROOT / "data")
    results = export_all(out_dir=out_dir, db_path=args.db)
    for name, n in results.items():
        log.info("exported %s: %d rows/envelopes", name, n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
