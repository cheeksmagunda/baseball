"""Calibrate env scoring thresholds from historical condition + outcome data.

Joins historical_players.csv (outcomes) with historical_conditions.csv (env inputs)
on (date, team), then shows how RS and HV rate distribute across each env factor's
current threshold range.

Run after accumulating new slates:

    python scripts/calibrate_env_scoring.py

Output is printed — no code is modified. Claude reads the results and edits
app/core/constants.py directly to adjust thresholds, add, or remove factors.
"""

import csv
import statistics
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PLAYERS_CSV = DATA_DIR / "historical_players.csv"
CONDITIONS_CSV = DATA_DIR / "historical_conditions.csv"

RS_SUCCESS_THRESHOLD = 3.0
MIN_N_FOR_SUGGESTION = 20  # don't suggest changes on sparse buckets


# ── Current thresholds (mirrors app/core/constants.py) ─────────────────────

BATTER_FACTORS = [
    ("Vegas O/U",          "vegas_total",      7.0,  9.5,  True,  "BATTER_ENV_VEGAS_FLOOR / CEILING"),
    ("Opp Starter ERA",    "opp_starter_era",  3.5,  5.5,  True,  "BATTER_ENV_ERA_FLOOR / CEILING"),
    ("Opp Bullpen ERA",    "opp_bullpen_era",  3.5,  5.5,  True,  "BATTER_ENV_BULLPEN_ERA_FLOOR / CEILING"),
]

PITCHER_FACTORS = [
    ("Opp Team OPS",       "opp_team_ops",     0.650, 0.780, False, "PITCHER_ENV_OPS_FLOOR / CEILING"),
    ("Opp Team K%",        "opp_team_k_pct",   0.20,  0.26,  True,  "PITCHER_ENV_K_PCT_FLOOR / CEILING"),
    ("Pitcher K/9",        "pitcher_k9",       6.0,   10.0,  True,  "PITCHER_ENV_K9_FLOOR / CEILING"),
]

# Ascending=True: higher value is better (ERA, O/U). False: lower is better (OPS).


def _load_conditions() -> dict[tuple[str, str], dict]:
    """Returns {(date, team): row} for both home and away perspective."""
    if not CONDITIONS_CSV.exists() or CONDITIONS_CSV.stat().st_size < 50:
        return {}

    index: dict[tuple[str, str], dict] = {}
    with CONDITIONS_CSV.open() as f:
        for row in csv.DictReader(f):
            d = row["date"]
            # Build perspective for each team: facing their opponent's starter/bullpen
            home = row["home_team"]
            away = row["away_team"]

            index[(d, home)] = {
                "vegas_total":    _f(row["vegas_total"]),
                "opp_starter_era": _f(row["away_starter_era"]),   # home faces away starter
                "pitcher_k9":     _f(row["home_starter_k9"]),      # home pitcher's own K/9
                "opp_team_ops":   _f(row["away_team_ops"]),
                "opp_team_k_pct": _f(row["away_team_k_pct"]),
                "opp_bullpen_era": _f(row["away_bullpen_era"]),
            }
            index[(d, away)] = {
                "vegas_total":    _f(row["vegas_total"]),
                "opp_starter_era": _f(row["home_starter_era"]),   # away faces home starter
                "pitcher_k9":     _f(row["away_starter_k9"]),
                "opp_team_ops":   _f(row["home_team_ops"]),
                "opp_team_k_pct": _f(row["home_team_k_pct"]),
                "opp_bullpen_era": _f(row["home_bullpen_era"]),
            }
    return index


def _f(val: str) -> float | None:
    if not val or val.strip() == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _load_players() -> list[dict]:
    with PLAYERS_CSV.open() as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r["real_score"].strip() not in ("", "None")]


def _bucket(value: float, floor: float, ceiling: float, ascending: bool) -> str:
    """Return bucket label: below-floor, floor-ceiling, or above-ceiling."""
    if ascending:
        if value <= floor:
            return f"< {floor}"
        if value >= ceiling:
            return f"> {ceiling}"
        return f"{floor}–{ceiling}"
    else:
        # OPS: lower is better, so floor/ceiling are swapped in meaning
        if value >= ceiling:
            return f"> {ceiling} (bad)"
        if value <= floor:
            return f"< {floor} (good)"
        return f"{floor}–{ceiling}"


