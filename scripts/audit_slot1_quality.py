"""Slot-1 quality audit: how high-RS is our slot-1 pick on each slate?

audit_hv_hit_rate.py optimises HV-hit-rate@5 — "did any of our 5 picks
land on the HV leaderboard?" That's a useful signal but it conflates
two different draft outcomes: capturing one mid-RS HV winner (slot-3
material) vs capturing the slate's top-RS HV winner (slot-1 material,
the actual win-condition for Real Sports daily contests).

This audit reports the same env+leverage replay as audit_hv_hit_rate.py
but scored on slot-1 quality: the RS of our highest-EV pick, the
slot-weighted total of the top-5 (assuming zero card_boost), and
whether our slot-1 pick equals the slate's top-RS HV winner.

Limitations (same as audit_hv_hit_rate.py):
    - trait_factor pinned to 1.0 (Statcast kinematics aren't in the
      historical CSV)
    - DEFAULT_BATTING_ORDER=5 (CSV doesn't carry batter lineup slot)
    - single-pool ranking — no per-team caps, no anti-correlation
      guard, no V12 variant chooser
    Live ranking will differ from the audit; trends are directional.

Per CLAUDE.md the runtime never reads outcome columns; this script
lives in /scripts/ where it's permitted to.

Usage:
    BO_CURRENT_SEASON=2026 .venv/bin/python scripts/audit_slot1_quality.py
    # CSV at scripts/output/slot1_quality_per_slate.csv

Sweep popularity calibration:
    BO_OVERRIDE_POPULARITY_SLOPE=0.09 \
    BO_OVERRIDE_POPULARITY_MULT_FLOOR=0.85 \
    .venv/bin/python scripts/audit_slot1_quality.py
"""
from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from datetime import date as DateType
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")

from scripts.audit_hv_hit_rate import (  # noqa: E402
    _maybe_override,
    load_slate_envs,
    neutral_total_score,
    score_one_player,
    slate_stack_eligible_teams,
)

# Allow constant overrides for parameter sweeps (env-driven, no code edits).
for _v in [
    "ENV_MODIFIER_FLOOR",
    "ENV_MODIFIER_CEILING",
    "PITCHER_ENV_MODIFIER_CEILING",
    "ROOKIE_ENV_MODIFIER_CEILING",
    "POPULARITY_NEUTRAL_SCORE",
    "POPULARITY_SLOPE",
    "POPULARITY_MULT_FLOOR",
    "POPULARITY_MULT_CEILING",
    "STACK_BONUS",
]:
    _maybe_override(_v)

SLOT_MULTIPLIERS = (2.0, 1.8, 1.6, 1.4, 1.2)


