# Highest Value Player Trait Analysis
## Pre-Draft Filtering Strategy — Based on March 25-31, 2026 Data

> **V2.4 Update (April 9):** The expanded 15-date dataset (March 25 – April 8) confirms and
> sharpens the findings below. The single most predictive signal is **draft tier × boost**:
> - mega-ghost (<50 drafts) + boost ≥ 2.0 → **82% TV>15 rate**, avg TV 19.9
> - ghost (50–99 drafts) + boost ≥ 2.0 → **100% TV>15 rate**, avg TV 20.7
> - medium (200–499 drafts) + boost ≥ 2.0 → 0% TV>15 rate, avg TV 2.5
>
> Trait scores (power_profile, k_rate, etc.) remain useful for ranking within a tier, but
> the tier selection dominates. A mega-ghost with average traits beats a chalk player with
> elite traits when both have the same boost. The optimizer (V2.4) encodes this via an
> env-independent EV floor at score=30 and unconstrained mega-ghost synergy bonus (1.50×).

### Dataset
- 98 Highest Value player appearances across 7 slate dates
- 16 pitcher appearances, 82 batter appearances
- All game stats researched from actual box scores

---

## KEY FINDING: What Highest Value Players Did In Their Games

### Pitchers (16 appearances)

| Trait | Count | % |
|-------|-------|---|
| Quality Start (5+ IP, ≤3 ER) | 14/16 | 87.5% |
| ≤1 ER allowed | 13/16 | 81.3% |
| 0 ER allowed | 11/16 | 68.8% |
| 5+ strikeouts | 15/16 | 93.8% |
| 6+ strikeouts | 13/16 | 81.3% |
| Won the game (W decision) | 11/16 | 68.8% |
| 5+ IP | 15/16 | 93.8% |

**Typical HV Pitcher Line:** 6 IP, 2-3 H, 0-1 ER, 6-8 K

**Pitcher RS Breakdown:**
- RS 5.0+: All threw 5+ IP with 0 ER and 5+ K (dominant aces)
- RS 4.0-4.9: Quality starts with maybe 1 ER but high K rates
- RS 3.7 (Webb 3/31): 6 IP, 3 ER, 5 K — worst HV pitcher line but had +3.0x boost
- RS 0.3 (Webb 3/25): 5 IP, 6 ER — only appeared on HV due to single-game slate

**Exceptions:** Webb on 3/25 (RS 0.3) and relievers Bird/Doval/Brubaker (RS 0.8-1.8) only appeared because March 25 was a single-game slate with limited player pool.

### Batters (82 appearances)

| Trait | Count | % |
|-------|-------|---|
| At least 1 RBI | 76/82 | 92.7% |
| At least 2 RBI | 68/82 | 82.9% |
| At least 1 HR | 58/82 | 70.7% |
| At least 2 hits | 56/82 | 68.3% |
| Multi-HR game | 12/82 | 14.6% |
| 3+ RBI | 41/82 | 50.0% |
| At least 1 extra-base hit | 70/82 | 85.4% |

**Typical HV Batter Line:** 2+ hits, 1 HR, 2-3 RBI

---

## PRE-DRAFT FILTERING CHECKLIST

### Tier 1: Must-Draft (if available with any boost)

