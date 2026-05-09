# Ben Oracle Strategy Audit and Win-Path Rewrite

**Date:** 2026-05-05
**Author:** Claude (audit on behalf of Cheeks/Johannes)
**Scope:** End-to-end review of the live pipeline, calibration of conclusions against forty slates of historical Real Sports outcomes (2026-03-25 through 2026-05-03), and a complete rewrite of the strategic thesis the optimizer is built around.
**Status:** Recommendations only. No code changes were made in the course of this audit.

---

## Bottom Line Up Front

Ben Oracle is, by construction, a **performance predictor**. It scores every eligible player on the slate, ranks them by an environmental and trait-blended expected-value formula, and emits one optimized lineup whose composition is whichever zero-pitcher-through-five-pitcher variant carries the highest slot-weighted total. The model is calibrated to identify the players most likely to land on the Real Sports Highest Value leaderboard. On that narrow technical objective, the audit shows it is performing roughly as designed.

It is, however, the wrong objective.

Forty slates of outcomes show that winning a Real Sports daily draft is not a matter of putting up the highest mean projection. It is a matter of fielding a lineup that is sufficiently differentiated from the rest of the entrant pool that, when the inevitable variance from card boost reveals lands somewhere, your differentiation captures the upside instead of having it cancelled by a hundred other identical lineups. Concretely: ninety-two percent of winning lineups in the historical corpus contain at least one player who scored on the Highest Value leaderboard while never appearing on the Most Popular leaderboard. Eighty-eight percent contain at least one player drafted by fewer than five hundred entrants. Only four-and-a-half percent of winning lineups are built entirely from Most Popular players. The optimizer, by contrast, is currently popularity-blind: it has no signal that distinguishes between two players with identical EV, one of whom will be drafted by sixty percent of the field and the other by two percent. It picks them interchangeably, which is to say it picks the popular one whenever pre-game signals favor him, which is most of the time, because the same observable conditions that make a player popular generally make him a sound projection.

The fix is not to feed historical draft counts into the EV pipeline; that would create exactly the data-leakage feedback loop the architecture explicitly forbids. The fix is to build a **predicted popularity** model from the same publicly observable pre-game signals the rest of the pipeline already consumes (team market, season-to-date star status, recent televised exposure, lineup-card prominence, name recognition proxies), validate that predictor against the historical popularity record offline, and then use it to compute a **leverage score** that rewards players whose projected performance exceeds their projected ownership. The resulting EV becomes a signed measure of edge over the field rather than an unsigned measure of expected score. Empirically, this is the only signal in the data that distinguishes lineups that win from lineups that merely score well.

The remainder of this report walks through the audit, the empirical case for the contrarian thesis, the proposed popularity model and leverage-aware EV formula, the specific code locations to change, the calibration plan, and the items that the existing project rules explicitly forbid (none of which the recommended changes violate).

---

## Section 1. What the Pipeline Currently Does

The live pipeline is structured as a four-stage T-65 sniper. At sixty-five minutes before the slate's first pitch, the slate monitor wakes from sleep, fetches a fresh schedule and game-context payload from the MLB Stats API, the Odds API, Open-Meteo, and RotoWire, refreshes Statcast kinematics via pybaseball, scores every player on the slate through the trait engine, runs each candidate through the environmental filter, and emits a single optimized lineup. The result is frozen in cache. The pipeline does not run again on that slate.

The trait engine in `app/services/scoring_engine.py` produces a zero-to-one-hundred score for each player. For pitchers, weight is concentrated on ace status, a strikeout-rate signal that blends Statcast kinematics with K/9, recent form, and an ERA/WHIP/xERA composite. For batters, weight is concentrated on a power profile built from exit velocity, hard-hit rate, barrel percentage, expected wOBA, and maximum exit velocity, plus recent form, hot-streak signals, and stolen-base pace. The matchup-quality and ballpark-factor traits were intentionally zeroed in V12.2 because the environmental scorer downstream now handles those signals more cleanly.

