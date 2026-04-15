# Disaster Recovery Runbook — Baseball DFS Optimizer

**Date:** April 15, 2026  
**Version:** 1.0  
**Rule:** FAIL LOUDLY, NEVER FALLBACK

---

## Core Principle

**If the T-65 pipeline fails at any stage, users see HTTP 503 with a clear error message. There is no graceful degradation, no fallback to stale lineups, no mock data, no neutral defaults.**

Bad data is worse than no data. Operations must restore the system rather than serve corrupted picks.

---

## Failure Scenarios & Recovery

### Scenario 1: Database (SQLite or Postgres) Unavailable at T-65

**Symptom:** T-65 monitor crashes with `sqlalchemy.exc.OperationalError` or `sqlalchemy.exc.DatabaseError`

**Root Cause:**
- Database dyno down or unreachable (network failure, connection timeout)
- Connection pool exhausted (too many concurrent connections)
- Disk space full (Postgres/SQLite)
- Invalid DFS_DATABASE_URL configuration

**Recovery Steps:**
1. **Check Railway dashboard:** Verify database dyno status is "Running"
2. **Verify connectivity:** From app dyno, test `psql -h <host> -U <user> -d <dbname>` (if Postgres)
3. **Restart database dyno:** If hung, restart from Railway dashboard
4. **Check DFS_DATABASE_URL:** Ensure format is correct:
   - SQLite: `sqlite:///db/baseball_dfs.db`
   - Postgres: `postgresql+psycopg2://user:pass@host:5432/dbname`
5. **Restart app dyno:** Lifespan will re-attempt initialization
6. **Verify picks frozen:** Check logs for "Cache FROZEN" message

**If not recoverable before first pitch:**
- Users see HTTP 503 "T-65 lineup not available"
- Operations manually notify users: "System malfunction; please use default lineup"
- No automated recovery; requires human decision on how to proceed

---

### Scenario 2: The Odds API Key Invalid or Quota Exhausted

**Symptom:** T-65 monitor crashes with `RuntimeError: "The Odds API: invalid API key (401)"` or `RuntimeError: "The Odds API: quota exhausted (422)"`

**Root Cause:**
- `DFS_ODDS_API_KEY` environment variable is unset
- API key is incorrect or revoked
- Monthly quota (500 free-tier requests) has been exhausted
- API endpoint is down (rare)

**Recovery Steps:**
1. **Check environment variable:** Verify `DFS_ODDS_API_KEY` is set in Railway Config Vars
   ```bash
   echo $DFS_ODDS_API_KEY  # In app dyno terminal
   ```