def _analyze_factor(
    label: str,
    field: str,
    floor: float,
    ceiling: float,
    ascending: bool,
    constant_name: str,
    player_rows: list[dict],
    conditions: dict,
    is_pitcher: bool,
) -> None:
    buckets: dict[str, list[float]] = {}
    hv_buckets: dict[str, list[int]] = {}

    for row in player_rows:
        pos = row["position"].strip().upper()
        player_is_pitcher = pos in ("P", "SP", "RP")
        if player_is_pitcher != is_pitcher:
            continue

        key = (row["date"], row["team"])
        cond = conditions.get(key)
        if not cond:
            continue

        val = cond.get(field)
        if val is None:
            continue

        rs = float(row["real_score"])
        hv = 1 if row["is_highest_value"] == "1" else 0
        bkt = _bucket(val, floor, ceiling, ascending)

        buckets.setdefault(bkt, []).append(rs)
        hv_buckets.setdefault(bkt, []).append(hv)

    if not buckets:
        print(f"  {label}: no linked data")
        return

    print(f"  {label}  [{constant_name}]")
    print(f"  Current thresholds: floor={floor}  ceiling={ceiling}")

    # Sort buckets logically
    order = [f"< {floor}", f"{floor}–{ceiling}", f"> {ceiling}"] if ascending else \
            [f"< {floor} (good)", f"{floor}–{ceiling}", f"> {ceiling} (bad)"]

    rows_out = []
    for bkt in order:
        rs_vals = buckets.get(bkt, [])
        hv_vals = hv_buckets.get(bkt, [])
        if not rs_vals:
            rows_out.append((bkt, 0, None, None, None))
            continue
        avg_rs = statistics.mean(rs_vals)
        hv_rate = sum(hv_vals) / len(hv_vals)
        rs3_rate = sum(1 for v in rs_vals if v > RS_SUCCESS_THRESHOLD) / len(rs_vals)
        rows_out.append((bkt, len(rs_vals), avg_rs, hv_rate, rs3_rate))

    for bkt, n, avg_rs, hv_rate, rs3_rate in rows_out:
        if n == 0:
            print(f"    {bkt:<18}  n=0")
        else:
            print(
                f"    {bkt:<18}  n={n:<4}  avg_rs={avg_rs:>5.2f}  "
                f"hv={hv_rate:>5.1%}  rs>3={rs3_rate:>5.1%}"
            )

    # Suggestion: check if the middle bucket looks meaningfully different from both ends
    mid_bkt = f"{floor}–{ceiling}"
    mid_rs = buckets.get(mid_bkt, [])
    low_key = f"< {floor}" if ascending else f"< {floor} (good)"
    high_key = f"> {ceiling}" if ascending else f"> {ceiling} (bad)"
    low_rs = buckets.get(low_key, [])
    high_rs = buckets.get(high_key, [])

    all_n = sum(r[1] for r in rows_out)
    if all_n < MIN_N_FOR_SUGGESTION:
        print(f"    → Insufficient data (n={all_n} < {MIN_N_FOR_SUGGESTION}) — no suggestion")
    else:
        print(f"    → {all_n} total linked player-games. Adjust thresholds if mid-bucket")
        print(f"       avg_rs doesn't fall between the two extremes, or extremes overlap.")
    print()


def main() -> None:
    conditions = _load_conditions()
    if not conditions:
        print("No data in historical_conditions.csv yet.")
        print("Run scripts/export_slate_conditions.py after each slate to populate it.")
        return

    players = _load_players()
    linked = sum(1 for r in players if (r["date"], r["team"]) in conditions)
    print(f"Dataset: {len(players)} player-appearances, {linked} linked to conditions")
    print(f"Games in conditions: {len(conditions) // 2} unique games\n")

    print("=" * 70)
    print("BATTER ENV FACTORS")
    print("=" * 70)
    for args in BATTER_FACTORS:
        _analyze_factor(*args, players, conditions, is_pitcher=False)

    print("=" * 70)
    print("PITCHER ENV FACTORS")
    print("=" * 70)
    for args in PITCHER_FACTORS:
        _analyze_factor(*args, players, conditions, is_pitcher=True)

    print("─" * 70)
    print("After reviewing, edit app/core/constants.py to adjust thresholds.")
    print("No changes are made automatically.")


if __name__ == "__main__":
    main()