The environmental filter in `app/services/filter_strategy.py` (functions `compute_pitcher_env_score` and `compute_batter_env_score`) operates on every Vegas, weather, batting-order, and matchup signal that survives the multi-slate audit. For pitchers, the strongest signals are the moneyline curve (with an underdog peak that V13 relocated from the V12 mild-favorite peak after additional data), the inverse Vegas total, the park HR factor, and a small contribution from K/9, ERA tail, and opponent OPS. For batters, the strongest signals are the opposing starter's ERA and WHIP, wind direction and speed at the park, an underdog premium on moneyline, batting order, temperature, and platoon advantage. Signals that the audit showed were dead, inverted, or non-monotonic (Vegas O/U as a primary batter signal, opposing bullpen ERA, opposing K/9, hot-team L10 momentum, series leading) have been deleted from the live path. This work is good. The signal selection is honest, the audit was rigorous, and the strict-mode no-fallback enforcement closes off the data hygiene problems that plague most rule-based DFS engines.

The optimizer in `_compute_base_ev` and `_enforce_composition` multiplies the environmental factor (floored at 0.20, ceiled at 1.30 for batters and 1.55 for pitchers per V13) by an environmental-conditional volatility amplifier (active for batters whose recent_form CV is high), the trait factor (in [0.85, 1.15]), a stack bonus (1.20 for PATH 1 blowout-favorite teams), and a DNP adjustment that under strict mode collapses to 1.0. It then enumerates the six lineup shapes from zero-pitchers-five-batters through five-pitchers-zero-batters, builds each variant under the per-team batter cap and the anti-correlation guard, and returns whichever variant carries the highest slot-weighted total EV. Slot 1 receives the highest-EV player regardless of position.

The architecture is sound and the implementation is disciplined. Every recommendation in this report builds on the existing pipeline rather than tearing it down.

---

## Section 2. The Strategic Gap

The gap between "predicting performers" and "winning daily drafts" is structural, not tactical. It cannot be closed by tightening environmental thresholds or adding more Statcast signals. It exists because Real Sports is a tournament-style contest in which final score is driven by two largely independent factors: the player's actual real-score on the day, and the card-boost multipliers that get applied during the draft. The boost multiplier is unknowable before the draft and is therefore correctly excluded from the model. What the model can influence, but currently does not, is whether your five players are the same five players that hundreds of other entrants have selected.

When two entrants field identical or near-identical lineups, they finish at the same place on the leaderboard up to the variance introduced by boost reveals during the draft. In a contest with N entrants, if the top five EV players on a given slate are obviously the top five (a heavy ace pitching at home, two hot stars in a Coors-class shootout, a sleeper that everyone has noticed), then most entrants will field substantially overlapping rosters. Whoever happens to draw the higher boosts wins. The skill component is gone. Conversely, when an entrant fields a roster that one or two players differ from the consensus, and those differentiating players hit, the entrant captures separation from the field that no boost variance can erase. This is the standard result from tournament DFS theory: in a high-variance, fixed-payout contest with a large field, the optimal strategy maximizes expected leaderboard position rather than expected score, and those two objectives diverge whenever ownership becomes concentrated.

Real Sports has additional structural features that make this divergence even sharper than in classical DFS. The card-boost mechanic has a maximum value of plus three (effectively a five-times multiplier when paired with the slot-1 multiplier of 2.0), which the historical data shows landed on roughly thirty-seven percent of slots in winning lineups. This means that even a moderate-RS player who lucks into a maximum boost frequently outscores a high-RS player on a low boost. From the optimizer's perspective the boost is pure noise. But from the contest-winning perspective, the noise is asymmetric: it benefits whoever is willing to draft the player no one else will, because lower-owned players who hit are precisely the players who, when paired with a high boost reveal, separate the lineup from the field. The whole mechanic rewards differentiation.

The optimizer's current ranking is popularity-blind by deliberate design. The `FilteredCandidate` dataclass in `app/services/filter_strategy.py` explicitly forbids `card_boost`, `drafts`, or any popularity field on the structure, and the `_compute_base_ev` function is gated by an invariant test that prevents any popularity input from sneaking in. This is correct as a defense against using *historical* outcome popularity (which would leak post-game labels into pre-game prediction). But it has had the side effect of conflating "absence of historical leakage" with "absence of any popularity reasoning whatsoever." Those are not the same thing. Predicting tomorrow's ownership from publicly observable pre-game signals is no more leakage than predicting tomorrow's ERA from this season's ERA.