2. **Verify API key validity:** Log in to The Odds API dashboard (https://the-odds-api.com)
   - Check "API Key" page for active key
   - Check "Usage" page for remaining quota
3. **If key is wrong:** Update `DFS_ODDS_API_KEY` in Railway Config Vars
4. **If quota exhausted:** 
   - No immediate fix (API tier is monthly; quota resets next month)
   - Contact Real Sports platform if additional quota needed
5. **If API is down:** Check https://api.the-odds-api.com/status (rare)
6. **Restart app dyno:** After fixing, restart to trigger new T-65 pipeline run

**If not recoverable before first pitch:**
- Users see HTTP 503 "Vegas API failed"
- No lineups available (Vegas lines feed pitcher and batter env scoring; critical signal)

---

### Scenario 3: Redis Unavailable at Runtime

**Symptom:** App crashes with RuntimeError "Redis configured but unreachable"

**Root Cause:**
- Redis dyno down or unreachable
- Connection timeout or network partition
- Redis memory limit exceeded

**Recovery Behavior (FAIL LOUDLY):**
- If `DFS_REDIS_URL` is set, Redis is REQUIRED
- App fails loudly at startup with clear error message
- **No fallback to SQLite** — performance degradation is unacceptable for production
- Users see HTTP 503 "System unavailable — cache layer down"

**Manual Recovery:**
1. **Restart Redis dyno:** From Railway dashboard
2. **Restart app:** App will reconnect and proceed
3. **Verify picks frozen:** Check `/api/filter-strategy/status` returns 200 with picks

**If Redis is not required:** Do NOT set `DFS_REDIS_URL` environment variable. Then SQLite is the sole cache tier (slower but acceptable for single-dyno deployments).

**Impact:** System is down until Redis is restored. No silent degradation. Clear operational visibility.

---

### Scenario 4: MLB API Unavailable at T-65

**Symptom:** T-65 monitor crashes with `httpx.HTTPError` or timeout (after 15s)

**Root Cause:**
- MLB API (stats.mlb.com) is down or rate-limited
- Network connectivity issue between app and MLB
- Rate limiting (app exceeded request quota)

**Recovery Steps:**
1. **Check MLB API status:** https://statsapi.mlb.com/api/v1/schedule?date=2026-04-15 (or today's date)
   - If endpoint is unreachable, MLB API is likely down
2. **Verify network:** From app dyno, test `curl -s https://statsapi.mlb.com/ | head -c 100`
3. **Check rate limits:** MLB API has no public rate limits but is strict; our concurrency limit is 20 (line 70 of `pipeline.py`)
4. **Wait and retry:** MLB API outages are typically < 30 minutes
5. **Manually restart T-65:** Kill monitor task and restart app

**If not recoverable before first pitch:**
- Users see HTTP 503 "MLB Stats API failed"
- No rosters, no player stats, no optimization possible
- This is a hard blocker (no fallback to yesterday's roster)

---

### Scenario 5: T-65 Monitor Task Hangs or Delays (Weather Delay)

**Symptom:** Monitor is sleeping in `_sleep_until()` loop; first pitch repeatedly pushed back; lineups not frozen by expected time

**Root Cause:**
- Game start time updated on MLB API (weather delay, rain, etc.)
- Monitor re-calculates lock time and re-sleeps

**Automatic Recovery:**
- Monitor re-sleeps to new T-65 time
- Lock is pushed back by the same delay as the game
- Picks freeze 65 minutes before the new first pitch
- **No manual action needed** unless delay exceeds ~2 hours

**Manual Override (if needed):**
1. **Kill the monitor task:** Stop the T-65 monitor from sleeping
2. **Manually trigger final run:**
   ```python
   from app.database import SessionLocal
   from app.services.pipeline import run_full_pipeline
   from app.routers.filter_strategy import build_and_cache_lineups
   db = SessionLocal()
   await run_full_pipeline(db, date.today())
   cached = await build_and_cache_lineups(db, slate_date=date.today())
   ```
3. **Freeze cache:** `lineup_cache.freeze(first_pitch_utc=<new_time>)`
4. **Restart monitor:** Restart app to begin post-lock monitoring

**Impact:** Minor—users see HTTP 425 with updated countdown until picks unlock. Picks unlock at T-60 regardless of delay.

---

### Scenario 6: App Crashes During T-65 Pipeline (DB Write Fails, Etc.)

**Symptom:** App dyno crashes with exception during T-65 run; lifespan exits; app restarts

**Root Cause:**
- Exception raised during `run_full_pipeline()`
- Example: SQL constraint violation, out-of-memory, network timeout, MLB API failure, Odds API failure

**Recovery Behavior (REGENERATE FROM SCRATCH OR CRASH):**
1. App restarts (Railway auto-restarts)
2. Lifespan checks: has T-65 already passed?
3. **If T-65 has NOT passed:** Purge cache and wait for startup pipeline to run (normal)
4. **If T-65 HAS passed:** 
   - **Do NOT restore frozen picks from cache**
   - Instead, immediately attempt a full pipeline run with fresh data
   - If any dependency is unavailable (MLB API down, Odds API down, DB down, etc.), crash loudly with error
   - Users see HTTP 503 "T-65 regeneration failed — dependencies unavailable"

**Why no restoration:** Restoration would serve cached picks (fallback behavior). The fail-loud principle requires either:
- Fresh picks from a complete T-65 pipeline run with live data, OR
- A clear crash with error

**Manual Recovery Steps:**
1. **Check logs:** Find the exception that caused crash
2. **Verify dependencies:** 
   - Check Railway database dyno status
   - Verify MLB API is reachable (statsapi.mlb.com)
   - Verify Odds API is reachable (check DFS_ODDS_API_KEY is set)
   - Verify network connectivity
3. **Fix root cause:** (e.g., add missing column, increase dyno memory, extend timeout)
4. **Restart app:** Railway will auto-restart, or manually trigger restart
5. **Verify picks frozen:** Check `/api/filter-strategy/status` returns 200 with picks. If HTTP 503, read error message and fix remaining dependencies.

**Impact:** System either recovers with fresh T-65 picks or crashes loudly with clear error. No silent degradation, no stale data served to users.

---

### Scenario 7: Redis Cache Corrupted (Invalid JSON)

**Symptom:** App crashes with RuntimeError "Redis cache corrupted — cannot parse lineup data"

**Root Cause:**
- Redis corruption (rare, typically from power loss or manual edit)
- Corrupted JSON in `lineup:<date>` key prevents cache loading

**Recovery Behavior (FAIL LOUDLY):**
- App detects corrupted JSON when loading from Redis
- Raises `RuntimeError` with clear message
- Crashes loudly — no fallback to SQLite
- Users see HTTP 503 "Cache layer corrupted — manual recovery required"

**Manual Recovery:**
1. **Clear corrupted Redis key:**
   ```bash
   redis-cli DEL lineup:2026-04-15  # Replace with today's date
   redis-cli DEL lineup:*           # Or clear all lineup keys if unsure
   ```
2. **Option A (Recommended): Regenerate from fresh pipeline**
   - Restart app dyno
   - If T-65 has not yet passed: startup pipeline will run and generate fresh picks
   - If T-65 has already passed: app will attempt full pipeline regeneration (or crash if dependencies missing)
3. **Option B (Emergency): Restore from SQLite if available**
   - Only if T-65 picks were previously frozen and SQLite backup exists
   - Requires manual verification that SQLite data is valid
   - Contact operations before serving cached picks from SQLite

**Impact:** System crashes and requires explicit manual recovery. Clear visibility into cache corruption — no silent serving of corrupted data.

---

## Monitoring Checklist

### At App Startup (Every Day)
- [ ] Logs show "DFS_DATABASE_URL validated" (database URL format correct)
- [ ] Logs show "Redis connectivity verified" OR "Redis configured but unreachable..." (if Redis configured)
- [ ] Logs show "DFS_ODDS_API_KEY configured — Vegas API enrichment enabled" OR critical warning (Vegas warning is OK if key is set but not shown in logs)
- [ ] Logs show "Startup: frozen picks restored=true/false" (restore guard activated)
- [ ] App is healthy on Railway dashboard

### At T-65 Lock Time (60-70 minutes before first pitch)
- [ ] Logs show "T-65 monitor waiting for startup pipeline to complete"
- [ ] Logs show "T-65 monitor targeting date: 2026-04-XX"
- [ ] Logs show "T-65 FINAL RUN — fetching data, building lineups, freezing cache"
- [ ] Logs show "T-65 pipeline complete"
- [ ] Logs show "Cache FROZEN. First pitch: HH:MM UTC. Picks are locked."

### At T-60 Unlock Time (60 minutes before first pitch)
- [ ] GET /api/filter-strategy/optimize returns HTTP 200 (not 425)
- [ ] Response contains two lineups (Starting 5 + Moonshot)
- [ ] Each lineup has exactly 1 pitcher + 4 batters
- [ ] EV values are non-zero and reasonable (50-80 range typical)

### Post-Lock (During Slate)
- [ ] Logs show "Post-lock monitor active — watching 2026-04-XX for completion"
- [ ] Every 60 seconds, logs show status refresh attempt
- [ ] On game completion: "Slate 2026-04-XX complete (XX games final) — clearing frozen cache"

### Optional: Metrics Endpoint (if implemented)
- [ ] GET /api/metrics returns JSON with:
  - `"t65_pipeline_latency_seconds"`: numeric value (typical: 30-120s)
  - `"candidate_pool_size"`: numeric value (typical: 300-500 for 10-game slate)
  - `"cache_hit_rate"`: value 0.0-1.0 (typical: 0.8-0.95 for repeat calls)
  - `"slate_date"`: ISO date string

---

## Testing Recovery (Non-Prod Only)

### Scenario 1: Invalid Database URL
```bash
# In Railway Config Vars, set:
DFS_DATABASE_URL=sqlite:///nonexistent/path/db.db
# Expected: App crashes at startup with "Cannot create database directory"
# Fix: Restore correct path
```

### Scenario 2: Missing Odds API Key
```bash
# In Railway Config Vars, unset DFS_ODDS_API_KEY:
# Expected: App starts with critical log, T-65 pipeline crashes with RuntimeError
# Fix: Set DFS_ODDS_API_KEY to valid key
```

### Scenario 3: Redis Unavailable
```bash
# Stop Redis dyno on Railway
# Expected: App starts with warning "Redis configured but unreachable at startup"
# Expected: POST /api/filter-strategy/optimize still returns picks (SQLite fallback)
# Fix: Restart Redis dyno
```

### Scenario 4: Simulate T-65 Monitor Failure (Graceful Restart)
```bash
# Kill app dyno during T-65 run (before "Cache FROZEN" log)
# Expected: App restarts, logs show "Startup during live slate — restored frozen picks"
# Expected: Picks are unchanged (restoration instead of regeneration)
# Verify: Call /api/filter-strategy/optimize, same picks returned
```

---

## Escalation Contacts

- **MLB API Down:** No contact required; wait for recovery (check status online)
- **The Odds API Key Issue:** Update in Railway Config Vars immediately
- **Database Dyno Problem:** Restart from Railway dashboard; contact Railway support if persistent
- **App Logic Crash:** Check logs, identify exception, apply fix, restart
- **User Notification:** If system down > 10 minutes before first pitch, manually notify users via platform

---

## Key Files for Debugging

| File | Purpose |
|------|---------|
| `app/main.py` | Startup validation logic (lines 61-100) |
| `app/services/slate_monitor.py` | T-65 timing + recovery logic (lines 230-390) |
| `app/services/lineup_cache.py` | Cache persistence + restore logic (lines 157-182) |
| `app/services/pipeline.py` | Pipeline orchestration (lines 478-514) |
| `app/core/odds_api.py` | Vegas API client (lines 66-159) |
| `CLAUDE.md` | System rules + no-fallback philosophy |

---

## Related Documentation

- **CLAUDE.md § "Vegas Lines: Required, Never Optional"** — Why Vegas API failure is critical
- **CLAUDE.md § "ABSOLUTE RULE: No Fallbacks. Ever"** — Why system fails loudly
- **CLAUDE.md § "T-65 Sniper Architecture"** — Complete timing model
- **README.md § "T-65 Event-Driven Timing"** — User-facing timing explanation

---

## Version History

| Date | Version | Changes |
|------|---------|---------|
| 2026-04-15 | 1.0 | Initial runbook. Seven scenarios covered. Monitoring checklist. Testing guide. |

