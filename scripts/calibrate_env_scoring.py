"""Calibrate env scoring thresholds from historical condition + outcome data.

The live pipeline scores players using pre-game conditions (Vegas lines, ERA, weather,
etc.) to compute env_factor. This script validates whether those conditions actually
correlate with real outcomes by joining two datasets on (date, team):

  - CONDITIONS  historical_conditions.csv — the game context signals that existed
                before each slate. These mirror exactly what the T-65 pipeline reads
                from live APIs (Vegas, bullpen ERA, series context, weather, etc.).
                NOTE: planned to move these fields into historical_slate_results.json
                game objects; the join logic here will update accordingly.

  - OUTCOMES    historical_players.csv — real_score and HV/MP/3X flags per player
                per slate. These are retrospective outcome labels; they are never
                used as pipeline inputs. Here they serve as ground truth to measure
                whether condition-based scoring predicted performance correctly.

Output shows RS and HV-rate distributions across each threshold bucket (below floor /
mid / above ceiling). No code is modified. Read the results and edit
app/core/constants.py directly to adjust thresholds, add, or remove factors.

Run after accumulating new slates:

    python scripts/calibrate_env_scoring.py
"""

import csv
import statistics
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PLAYERS_CSV = DATA_DIR / "historical_players.csv"
CONDITIONS_CSV = DATA_DIR / "historical_conditions.csv"

RS_SUCCESS_THRESHOLD = 3.0
MIN_N_FOR_SUGGESTION = 20


# ── Current thresholds (mirrors app/core/constants.py) ─────────────────────
# Graduated factors: (label, field, floor, ceiling, ascending, constant_name)
# ascending=True: higher value is better. False: lower is better.

BATTER_GRADUATED = [
    ("Vegas O/U",       "vegas_total",      7.0,   9.5,   True,  "BATTER_ENV_VEGAS_FLOOR / CEILING"),
    ("Opp Starter ERA", "opp_starter_era",  3.5,   5.5,   True,  "BATTER_ENV_ERA_FLOOR / CEILING"),
    ("Opp Bullpen ERA", "opp_bullpen_era",  3.5,   5.5,   True,  "BATTER_ENV_BULLPEN_ERA_FLOOR / CEILING"),
]

PITCHER_GRADUATED = [
    ("Opp Team OPS",    "opp_team_ops",     0.650, 0.780, False, "PITCHER_ENV_OPS_FLOOR / CEILING"),
    ("Opp Team K%",     "opp_team_k_pct",   0.20,  0.26,  True,  "PITCHER_ENV_K_PCT_FLOOR / CEILING"),
    ("Pitcher K/9",     "pitcher_k9",       6.0,   10.0,  True,  "PITCHER_ENV_K9_FLOOR / CEILING"),
]

# Moneyline thresholds (shared batter + pitcher)
ML_BUCKETS = [
    ("underdog/even (> -110)",  lambda ml: ml > -110),
    ("-110 to -150",            lambda ml: -150 <= ml <= -110),
    ("-150 to -200",            lambda ml: -200 <= ml < -150),
    ("-200 to -250",            lambda ml: -250 <= ml < -200),
    ("heavy fav (< -250)",      lambda ml: ml < -250),
]

# Series context buckets (from SERIES_LEADING_BONUS, SERIES_TRAILING_PENALTY)
SERIES_BUCKETS = [
    ("trailing 2+",  lambda tw, ow: ow - tw >= 2),
    ("trailing 1",   lambda tw, ow: ow - tw == 1),
    ("tied",         lambda tw, ow: tw == ow),
    ("leading 1",    lambda tw, ow: tw - ow == 1),
    ("leading 2+",   lambda tw, ow: tw - ow >= 2),
]

# L10 wins buckets (from TEAM_HOT/COLD_L10_THRESHOLD)
L10_BUCKETS = [
    ("cold (≤ 3)",    lambda w: w <= 3),
    ("4-6",           lambda w: 4 <= w <= 6),
    ("7-9",           lambda w: 7 <= w <= 9),
    ("hot (10)",      lambda w: w == 10),
]

# Temperature buckets (from BATTER_ENV_WARM_TEMP_THRESHOLD = 80)
TEMP_BUCKETS = [
    ("cold (< 50°F)",   lambda t: t < 50),
    ("50–65°F",         lambda t: 50 <= t < 65),
    ("65–80°F",         lambda t: 65 <= t < 80),
    ("warm (≥ 80°F)",   lambda t: t >= 80),
]


# ── Data loading ─────────────────────────────────────────────────────────────