def main() -> int:
    # Step 6: every reader materialises data/historical.db into a tempdir
    # and rebinds the data file paths to it.  /data/ on-disk files are
    # still produced by writers + backfills as a derived export, but
    # readers consume the canonical store directly.
    import tempfile as _tempfile
    import sys as _sys, pathlib as _pathlib
    _repo = _pathlib.Path(__file__).resolve().parents[1]
    if str(_repo) not in _sys.path:
        _sys.path.insert(0, str(_repo))
    from scripts.export_historical_csvs import export_all as _export_all
    _hist_tmpdir = _tempfile.mkdtemp(prefix="hist_export_")
    _export_all(out_dir=_pathlib.Path(_hist_tmpdir))
    _hist_data_dir = _pathlib.Path(_hist_tmpdir)
    rows_by_date: dict[str, list[dict]] = defaultdict(list)
    with (_hist_data_dir / "historical_players.csv").open() as f:
        for row in csv.DictReader(f):
            rows_by_date[row["date"]].append(row)

    slate_envs = load_slate_envs(_hist_data_dir / "historical_slate_results.json")
    NEUTRAL = neutral_total_score()
    output_dir = ROOT / "scripts" / "output"
    output_dir.mkdir(exist_ok=True)
    output_csv = output_dir / "slot1_quality_per_slate.csv"

    rows_out: list[dict] = []
    for date_str in sorted(rows_by_date):
        if date_str not in slate_envs:
            continue
        env_lookup = slate_envs[date_str]
        eligible = slate_stack_eligible_teams(env_lookup)
        as_of = DateType.fromisoformat(date_str)

        scored: list[dict] = []
        for r in rows_by_date[date_str]:
            rec = score_one_player(r, env_lookup, eligible, as_of, NEUTRAL)
            if rec is None:
                continue
            try:
                rec["real_score"] = float(r["real_score"]) if r["real_score"] else 0.0
            except ValueError:
                rec["real_score"] = 0.0
            scored.append(rec)
        if not scored:
            continue
        scored.sort(key=lambda c: c["filter_ev"], reverse=True)

        # The actual top-RS HV winners of the slate (sorted by RS desc)
        hv_winners = sorted(
            (c for c in scored if c["is_hv"]),
            key=lambda c: c["real_score"],
            reverse=True,
        )
        slate_top_hv_rs = hv_winners[0]["real_score"] if hv_winners else 0.0
        slate_top_hv_name = hv_winners[0]["name"] if hv_winners else ""

        # Optimizer top-5 (no slot reordering — audit is single-pool ranking)
        top5 = scored[:5]
        slot1 = top5[0] if top5 else None
        if slot1:
            slot1_rs = slot1["real_score"]
            slot1_name = slot1["name"]
            # rank slot1's RS among HV winners
            slot1_rs_rank_among_hv = next(
                (i for i, c in enumerate(hv_winners, start=1)
                 if c["name"] == slot1["name"] and c["team"] == slot1["team"]),
                None,
            )
        else:
            slot1_rs = 0.0
            slot1_name = ""
            slot1_rs_rank_among_hv = None

        slot_weighted_rs = sum(
            (top5[i]["real_score"] if i < len(top5) else 0.0) * mult
            for i, mult in enumerate(SLOT_MULTIPLIERS)
        )

        rows_out.append({
            "date": date_str,
            "slot1_pick": slot1_name,
            "slot1_rs": round(slot1_rs, 2),
            "slot1_is_hv": int(slot1["is_hv"]) if slot1 else 0,
            "slot1_rs_rank_among_hv": slot1_rs_rank_among_hv or "—",
            "slate_top_hv_name": slate_top_hv_name,
            "slate_top_hv_rs": round(slate_top_hv_rs, 2),
            "slot_weighted_rs": round(slot_weighted_rs, 2),
        })

    # Write CSV
    fieldnames = list(rows_out[0].keys()) if rows_out else []
    if fieldnames:
        with output_csv.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows_out)

    # Print
    print(f"{'date':<12} {'slot-1 pick':<22} {'RS':>5} HV  {'slate top':<22} {'topRS':>6}  swRS")
    for r in rows_out:
        print(
            f"{r['date']:<12} {r['slot1_pick'][:22]:<22} "
            f"{r['slot1_rs']:>5.1f} {'Y' if r['slot1_is_hv'] else 'n'}   "
            f"{r['slate_top_hv_name'][:22]:<22} {r['slate_top_hv_rs']:>6.1f}  "
            f"{r['slot_weighted_rs']:>6.2f}"
        )

    n = len(rows_out)
    slot1_hv_rate = sum(1 for r in rows_out if r["slot1_is_hv"]) / max(n, 1)
    avg_slot1_rs = sum(r["slot1_rs"] for r in rows_out) / max(n, 1)
    avg_swrs = sum(r["slot_weighted_rs"] for r in rows_out) / max(n, 1)
    avg_slate_top_rs = sum(r["slate_top_hv_rs"] for r in rows_out) / max(n, 1)
    captured_top_hv = sum(
        1 for r in rows_out if r["slot1_pick"] == r["slate_top_hv_name"]
    )
    print()
    print(f"Slates: {n}")
    print(f"Slot-1 HV-hit rate:     {slot1_hv_rate:.1%}")
    print(f"Avg slot-1 RS:          {avg_slot1_rs:.2f}")
    print(f"Avg slot-weighted RS:   {avg_swrs:.2f}")
    print(f"Avg slate top-HV RS:    {avg_slate_top_rs:.2f}")
    print(f"Captured slate top-HV:  {captured_top_hv}/{n} ({captured_top_hv/max(n,1):.1%})")
    print(f"\nPer-slate CSV: {output_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
