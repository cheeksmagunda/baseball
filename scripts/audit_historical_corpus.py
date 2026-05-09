"""Post-migration audit of data/historical.db.

Comprehensive health-check of the canonical historical corpus: storage
efficiency, schema integrity, data coverage, index utilization, query
performance, and reproducibility.  Produces a single human-readable
report at scripts/output/historical_corpus_audit.txt for review.

Run after every significant migration or backfill cycle.

Usage:
    python scripts/audit_historical_corpus.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BO_CURRENT_SEASON", "2026")
os.environ.setdefault("BO_ODDS_API_KEY", "audit-historical-corpus-stub")

from app.core import historical_db  # noqa: E402

REPORT = ROOT / "scripts" / "output" / "historical_corpus_audit.txt"

# Expected slate-date range (corpus carve-out: 41 dates have prior-game logs;
# 2 season-opener dates have empty player_game_log).
EXPECTED_SLATES = 43
EXPECTED_SLATE_GAMES = 551
EXPECTED_PLAYER_SLATES = 1644
EXPECTED_PLAYER_GAME_LOGS = 12290


def _section(title: str) -> str:
    return f"\n{'=' * 78}\n{title}\n{'=' * 78}\n"


def main() -> int:
    lines: list[str] = []

    lines.append(_section("HISTORICAL-CORPUS AUDIT — post-Step-8 baseline"))
    from datetime import datetime, timezone
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"DB path:   {historical_db.DEFAULT_DB_PATH}")

    db_size = historical_db.DEFAULT_DB_PATH.stat().st_size
    lines.append(f"DB size:   {db_size:,} bytes ({db_size / 1024**2:.2f} MiB)")

    conn = historical_db.connect_readonly()
    try:
        # ---- Section 1: SCHEMA -------------------------------------------------
        lines.append(_section("1. SCHEMA"))
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = [r[0] for r in cur.fetchall()]
        lines.append(f"Tables: {len(tables)}")
        for t in tables:
            cur = conn.execute(f"PRAGMA table_info({t})")
            cols = cur.fetchall()
            lines.append(f"  {t}: {len(cols)} columns")
        cur = conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        idx = cur.fetchall()
        lines.append(f"Indexes: {len(idx)}")
        for name, tbl in idx:
            lines.append(f"  {name} on {tbl}")

        # ---- Section 2: ROW COUNTS --------------------------------------------
        lines.append(_section("2. ROW COUNTS"))
        ok = True
        for tbl, want in (
            ("slate", EXPECTED_SLATES),
            ("slate_game", EXPECTED_SLATE_GAMES),
            ("player_slate", EXPECTED_PLAYER_SLATES),
            ("player_game_log", EXPECTED_PLAYER_GAME_LOGS),
        ):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            mark = "✓" if n == want else "✗"
            if n != want:
                ok = False
            lines.append(f"  {mark} {tbl}: {n} (expected {want})")
        n_le = conn.execute("SELECT COUNT(*) FROM label_event").fetchone()[0]
        lines.append(f"  label_event: {n_le}")
        n_pa = conn.execute("SELECT COUNT(*) FROM player_alias").fetchone()[0]
        lines.append(f"  player_alias: {n_pa}")
        lines.append(f"Status: {'PASS' if ok else 'FAIL'}")

        # ---- Section 3: LABEL DISTRIBUTION ------------------------------------
        lines.append(_section("3. LABEL EVENT VOCABULARY COVERAGE"))
        cur = conn.execute(
            "SELECT label_type, COUNT(*) FROM label_event "
            "GROUP BY label_type ORDER BY label_type"
        )
        for ltype, count in cur.fetchall():
            lines.append(f"  {ltype:<22} {count:>6}")
        expected = (
            historical_db.LABEL_TYPES_NUMERIC
            + historical_db.LABEL_TYPES_FLAG
            + historical_db.LABEL_TYPES_CATEGORICAL
        )
        actual = {r[0] for r in conn.execute(
            "SELECT DISTINCT label_type FROM label_event").fetchall()}
        # total_mult is encoded inside winning_lineup_slot label_text, not standalone
        missing = (set(expected) - actual) - {"total_mult"}
        if missing:
            lines.append(f"  MISSING: {missing}")
        else:
            lines.append("  All declared label_types are populated.")

        # ---- Section 4: DATA COVERAGE PER SLATE -------------------------------
        lines.append(_section("4. PER-SLATE COVERAGE"))
        cur = conn.execute(
            """
            SELECT s.slate_date,
                   (SELECT COUNT(*) FROM slate_game WHERE slate_date=s.slate_date) AS games,
                   (SELECT COUNT(*) FROM player_slate WHERE slate_date=s.slate_date) AS players,
                   (SELECT COUNT(*) FROM player_game_log WHERE slate_date=s.slate_date) AS logs,
                   (SELECT COUNT(DISTINCT label_type)
                      FROM label_event WHERE slate_date=s.slate_date) AS label_types
            FROM slate s
            ORDER BY s.slate_date
            """
        )
        rows = cur.fetchall()
        lines.append(f"  {'date':<12} {'games':>6} {'players':>8} {'logs':>6} {'label_types':>12}")
        for r in rows:
            lines.append(f"  {r['slate_date']:<12} {r['games']:>6} "
                         f"{r['players']:>8} {r['logs']:>6} {r['label_types']:>12}")
        # Identify low-coverage slates
        low_cov = [r for r in rows if r["players"] < 25 or r["games"] == 0]
        if low_cov:
            lines.append("\n  LOW-COVERAGE SLATES:")
            for r in low_cov:
                lines.append(f"    {r['slate_date']}: games={r['games']} players={r['players']}")

        # ---- Section 5: NULL AUDIT --------------------------------------------
        lines.append(_section("5. NULL AUDIT (key columns)"))
        null_checks = [
            ("player_slate", "mlb_id"), ("player_slate", "player_name"),
            ("player_slate", "team"), ("player_slate", "position"),
            ("slate_game", "home_team"), ("slate_game", "away_team"),
            ("slate_game", "vegas_total"),
            ("slate_game", "home_starter_era"), ("slate_game", "away_starter_era"),
            ("label_event", "source"), ("label_event", "observed_at"),
        ]
        for tbl, col in null_checks:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE {col} IS NULL"
            ).fetchone()[0]
            tot = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            pct = (100.0 * n / tot) if tot else 0
            lines.append(f"  {tbl}.{col}: {n}/{tot} NULL ({pct:.1f}%)")

        # ---- Section 6: SYNTHETIC ID AUDIT ------------------------------------
        lines.append(_section("6. SYNTHETIC mlb_id AUDIT"))
        cur = conn.execute(
            "SELECT COUNT(*) FROM player_slate WHERE mlb_id < 0"
        )
        synth_player_slate = cur.fetchone()[0]
        lines.append(
            f"  player_slate rows with synthetic (negative) mlb_id: {synth_player_slate}"
        )
        if synth_player_slate:
            cur = conn.execute(
                "SELECT slate_date, mlb_id, player_name, team FROM player_slate "
                "WHERE mlb_id < 0 ORDER BY slate_date"
            )
            for r in cur.fetchall():
                lines.append(
                    f"    {r['slate_date']} | mlb_id={r['mlb_id']} | "
                    f"{r['player_name']} ({r['team']})"
                )

        # ---- Section 7: FK INTEGRITY ------------------------------------------
        lines.append(_section("7. FOREIGN KEY INTEGRITY"))
        cur = conn.execute("PRAGMA foreign_key_check")
        violations = cur.fetchall()
        lines.append(f"  PRAGMA foreign_key_check: {len(violations)} violations")
        if violations:
            for v in violations[:10]:
                lines.append(f"    {tuple(v)}")
        cur = conn.execute("PRAGMA integrity_check")
        integrity = [r[0] for r in cur.fetchall()]
        lines.append(f"  PRAGMA integrity_check: {integrity[0]}")

        # ---- Section 8: QUERY PERFORMANCE -------------------------------------
        lines.append(_section("8. QUERY PERFORMANCE (cold-cache)"))

        def _time(query, params=()):
            t0 = time.time()
            n = conn.execute(query, params).fetchall()
            return (time.time() - t0) * 1000, len(n)

        # Common query: popularity fame index (the live runtime query).
        from datetime import date as _D, timedelta as _td
        as_of = _D(2026, 5, 7)
        cutoff = as_of - _td(days=14)
        ms, n = _time(
            """
            SELECT ps.player_name, ps.team,
                   CASE WHEN mp.mlb_id IS NOT NULL THEN 1 ELSE 0 END AS is_mp
            FROM player_slate ps
            LEFT JOIN (
                SELECT DISTINCT slate_date, mlb_id FROM label_event
                WHERE label_type='most_popular'
                  AND slate_date >= ? AND slate_date < ?
            ) AS mp
              ON mp.slate_date=ps.slate_date AND mp.mlb_id=ps.mlb_id
            WHERE ps.slate_date >= ? AND ps.slate_date < ?
            """,
            (cutoff.isoformat(), as_of.isoformat(),
             cutoff.isoformat(), as_of.isoformat()),
        )
        lines.append(f"  popularity fame-index (14d window): {ms:.2f} ms, {n} rows")

        # Common query: all label_events for one slate
        ms, n = _time(
            "SELECT * FROM label_event WHERE slate_date='2026-05-07'"
        )
        lines.append(f"  label_events for 1 slate:           {ms:.2f} ms, {n} rows")

        # Common query: top players by total_value over corpus
        ms, n = _time(
            """
            SELECT ps.player_name, ps.team, SUM(le.label_value) AS total_tv
            FROM player_slate ps
            JOIN label_event le
              ON le.slate_date=ps.slate_date AND le.mlb_id=ps.mlb_id
            WHERE le.label_type='total_value'
            GROUP BY ps.player_name, ps.team
            ORDER BY total_tv DESC LIMIT 20
            """
        )
        lines.append(f"  top-20 players by total_value:      {ms:.2f} ms, {n} rows")

        # ---- Section 9: REPRODUCIBILITY ---------------------------------------
        lines.append(_section("9. REPRODUCIBILITY GATE"))
        # Build a fresh DB and compare data hashes (excluding observed_at).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp_db = Path(td) / "rebuild.db"
            r = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "build_historical_db.py"),
                 "--db", str(tmp_db), "--rebuild"],
                cwd=str(ROOT), capture_output=True, text=True,
                env={**os.environ, "BO_CURRENT_SEASON": "2026",
                     "BO_ODDS_API_KEY": "audit-stub"},
            )
            if r.returncode != 0:
                lines.append(f"  REBUILD FAILED: {r.stderr[:500]}")
                return 1

            import hashlib
            import sqlite3

            def hash_data(db_path):
                cn = sqlite3.connect(str(db_path))
                h = hashlib.sha256()
                for tbl in ("slate", "slate_game", "player_slate",
                            "player_game_log", "label_event", "player_alias"):
                    cur = cn.execute(f"PRAGMA table_info({tbl})")
                    cols = [c[1] for c in cur.fetchall()
                            if c[1] not in ("observed_at", "saved_at", "rowid_seq")]
                    col_list = ", ".join(cols)
                    cur = cn.execute(
                        f"SELECT {col_list} FROM {tbl} ORDER BY {col_list}"
                    )
                    for row in cur.fetchall():
                        h.update(repr(tuple(row)).encode())
                cn.close()
                return h.hexdigest()

            orig_hash = hash_data(historical_db.DEFAULT_DB_PATH)
            rebuild_hash = hash_data(tmp_db)
            match = "MATCH" if orig_hash == rebuild_hash else "MISMATCH"
            lines.append(f"  data-hash original: {orig_hash}")
            lines.append(f"  data-hash rebuild:  {rebuild_hash}")
            lines.append(f"  status: {match}")
    finally:
        conn.close()

    # ---- Section 10: TEST + AUDIT GATES ---------------------------------------
    lines.append(_section("10. EXTERNAL VERIFICATION GATES"))
    pytest_run = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line"],
        cwd=str(ROOT), capture_output=True, text=True,
        env={**os.environ, "BO_CURRENT_SEASON": "2026",
             "BO_ODDS_API_KEY": "audit-stub"},
    )
    last_line = pytest_run.stdout.strip().splitlines()[-1] if pytest_run.stdout.strip() else "no output"
    lines.append(f"  pytest tests/: {last_line} (exit {pytest_run.returncode})")

    audit_run = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "audit_live_isolation.py")],
        cwd=str(ROOT), capture_output=True, text=True,
        env={**os.environ, "BO_CURRENT_SEASON": "2026",
             "BO_ODDS_API_KEY": "audit-stub"},
    )
    lines.append(f"  audit_live_isolation.py: exit {audit_run.returncode}")

    parity_run = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_popularity_parity.py")],
        cwd=str(ROOT), capture_output=True, text=True,
        env={**os.environ, "BO_CURRENT_SEASON": "2026",
             "BO_ODDS_API_KEY": "audit-stub"},
    )
    last_line = parity_run.stdout.strip().splitlines()[-1] if parity_run.stdout.strip() else "no output"
    lines.append(f"  verify_popularity_parity.py: {last_line} (exit {parity_run.returncode})")

    lines.append(_section("AUDIT COMPLETE"))

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")
    print(f"Report written to {REPORT}")
    print(f"Lines: {len(lines)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
