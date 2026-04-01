# Baseball DFS Predictor - AI Assistant Guide

## Architecture: "The Three File Kingdom"
This app relies entirely on three static files located in `/data/`:
1. `historical_players.csv`: Master player ledger (1 row per player/day).
2. `historical_winning_drafts.csv`: Top 20 lineups (5 rows per lineup).
3. `historical_slate_results.json`: MLB game outcomes.

## Core Rules & Business Logic
1. **The App is Sport-Specific:** Do NOT suggest or write logic for NBA features (minutes played, rebounds, etc.). Assume all entities are MLB.
2. **Target Variable (`total_value`):** In `historical_players.csv`, the `total_value` column is mathematically absolute. It must always be calculated as: `total_value = real_score * (2 + card_boost)`. Never leave it null.
3. **Enrichment:** Real Sports data does NOT provide Team or Position. During ingestion, the script or AI must automatically append standard 3-letter MLB team abbreviations (e.g., LAD, NYY) and standard positions (P, C, 1B, 2B, 3B, SS, OF, DH) to the player rows.
4. **Volume:** Ownership volume is captured in a single `drafts` column. We use boolean flags (`is_most_popular`, `is_highest_value`, `is_most_drafted_3x`) to indicate which leaderboards the player appeared on.