def _f(val: str) -> float | None:
    if not val or val.strip() == "":
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _load_conditions() -> dict[tuple[str, str], dict]:
    """Returns {(date, team): perspective-dict} for both home and away teams."""
    if not CONDITIONS_CSV.exists() or CONDITIONS_CSV.stat().st_size < 50:
        return {}

    index: dict[tuple[str, str], dict] = {}
    with CONDITIONS_CSV.open() as f:
        for row in csv.DictReader(f):
            d = row["date"]
            home, away = row["home_team"], row["away_team"]

            # Shared game-level fields (same for both teams)
            shared = {
                "vegas_total":    _f(row["vegas_total"]),
                "wind_speed_mph": _f(row.get("wind_speed_mph", "")),
                "wind_direction": row.get("wind_direction", "").strip() or None,
                "temperature_f":  _f(row.get("temperature_f", "")),
            }

            index[(d, home)] = {
                **shared,
                "opp_starter_era":  _f(row["away_starter_era"]),
                "pitcher_k9":       _f(row["home_starter_k9"]),
                "opp_team_ops":     _f(row["away_team_ops"]),
                "opp_team_k_pct":   _f(row["away_team_k_pct"]),
                "opp_bullpen_era":  _f(row["away_bullpen_era"]),
                "team_moneyline":   _f(row["home_moneyline"]),
                "series_team_wins": _f(row["series_home_wins"]),
                "series_opp_wins":  _f(row["series_away_wins"]),
                "team_l10_wins":    _f(row["home_team_l10_wins"]),
            }
            index[(d, away)] = {
                **shared,
                "opp_starter_era":  _f(row["home_starter_era"]),
                "pitcher_k9":       _f(row["away_starter_k9"]),
                "opp_team_ops":     _f(row["home_team_ops"]),
                "opp_team_k_pct":   _f(row["home_team_k_pct"]),
                "opp_bullpen_era":  _f(row["home_bullpen_era"]),
                "team_moneyline":   _f(row["away_moneyline"]),
                "series_team_wins": _f(row["series_away_wins"]),
                "series_opp_wins":  _f(row["series_home_wins"]),
                "team_l10_wins":    _f(row["away_team_l10_wins"]),
            }
    return index


def _load_players() -> list[dict]:
    with PLAYERS_CSV.open() as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r["real_score"].strip() not in ("", "None")]


# ── Analysis helpers ──────────────────────────────────────────────────────────

def _stats(rs_vals: list[float], hv_vals: list[int]) -> tuple[int, float, float, float]:
    n = len(rs_vals)
    if n == 0:
        return 0, 0.0, 0.0, 0.0
    return (
        n,
        statistics.mean(rs_vals),
        sum(hv_vals) / n,
        sum(1 for v in rs_vals if v > RS_SUCCESS_THRESHOLD) / n,
    )


def _print_row(label: str, n: int, avg_rs: float, hv_rate: float, rs3_rate: float) -> None:
    if n == 0:
        print(f"    {label:<22}  n=0")
    else:
        print(f"    {label:<22}  n={n:<4}  avg_rs={avg_rs:>5.2f}  hv={hv_rate:>5.1%}  rs>3={rs3_rate:>5.1%}")


def _analyze_graduated(label, field, floor, ceiling, ascending, constant_name,
                        players, conditions, is_pitcher):
    buckets: dict[str, tuple[list, list]] = {}

    for row in players:
        pos = row["position"].strip().upper()
        if (pos in ("P", "SP", "RP")) != is_pitcher:
            continue
        cond = conditions.get((row["date"], row["team"]))
        if not cond:
            continue
        val = cond.get(field)
        if val is None:
            continue

        rs = float(row["real_score"])
        hv = 1 if row["is_highest_value"] == "1" else 0

        if ascending:
            bkt = f"< {floor}" if val <= floor else (f"> {ceiling}" if val >= ceiling else f"{floor}–{ceiling}")
        else:
            bkt = f"< {floor} (good)" if val <= floor else (f"> {ceiling} (bad)" if val >= ceiling else f"{floor}–{ceiling}")

        rs_l, hv_l = buckets.setdefault(bkt, ([], []))
        rs_l.append(rs)
        hv_l.append(hv)

    if not buckets:
        print(f"  {label}: no linked data\n")
        return

    print(f"  {label}  [{constant_name}]")
    print(f"  Current: floor={floor}  ceiling={ceiling}")

    order = ([f"< {floor}", f"{floor}–{ceiling}", f"> {ceiling}"] if ascending else
             [f"< {floor} (good)", f"{floor}–{ceiling}", f"> {ceiling} (bad)"])
    total_n = 0
    for bkt in order:
        rs_l, hv_l = buckets.get(bkt, ([], []))
        n, avg_rs, hv_rate, rs3 = _stats(rs_l, hv_l)
        total_n += n
        _print_row(bkt, n, avg_rs, hv_rate, rs3)

    suffix = f"n={total_n}" if total_n >= MIN_N_FOR_SUGGESTION else f"n={total_n} < {MIN_N_FOR_SUGGESTION} — sparse"
    print(f"    → {suffix}\n")