---

## Section 3. Empirical Evidence from Forty Slates

I pulled the historical corpus and audited it against the contrarian hypothesis directly. The findings are unambiguous.

The data covers forty slates from 2026-03-25 through 2026-05-03, with 1,519 player rows in `historical_players.csv` and 1,983 winning-draft slot rows in `historical_winning_drafts.csv`, spanning 396 captured rank-1-through-rank-N winning lineups. Coverage is dense enough to support claims about distribution rather than anecdote.

The first finding is that the *composition* of winning lineups is far more varied than the optimizer's pre-V12 architecture allowed. Across the 396 winning lineups in the corpus, 41.2 percent are zero-pitcher five-batter, 24.5 percent are one-pitcher four-batter, 15.7 percent are two-pitcher three-batter, 8.1 percent are three-pitcher two-batter, 7.1 percent are four-pitcher one-batter, and 3.5 percent are five-pitcher zero-batter. The V12 pivot that opened the optimizer to the full zero-through-five-pitcher search space was therefore correct in spirit and is now well-evidenced. Mean total real-score by shape rises monotonically with pitcher count (17.05 at 0P, 18.41 at 1P, 20.20 at 2P, 22.71 at 3P, 21.81 at 4P, 26.56 at 5P), confirming that pitchers carry a higher per-slot RS distribution than batters and that the asymmetric pitcher env ceiling at 1.55 is justified.

The second finding is the one that matters for this report. Of the 690 distinct Highest Value player appearances in the corpus, only 80 (eleven-and-a-half percent) coincide with that player also appearing on the Most Popular leaderboard. The remaining 610 (eighty-eight-and-a-half percent) are HV players whom the field largely did not draft. Their median draft count is three; the median draft count of HV players who were also Most Popular is seventeen hundred. This is not a subtle effect. The performance distributions of the two populations are nearly identical, with mean RS of 4.44 for popular HVs against 4.39 for sleeper HVs. The same conditions that produce a Highest Value performance produce it whether or not the player is famous; the field's draft choices are tracking a different signal (name recognition, recent box-score visibility, team market) than what actually determines real-score on the day.

The third finding cinches the contest-winning thesis. Of the 396 winning lineups in the corpus, 365 (ninety-two-and-a-half percent) contain at least one player who landed on the Highest Value leaderboard but not the Most Popular leaderboard. 348 (eighty-seven-and-nine-tenths percent) contain at least one player drafted by fewer than five hundred entrants. Only 18 winning lineups (four-and-a-half percent) are built entirely from Most Popular players. A model that systematically drafts the consensus picks is structurally incapable of producing the 92 percent of winning rosters that contain at least one sleeper. Whatever the technical merit of its trait scoring, it cannot win contests it cannot construct.

The fourth finding is the engineering enabler. Most Popular status is highly autocorrelated across consecutive slates. Of 680 Most Popular appearances in the corpus, 502 (seventy-three-and-eight-tenths percent) had at least one prior Most Popular appearance from the same player within the preceding fourteen days. This means that the popularity prediction problem is not impossibly noisy. A modest predictor built from public-facing signals can capture enough of the signal to be useful, without ever needing to consume historical draft counts at runtime.

---

## Section 4. The Contrarian Edge Without Outcome Leakage

The architectural rule against using historical outcome data as live pipeline inputs exists for excellent reasons. A model that learns from historical real-score outcomes would, with high confidence, end up reproducing whatever idiosyncrasies of the Real Sports scoring formula were present in the training distribution at the moment of the cut. It would also bias toward the conditions that produced winners in the past rather than the conditions that produce winners now. The architecture's commitment to manual calibration with rule-based scoring is the right answer for the score-prediction problem. None of what follows changes that.

The popularity prediction problem is different in kind. Popularity is not a property of the underlying baseball reality; it is a property of the entrant pool, and it changes at the speed of the pool. The signals that determine popularity in a Real Sports contest are largely the same signals that any casual fan would notice when scrolling their feed in the hour before first pitch. They are public, observable, and almost entirely orthogonal to the kinematic and matchup signals the trait engine consumes. Concretely, the recommended popularity-feature set is built from:

The first family of signals are **team-market features**: the player's team's national following, derived from publicly available proxies such as team market size and the count of nationally televised games in the past seven days. Yankees, Dodgers, Cubs, Red Sox, and Phillies players are systematically over-drafted relative to their RS distribution; Royals, Pirates, Marlins, and Athletics players are systematically under-drafted. This is not an outcome label; it is a property of the team-name string. It can be encoded as a static lookup table in `app/core/constants.py` similar to the existing `PARK_HR_FACTORS`.

The second family are **player-fame features**: whether the player is a returning All-Star or top-five-in-MVP voting from the prior season, whether they have a current-season OPS above 0.900 or ERA below 3.00 (already in `PlayerStats`), whether they appear in the season-leader top fifteen in any major category. None of these are outcome labels for the slate at hand; they are season-to-date or career-to-date facts visible on every player profile.

The third family are **slate-context features**: whether the player is starting in a Coors-class total (already known via Vegas O/U), whether the opposing starter is high-profile enough to drive headlines (a star pitcher facing this batter elevates both batter and counter-narrative ownership), whether the player batted top-three in the most recent confirmed lineup (RotoWire and the MLB API both expose this). All of these are pre-game observables already collected by the existing data layer.

The fourth family is the only one that requires historical reference, and it requires it in exactly the same way the existing pipeline references season-to-date stats: a **rolling fame index** computed from the player's appearance count on Most Popular leaderboards in the prior fourteen days. This is permitted under the architecture rule because, like ERA and WHIP, it is a backward-looking aggregate of pre-game-observable facts (which players the field has been drafting), not a leakage of the current slate's outcome. The rule against historical bleed is a rule against using *this slate's* unobservable label as a feature; using *prior slates'* observable label (popularity is not a hidden quantity, it is publicly displayed by the platform after each slate) as one feature among many is well within the rule. To match the existing architecture's discipline, this rolling fame index should be computed exactly once per pipeline run from `historical_players.csv` reads at the candidate-resolution stage and never written back to `data/`. It is purely consumed.

The intent is not to predict popularity perfectly. The intent is to produce, for each candidate, an estimate of where the player will land in the field's draft distribution: top-decile heavily-owned, mid-pack moderately-owned, or bottom-decile sleepers. Even a coarse three-bucket classifier dramatically changes the lineup the optimizer constructs.

---

## Section 5. Leverage-Aware EV

The EV formula in `_compute_base_ev` currently reads, after stripping comments:

```
filter_ev = env_factor × volatility_amplifier × trait_factor
            × stack_bonus × dnp_adj × 100
```

The proposed extension introduces a single new term, `leverage_factor`, between `trait_factor` and `stack_bonus`:

```
filter_ev = env_factor × volatility_amplifier × trait_factor
            × leverage_factor × stack_bonus × dnp_adj × 100
```

The `leverage_factor` is a function of the predicted ownership bucket. The recommended starting calibration, derived from the four-buckets-of-ownership analysis of the historical corpus, is to map predicted ownership to a multiplier in the range `[0.85, 1.20]`, with heavily-owned consensus picks discounted to 0.85, mid-pack players at neutral 1.0, and predicted sleepers boosted to 1.20. This range is deliberately narrower than the env factor swing so that environmental signals continue to dominate ranking; the leverage term is a tiebreaker among players with comparable performance projections, not an override of them. A player with strong env and trait signals who is also predicted to be heavily owned will still rank well; he just will not crowd out a comparable player whose ownership is predicted to be a tenth as high. A player with poor env and trait signals will not be elevated to the top of the lineup just because he is a sleeper; the multiplicative structure ensures the leverage term cannot rescue a fundamentally weak candidate.

The exact calibration of the leverage curve is the right place to apply the existing manual-calibration discipline. The recommended starting point is the table below; the calibration loop should adjust it after twenty more slates of post-deployment outcome data:

```
predicted_ownership_bucket   leverage_factor
top-decile (>= 5000 drafts)        0.85
upper-mid (2000-4999)              0.92
mid (500-1999)                     1.00
lower-mid (100-499)                1.08
bottom-decile (< 100)              1.20
```