**Ace Starting Pitchers**
- [ ] Is the player a top-of-rotation starter (team's #1 or #2)?
- [ ] Career K/9 > 8.5?
- [ ] Facing a bottom-half offense (team OPS < .700)?
- [ ] Has pitched 5+ IP in most recent starts?
- Why: 81% of HV pitchers allowed ≤1 ER. Aces with high K rates dominate.
- Examples: Skubal, Sanchez, Crochet, Gausman, Schlittler, Sale, Alcantara

**Power Bats with High Boost (+2.5x or higher)**
- [ ] Player has 20+ HR power profile?
- [ ] Card boost is +2.5x or higher?
- [ ] Player is in the lineup (not bench)?
- Why: 71% of HV batters hit HRs. A +3.0x boost turns even RS 2.5 into total_value 12.5.
- Examples: Stanton, Bregman, O'Neill, Montgomery, McCutchen

### Tier 2: Strong Targets

**Contact + Power Hitters**
- [ ] Player bats in the 2-5 hole (RBI opportunities)?
- [ ] Has shown recent multi-hit games?
- [ ] Plays in a hitter-friendly park or favorable matchup?
- [ ] Has speed component (SB potential adds to RS)?
- Why: 68% of HV batters had 2+ hits. Multi-dimensional production = higher RS.
- Examples: Altuve (4H/2HR), Yandy Diaz (5H/4RBI), Adames (4H/1HR), Walker (3H/1HR)

**Young Breakout Candidates**
- [ ] Top prospect making debut or early-career?
- [ ] Generating buzz / high expectations?
- [ ] Low draft ownership (anti-popularity edge)?
- Why: DeLauter (2HR debut), McGonigle (4H debut), Wetherholt (walk-off), Schlittler (8K debut) all crushed it.
- Examples: DeLauter, McGonigle, Schlittler, Wetherholt, Caissie

### Tier 3: Boost-Dependent Value

**Moderate Performers with Max Boost (+3.0x)**
- [ ] Card boost = +3.0x?
- [ ] Player is a regular starter (not a bench bat)?
- [ ] Reasonable matchup (not facing an ace)?
- Why: +3.0x boost means RS 2.0 becomes total_value = 2.0 * (2 + 3.0) = 10.0. Even average production yields high value.
- Examples: Garrett Mitchell (RS 3.6, +3.0x = 18.0), Jake Meyers (RS 3.0, +3.0x = 15.0)

---

## THE MATH OF BOOSTS

Card boost is the great equalizer. Consider these real examples:

| Player | RS | Boost | total_value | Notes |
|--------|-----|-------|-------------|-------|
| Cristopher Sanchez | 6.8 | 0.0 | 13.6 | Dominant ace, no boost |
| Colson Montgomery | 6.3 | 3.0 | 31.5 | Grand slam + max boost |
| Garrett Mitchell | 3.6 | 3.0 | 18.0 | Modest stats + max boost |
| Randy Arozarena | 1.6 | 3.0 | 8.0 | Walked twice, barely hit + max boost |
| Max Fried | 6.0 | 0.0 | 12.0 | Ace performance, no boost |
| Ezequiel Tovar (3/28) | 2.9 | 3.0 | 14.5 | 1 HR + max boost |

**Key insight:** A player with RS 3.0 and +3.0x boost (total_value = 15.0) outscores a player with RS 6.0 and no boost (total_value = 12.0). The boost is MORE important than the base RS for total_value.

---

## ANTI-PATTERNS: What NOT to Draft

1. **Low-floor pitchers** — Pitchers who give up runs (Webb 3/25: RS 0.3) can tank your lineup
2. **High-ownership with no boost** — Popular picks with 0 boost need RS 5.0+ to compete with boosted players
3. **Bench/platoon players** — Risk of limited AB = limited RS opportunity
4. **Relievers without boost** — Short appearances cap RS (Bird RS 0.8, Doval RS 1.1)

---

## POSITION BREAKDOWN OF HV BATTERS

| Position | Count | Avg RS | Notable |
|----------|-------|--------|---------|
| SS | 20 | 3.9 | Most represented; Cruz, Adames, Tovar, Montgomery all stars |
| OF | 30 | 3.5 | Largest group; HR power is key differentiator |
| 3B | 8 | 3.8 | Bregman, Suarez, Vargas all high-RS with HR |
| 2B | 7 | 4.3 | Highest avg RS; Altuve, Gimenez, Wetherholt, DeLauter |
| 1B | 7 | 3.3 | Diaz (7.5 RS) is outlier; most moderate |
| C | 5 | 4.4 | Small sample but Langeliers (5.4, 5.7) + Jansen (5.1) excellent |
| DH | 8 | 3.6 | Stanton, Ohtani, Alvarez — established sluggers |

**Insight:** Catchers and 2B had the highest average RS among HV batters. Shortstops were the most frequently represented position.

---

## GAME CONTEXT PATTERNS

1. **Winning team players dominate but losing team players appear too**
   - ~65% of HV batters were on the winning team
   - But players like Abreu (2 appearances on losing teams), Henderson, and Neto made HV despite losses
   - Individual performance matters more than team outcome

2. **High-scoring games produce more HV batters**
   - Games with 8+ total runs had more HV representatives
   - Blowouts (COL 14-5, CWS 9-4) produced multiple HV players from same team

3. **Walk-off / clutch moments correlate with high RS**
   - Robert (walk-off 3-run HR, RS 5.5), Wetherholt (walk-off single, RS 4.5), Caissie (walk-off HR, RS 4.6)
   - Clutch hits in high-leverage spots seem to yield RS bonuses

4. **Opening Day / debut performances**
   - 5 players made their MLB debut during this period and appeared on HV
   - Fresh legs + adrenaline + weak book on them = advantage

---

## SUMMARY: THE PRE-DRAFT FILTER

Before drafting, ask these questions about each available player:

1. **Is the boost +2.5x or higher?** → Draft bias toward YES (boost multiplies everything)
2. **Is this a starting pitcher who is an ace?** → Draft if K-rate is high and matchup is favorable
3. **Is this a power hitter (20+ HR pace)?** → Draft, especially with boost
4. **Does this player bat 2nd-5th in the lineup?** → More RBI opportunities = higher RS
5. **Is this a hot young player / prospect?** → Debut/breakout energy correlates with HV appearances
6. **Is this a catcher or middle infielder?** → These positions had highest avg RS on HV list
7. **Is the game expected to be high-scoring?** → More runs = more RS opportunities

**The winning formula remains:** Get all 5 players above RS 1.0, with 2+ above RS 3.0. Target boosted power bats and dominant aces.