def _analyze_bucketed(label, constant_name, bucket_defs, field_fn, players, conditions, is_pitcher):
    """Generic bucketed analysis. field_fn(cond) returns the value(s) needed by bucket predicates."""
    buckets: dict[str, tuple[list, list]] = {name: ([], []) for name, _ in bucket_defs}

    for row in players:
        pos = row["position"].strip().upper()
        if (pos in ("P", "SP", "RP")) != is_pitcher:
            continue
        cond = conditions.get((row["date"], row["team"]))
        if not cond:
            continue

        val = field_fn(cond)
        if val is None:
            continue

        rs = float(row["real_score"])
        hv = 1 if row["is_highest_value"] == "1" else 0

        for name, predicate in bucket_defs:
            if predicate(*val) if isinstance(val, tuple) else predicate(val):
                buckets[name][0].append(rs)
                buckets[name][1].append(hv)
                break

    has_data = any(len(rs_l) > 0 for rs_l, _ in buckets.values())
    if not has_data:
        print(f"  {label}: no linked data\n")
        return

    print(f"  {label}  [{constant_name}]")
    total_n = 0
    for name, (rs_l, hv_l) in buckets.items():
        n, avg_rs, hv_rate, rs3 = _stats(rs_l, hv_l)
        total_n += n
        _print_row(name, n, avg_rs, hv_rate, rs3)

    suffix = f"n={total_n}" if total_n >= MIN_N_FOR_SUGGESTION else f"n={total_n} < {MIN_N_FOR_SUGGESTION} — sparse"
    print(f"    → {suffix}\n")


def _analyze_wind(players, conditions):
    """Wind bonus requires speed ≥ 10 mph AND out direction — show as combination."""
    WIND_OUT = {"OUT", "L TO R", "R TO L", "OUT TO CF"}

    bkts = {
        "calm (< 10 mph)":         ([], []),
        "wind in (10+ mph)":       ([], []),
        "wind out (10+ mph)":      ([], []),
        "wind neutral (10+ mph)":  ([], []),
    }

    for row in players:
        pos = row["position"].strip().upper()
        if pos in ("P", "SP", "RP"):
            continue
        cond = conditions.get((row["date"], row["team"]))
        if not cond:
            continue

        spd = cond.get("wind_speed_mph")
        direction = (cond.get("wind_direction") or "").upper()
        if spd is None:
            continue

        rs = float(row["real_score"])
        hv = 1 if row["is_highest_value"] == "1" else 0

        if spd < 10:
            bkt = "calm (< 10 mph)"
        elif any(d in direction for d in WIND_OUT):
            bkt = "wind out (10+ mph)"
        elif "IN" in direction:
            bkt = "wind in (10+ mph)"
        else:
            bkt = "wind neutral (10+ mph)"

        bkts[bkt][0].append(rs)
        bkts[bkt][1].append(hv)

    has_data = any(len(rs_l) > 0 for rs_l, _ in bkts.values())
    if not has_data:
        print("  Wind (speed + direction): no linked data\n")
        return

    print("  Wind  [BATTER_ENV_WIND_SPEED_MIN / WIND_OUT_DIRECTIONS]")
    total_n = 0
    for name, (rs_l, hv_l) in bkts.items():
        n, avg_rs, hv_rate, rs3 = _stats(rs_l, hv_l)
        total_n += n
        _print_row(name, n, avg_rs, hv_rate, rs3)
    suffix = f"n={total_n}" if total_n >= MIN_N_FOR_SUGGESTION else f"n={total_n} — sparse"
    print(f"    → {suffix}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

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

    # ── BATTER ──
    print("=" * 70)
    print("BATTER ENV FACTORS")
    print("=" * 70)

    for args in BATTER_GRADUATED:
        _analyze_graduated(*args, players, conditions, is_pitcher=False)

    _analyze_bucketed(
        "Team Moneyline", "BATTER_ENV_ML_FLOOR / CEILING",
        ML_BUCKETS, lambda c: c.get("team_moneyline"),
        players, conditions, is_pitcher=False,
    )

    _analyze_bucketed(
        "Series Context", "SERIES_LEADING_BONUS / TRAILING_PENALTY",
        SERIES_BUCKETS,
        lambda c: (
            (c["series_team_wins"], c["series_opp_wins"])
            if c.get("series_team_wins") is not None and c.get("series_opp_wins") is not None
            else None
        ),
        players, conditions, is_pitcher=False,
    )

    _analyze_bucketed(
        "Team L10 Wins", "TEAM_HOT_L10_THRESHOLD / COLD_L10_THRESHOLD",
        L10_BUCKETS, lambda c: c.get("team_l10_wins"),
        players, conditions, is_pitcher=False,
    )

    _analyze_bucketed(
        "Temperature", "BATTER_ENV_WARM_TEMP_THRESHOLD",
        TEMP_BUCKETS, lambda c: c.get("temperature_f"),
        players, conditions, is_pitcher=False,
    )

    _analyze_wind(players, conditions)

    # ── PITCHER ──
    print("=" * 70)
    print("PITCHER ENV FACTORS")
    print("=" * 70)

    for args in PITCHER_GRADUATED:
        _analyze_graduated(*args, players, conditions, is_pitcher=True)

    _analyze_bucketed(
        "Team Moneyline", "PITCHER_ENV_ML_FLOOR / CEILING",
        ML_BUCKETS, lambda c: c.get("team_moneyline"),
        players, conditions, is_pitcher=True,
    )

    print("─" * 70)
    print("After reviewing, edit app/core/constants.py to adjust thresholds.")
    print("No changes are made automatically.")


if __name__ == "__main__":
    main()