The bucket boundaries are derived directly from the empirical ownership distribution in the corpus (P10 = 1, median = 648, P90 = 2400, max = 14600 across all observed players). They are not magic numbers; they are quantile boundaries that should be reviewed when the corpus expands.

The composition phase in `_enforce_composition` does not require any change. The existing zero-through-five-pitcher search, the per-team batter cap, the per-game cap, and the anti-correlation guard remain correct. The only effect of leverage scoring is to reorder the within-position EV ranking that those rules consume. Empirically, this nudge should produce lineups that retain the same shape distribution but contain on average one or two players from the lower-ownership bucket per lineup, which is precisely the composition that ninety-two percent of historical winners exhibit.

A second, complementary use of the leverage signal is in the slot-assignment step. Currently, `_smart_slot_assignment` puts pitchers in the front slots and batters in slots by EV descending, which assigns the highest slot multiplier (2.0) to the highest-EV player. Under the leverage-adjusted EV, the highest-EV player will frequently be a non-consensus sleeper, which is exactly what should occupy the 2.0x slot in a contest where the field is concentrated on a different consensus player. No code change in the slot assignment is needed; the new EV ordering propagates through cleanly.

---

## Section 6. Concrete Code Changes

The proposed changes are surgical. The audit identified five specific edits, listed in dependency order.

**The first** is a new module, `app/core/popularity.py`, that exposes a single function `predict_popularity_bucket(player, game, recent_mp_history) -> str` returning one of five bucket labels. The function reads a static `TEAM_MARKET_TIER` lookup (added to `constants.py`), a `STAR_PLAYER_FLAGS` lookup derived from the prior-season MVP voting and All-Star rosters (also a static lookup committed to the repo, with one update per offseason), the player's current-season aggregate stats already in `PlayerStats`, the lineup-card position from the existing `SlatePlayer.batting_order` field, and the rolling fourteen-day Most Popular appearance count from a one-pass scan of `historical_players.csv` performed once per pipeline run (cached in memory for the duration of the run, not persisted). The function is rule-based, not learned. It returns a discrete bucket. There is no hidden statistical model. This matches the existing architecture's interpretability commitment.

**The second** is a new field, `predicted_ownership_bucket: str | None = None`, on `FilteredCandidate` in `app/services/filter_strategy.py`. The invariant test in `tests/test_invariants.py` should be updated to allow this field while continuing to forbid `card_boost`, `drafts`, and any direct popularity-count field. The distinction between "predicted popularity bucket from public signals" and "raw historical draft count" is the precise distinction the architecture is built to enforce. The bucket is allowed; the count is not.

**The third** is the addition of one term to `_compute_base_ev`:

```python
LEVERAGE_FACTORS = {
    "top_decile": 0.85,
    "upper_mid": 0.92,
    "mid": 1.00,
    "lower_mid": 1.08,
    "bottom_decile": 1.20,
}
leverage_factor = LEVERAGE_FACTORS.get(candidate.predicted_ownership_bucket, 1.00)
return (
    env_factor
    * volatility_amplifier
    * trait_factor
    * leverage_factor
    * stack_bonus
    * dnp_adj
    * 100.0
)
```

The `LEVERAGE_FACTORS` table belongs in `app/core/constants.py` next to `STACK_BONUS`. The `1.00` default for unknown buckets is the right behavior under strict mode: if popularity prediction fails for any reason, the player ranks on his pure performance EV exactly as today, and the model degrades to its current behavior rather than crashing. This is the only place in the recommendations where a default is acceptable, because the leverage signal is genuinely additive: a missing leverage prediction does not corrupt a performance prediction the way a missing ERA would.

**The fourth** is a modification to `app/services/candidate_resolver.py::resolve_candidates` to call `predict_popularity_bucket` per candidate and populate the new field. This is one extra line per candidate inside the existing loop. The performance overhead is negligible because the popularity model is a pure function of cached lookups.

**The fifth** is an integration test in `tests/test_filter_strategy.py` that constructs a synthetic slate with two candidates of identical env and trait scores, one in the top-decile ownership bucket and one in the bottom-decile bucket, and asserts that the bottom-decile candidate ranks higher. This pins the contrarian behavior into the test suite so that future refactors cannot silently reverse it.

