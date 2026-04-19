#!/bin/bash
set -euo pipefail

# =============================================================================
# HISTORICAL DATA INGEST GUIDE
# =============================================================================
#
# Canonical reference: see "Ingesting New Slate Data" in CLAUDE.md. This
# comment block is a startup cheat-sheet — keep it in sync with CLAUDE.md.
#
# Current coverage (2026-04-14): 21 consecutive dates, 2026-03-25 → 2026-04-14.
# All four files stay in lockstep — a date present in one must be present in
# all four. After each slate, append to these four files in data/:
#
# ── 1. data/historical_players.csv ──────────────────────────────────────────
#
#   Columns (in order):
#     date, player_name, team, position, real_score, card_boost, drafts,
#     total_value, is_highest_value, is_most_popular, is_most_drafted_3x
#
#   Rules:
#   • One row per player per slate day.
#   • team = 3-letter MLB abbreviation (NYY, LAD, KC, SD, CHW, BAL, etc.)
#   • position = P, C, 1B, 2B, 3B, SS, OF, DH
#   • real_score = the RS shown on the platform (can be negative)
#   • card_boost = the multiplier shown (+1.5x → 1.5). No boost = 0.0 or blank
#   • drafts = total draft count shown on the platform (1.5k → 1500)
#   • total_value = real_score × (2 + card_boost)  ← compute via compute_total_value() only
#   •   card_boost is for historical total_value ONLY — never a scoring/prediction input
#   • is_highest_value = 1 if player is in the Highest Value leaderboard
#   • is_most_popular = 1 if player is in the Most Popular leaderboard
#   • is_most_drafted_3x = 1 if player is in the Most Drafted 3x leaderboard
#   • A player can have multiple flags set (e.g., 0,1,1 for MP + MD3x)
#   • Leave card_boost blank (not 0.0) if the player truly has no boost card
#   • "—" in the platform UI = 0.0 boost; "—" for real_score = 0.0
#
#   Platform table → CSV column mapping:
#     Most Popular:    "Value" → real_score | "Multiplier" → card_boost
#                      "Drafts" → drafts
#     Highest Value:   "Value" (first)  → real_score
#                      "Multiplier"     → card_boost
#                      "Drafts"         → drafts (use this count, not HV slot)
#                      "Value" (second) → total_value (verify vs formula)
#     Most Drafted 3x: same mapping as Most Popular
#
#   Example rows:
#     2026-04-09,Aaron Judge,NYY,OF,-0.7,2.3,3900,-3.01,0,1,0
#     2026-04-09,Mick Abel,BAL,P,4.6,3.0,1700,23.0,0,1,1
#     2026-04-09,Munetaka Murakami,LAD,OF,0.3,3.0,1400,1.5,0,0,1
#
# ── 2. data/historical_winning_drafts.csv ───────────────────────────────────
#
#   Columns (in order):
#     date, winner_rank, slot_index, player_name, team, position,
#     real_score, slot_mult, card_boost
#
#   Rules:
#   • 5 rows per lineup (one per slot). Record top 20 lineups → 100 rows/day.
#   • winner_rank = 1–20 (leaderboard position)
#   • slot_index = 1–5 (slot 1 = 2.0x, 2 = 1.8x, 3 = 1.6x, 4 = 1.4x, 5 = 1.2x)
#   • slot_mult = the multiplier for that slot (2.0, 1.8, 1.6, 1.4, 1.2)
#   • card_boost = the card's boost for that player in that lineup
#
#   Example rows:
#     2026-04-09,1,1,Mick Abel,BAL,P,4.6,2.0,3.0
#     2026-04-09,1,2,Seth Lugo,KC,P,4.1,1.8,0.0
#
# ── 3. data/historical_slate_results.json ───────────────────────────────────
#
#   Append one JSON object to the top-level array per slate day.
#   Minimum required fields:
#     "date"         → "YYYY-MM-DD"
#     "game_count"   → integer (0 if unknown)
#     "games"        → array of game objects (empty [] if scores not captured)
#     "season_stage" → "regular-season"
#     "source"       → "screenshot_ingest"
#     "saved_at"     → "YYYY-MM-DDT00:00:00Z"
#     "notes"        → free-text summary of key outcomes (ghost wins, traps, etc.)
#
#   Each game object (when scores are available):
#     { "home": "NYY", "away": "BOS", "home_score": 5, "away_score": 2,
#       "winner": "NYY", "loser": "BOS", "winner_score": 5, "loser_score": 2 }
#
#   Notes should capture: biggest RS values, ghost wins, boost traps, 3x busts,
#   crowd overreactions, and any patterns relevant to V2 strategy validation.
#
# ── 4. data/hv_player_game_stats.csv ────────────────────────────────────────
#
#   Columns (in order):
#     date, player_name, team_actual, position, real_score, card_boost,
#     game_result, ab, r, h, hr, rbi, bb, so, ip, er, k_pitching,
#     decision, notes
#
#   Rules:
#   • One row per Highest-Value-leaderboard player appearance (grows each slate).
#   • Batters fill ab/r/h/hr/rbi/bb/so; pitchers fill ip/er/k_pitching/decision.
#     Leave non-applicable columns blank — do NOT zero-fill across roles.
#   • card_boost blank if no boost card (same convention as file 1).
#   • game_result: free-form score string ("SF 0 NYY 7").
#   • notes: short summary ("2-for-3 | vs SF (away)", "Minimal contribution").
#
#   Example rows:
#     2026-03-25,Austin Wells,NYY,C,1.2,,SF 0 NYY 7,3.0,1.0,2.0,0.0,0.0,1.0,0.0,,,,,2-for-3 | vs SF (away)
#     2026-03-25,Ben Rice,NYY,1B,0.2,,SF 0 NYY 7,4.0,1.0,1.0,0.0,0.0,1.0,2.0,,,,,Minimal contribution | vs SF (away)
#
# ── Quick checklist when ingesting a new slate ───────────────────────────────
#   [ ] Append player rows to historical_players.csv (MP + MD3x mandatory;
#       HV optional — set is_highest_value=1 only if recording HV leaderboard)
#   [ ] Append winning lineup rows to historical_winning_drafts.csv if available
#   [ ] Append slate entry to historical_slate_results.json
#   [ ] Append HV box-score rows to hv_player_game_stats.csv (one row per HV
#       leaderboard appearance, batter vs pitcher columns mutually exclusive)
#   [ ] Verify total_value = real_score × (2 + card_boost) for each row
#       (card_boost is for historical data ONLY — never used in scoring/prediction)
#   [ ] Players appearing in multiple leaderboards → single row, multiple flags
#   [ ] Reload DB: rm db/ben_oracle.db && python -m app.seed
#       (the seeder is idempotency-guarded on an empty DB — no incremental mode)
# =============================================================================

# Only run in remote (Claude Code on the web) environments
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

echo "==> Installing Python dependencies..."
pip install --quiet -e ".[dev]"

echo "==> Creating db/ directory if missing..."
mkdir -p db

echo "==> Creating .env if missing..."
if [ ! -f ".env" ]; then
  cp .env.example .env
fi

echo "==> Setting PYTHONPATH..."
echo 'export PYTHONPATH="$CLAUDE_PROJECT_DIR"' >> "$CLAUDE_ENV_FILE"

echo "==> Reading project documentation into context..."
echo "--- CLAUDE.md (full) ---"
cat "$CLAUDE_PROJECT_DIR/CLAUDE.md" 2>/dev/null || echo "(CLAUDE.md not found)"
echo "--- README.md (full) ---"
cat "$CLAUDE_PROJECT_DIR/README.md" 2>/dev/null || echo "(README.md not found)"
echo "---"

echo "==> Session start complete."