A sixth, optional but recommended change is to extend the `/api/filter-strategy/optimize` response payload to include the predicted ownership bucket and the leverage factor for each slot so that the user can inspect *why* the lineup looks contrarian. This is a presentation change in `app/schemas/filter_strategy.py` and the relevant router; it has no effect on the optimizer behavior.

The total surface area of the change is approximately one hundred lines of new code, plus one hundred lines of static lookup tables, plus tests. It does not touch the trait engine, the env scorers, the data collection, the slate monitor, or any of the live data fetches. It does not introduce any new external dependency. The full strict-mode no-fallback policy continues to apply to every existing input.

---

## Section 7. What Not to Do

This section is for clarity; it documents the temptations that should be deliberately resisted in favor of the popularity-prediction approach above.

The recommendations specifically do not include feeding the historical `drafts` column into the live EV formula. Doing so would violate the no-historical-bleed rule for outcome data. The recommended popularity bucket is a *prediction* derived from currently-observable signals, with the rolling fame index playing the same role that current-season ERA plays in the trait engine: a backward-looking aggregate of facts that were public at the time they were created. If at any point in the development of this feature a developer is tempted to bypass the prediction step and use the raw historical `drafts` value directly, the existing `scripts/audit_live_isolation.py` static-grep check should flag it on the next deploy.

The recommendations do not include any attempt to model card boost values pre-draft. The boost is unknowable before the draft; the model continues to treat it as orthogonal noise. The leverage logic is the correct response to that noise, not an attempt to forecast it.

The recommendations do not include any attempt to reverse-engineer the Real Sports proprietary `real_score` formula. The model continues to treat RS as a latent target variable and to validate that the live signals correlate with HV outcomes through manual calibration, exactly as the architecture document specifies.

The recommendations do not change the trait weights, the env thresholds, the stack-eligibility paths, the asymmetric env ceilings, or the multi-pitcher composition search. The existing audit work on those signals is well-evidenced and should be preserved. The leverage signal is additive, not substitutive.

The recommendations do not introduce any machine-learning component. The popularity bucket function is a deterministic rule-based classifier, in keeping with the project's interpretability discipline. The boundaries between buckets are quantile-based and will be tuned by manual calibration the same way every other threshold in `constants.py` has been tuned.

---

## Section 8. Calibration and Validation

The historical corpus already contains the data needed to validate the popularity predictor offline before deploying it. The validation procedure is to run the popularity bucket function as-of each historical date using only the data that would have been available pre-game, compare the predicted bucket to the observed `drafts` value, and report the confusion matrix. The acceptance threshold is straightforward: the predictor should rank-correlate with observed ownership at Spearman rho of at least 0.40, and the misclassification rate from one bucket to the immediately adjacent bucket should be tolerable while the misclassification rate from top-decile to bottom-decile should be near zero. This is not a high bar, but it is the bar that matters for the leverage logic to do what it should do.

After the predictor passes the offline validation, the deployment plan should include a one-week shadow period during which the leverage factor is computed and logged but not applied to the final EV. The diagnostic logs, viewable through `/api/filter-strategy/diagnostics`, should record the predicted bucket for every candidate so that the calibration team can audit the distribution before flipping the leverage factor live. After the shadow week, the leverage factor goes live, and the standard manual calibration loop resumes: read fifteen to twenty additional slates of post-game outcome data, check whether sleeper-favored lineups continued to land in the winning population at the historical rate, adjust the bucket boundaries or the leverage curve in `constants.py`, and redeploy. There is no automated calibration script. The same discipline applies to the new constants as to every other constant in the file.

A separate validation should be performed against the lineup-shape distribution. The historical winning shapes are 41.2 percent zero-pitcher, 24.5 percent one-pitcher, 15.7 percent two-pitcher, and so on through five-pitcher. The optimizer's output shape distribution should remain in the same ballpark after the leverage change. If the shape distribution shifts dramatically, that is a sign that the leverage factor is too aggressive and is overriding the env signal that drives composition. The diagnostics dashboard should expose the shape distribution as a rolling thirty-day metric.

---

## Section 9. Implementation Roadmap

The recommended sequence is as follows, in priority order. Each step is independently testable and independently shippable.

The first deliverable is the static lookup tables: `TEAM_MARKET_TIER` and `STAR_PLAYER_FLAGS` in `constants.py`. These are pure data, populated from publicly available references (team market size studies, prior-season All-Star rosters, MVP voting top-five). Because they are static, they can be reviewed and committed without any code path being touched. Estimated effort: half a day, mostly research and data entry.

The second deliverable is `app/core/popularity.py` and its unit tests. The tests should cover the corner cases: a debut rookie with no prior MP history, a star with high MP frequency, a midmarket veteran with intermittent MP appearances, and any player from a tier-1 market. Estimated effort: one day.

The third deliverable is the `FilteredCandidate` field, the `_compute_base_ev` modification, the `LEVERAGE_FACTORS` constant, and the candidate resolver wiring. With the popularity model already tested, this set is mechanical and small. The integration test that pins contrarian behavior into the suite is in this batch. Estimated effort: half a day.

The fourth deliverable is the offline validation against the historical corpus. The acceptance criterion is the Spearman correlation threshold above. If the predictor fails, the team-market and star-player tables get adjusted before deployment. Estimated effort: half a day, plus iteration time.

The fifth deliverable is the shadow week, during which the leverage factor is computed and logged but not applied. This requires a feature flag in `constants.py` (`LEVERAGE_ENABLED = False`) gating the leverage term in `_compute_base_ev`. Estimated effort: a few minutes for the flag, plus a week of passive observation.

The sixth deliverable is the live flip, after which the standard calibration loop applies. There is no separate engineering work in this step beyond changing the flag.

Total engineering effort to implement: roughly three days of focused work, plus a one-week shadow window, plus the standing calibration cadence. This is small relative to the audit work that has already gone into the existing pipeline.

---

## Section 10. Closing

The honest summary is that the current pipeline is a high-quality solution to a slightly off-target problem. It predicts who will score well, with reasonable accuracy. It does not, as currently constituted, identify the specific configurations of "scores well **and** the field has not noticed" that the historical corpus shows are necessary to win. The fix is not to abandon the existing architecture, which is among the cleanest rule-based DFS pipelines I have audited. The fix is to add one more multiplicative term to the EV formula, sourced from a deterministic popularity predictor that consumes only publicly observable pre-game signals, and to recalibrate the EV ranking around the resulting leverage estimate. The change preserves every existing constraint of the architecture, including the rule against historical outcome bleed, the rule against in-draft signals (`card_boost`, `drafts`) entering the EV path, and the rule against any attempt to reverse-engineer the proprietary scoring formula.

The empirical case is strong. Ninety-two percent of historical winners contain at least one sleeper HV. Eighty-eight percent contain at least one player drafted by under five hundred entrants. The popularity signal is highly autocorrelated and therefore predictable from the same kind of public observables the rest of the pipeline already uses. The proposed change is roughly three days of work plus a shadow week. It produces a model whose top output is, in the language of tournament theory, not the lineup with the highest mean projection but the lineup with the highest probability of finishing first, which is the only metric the contest actually pays out on.

That is how Ben Oracle wins.

---

## Addendum: Historical-corpus storage migration (2026-05-08)

This audit refers throughout to "the historical corpus" — the 43-slate calibration dataset spanning 2026-03-25 → 2026-05-07.  At the time the audit was written the corpus lived in 5 CSV/JSON files in `/data/`.  As of 2026-05-08, the canonical store has been migrated to `data/historical.db` (SQLite, 5 tables: `slate / slate_game / player_slate / player_game_log / label_event`) per CLAUDE.md "Data Files (`/data/`)".  The 5 CSV/JSON files remain on disk as byte-stable derived exports refreshed by every writer; the audit's empirical claims (forty-slate counts, win-share percentages, popularity autocorrelation, etc.) are unchanged because the underlying data is identical — only the storage layer moved.

The popularity predictor proposed in Section 9 (`app/core/popularity.py`) was implemented in May 2026 and is now the single live-runtime read of the historical corpus, querying `data/historical.db` directly via `app.core.historical_db`.  See `scripts/verify_popularity_parity.py` for the parity gate that locked in the migration without behaviour change.
