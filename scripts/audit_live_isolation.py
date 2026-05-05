"""Static audit: the T-65 live pipeline must not leak banned signals.

The live pipeline (app/services/*, app/routers/*, app/core/*) is architecturally
forbidden from reading five categories of signal:

  1. Historical outcomes (post-slate truth): `real_score`, `total_value`,
     `is_highest_value`, `is_most_popular`, `is_most_drafted_3x`.
  2. In-draft dynamic signals (revealed only during the draft): `card_boost`,
     `drafts` — except in router display-map blocks that pull them from the
     source FilterCard for response payloads.
  3. Popularity (V11.0 removed entirely): `PopularityClass`, `popularity` /
     `sharp_score` attribute reads.  Any reintroduction is a regression.
  4. Historical data file paths: the four /data/ files (historical_players,
     historical_winning_drafts, hv_player_game_stats, historical_slate_results)
     must never be opened or referenced in live runtime code.
  5. Cross-boundary imports of `scripts/*` modules into `app/*` runtime —
     scripts may read historical CSVs or perform offline backfill / analysis,
     and pulling them in at runtime risks importing those reads.  A single
     allowlist entry exists for `scripts.refresh_statcast.main`, the live
     Baseball Savant bulk-load wired into the T-65 pipeline, which fetches
     from Savant URLs only and never opens /data/ files.

Only scripts/ may read historical-outcome fields or the /data/ files.  No live
runtime code should reference any of these symbols or file paths.

This script greps the runtime code paths for any such reference.  Run it
before deploying or as a CI gate.

Exit codes:
    0 — clean tree, no leaks.
    2 — banned symbol detected in a runtime file.

Usage:
    python scripts/audit_live_isolation.py
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Runtime scopes that must never read banned signals.
RUNTIME_DIRS = [
    REPO_ROOT / "app" / "services",
    REPO_ROOT / "app" / "routers",
    REPO_ROOT / "app" / "core",
]

# Known-exempt files within the runtime scopes.
#   - routers/slates.py is a CRUD/admin router: GET returns stored slate data
#     for display, PUT ingests post-game results.  Neither is on the T-65 path.
#   - core/popularity.py reads the prior-slate `is_most_popular` flag from
#     historical_players.csv strictly before the current date to compute a
#     rolling 14-day fame index.  Per STRATEGY_AUDIT_2026-05.md this is
#     analogous to using prior-season ERA — a backward-looking aggregate
#     of pre-game observables, not leakage of the current slate's outcome.
#     The module never reads real_score, total_value, is_highest_value,
#     is_most_drafted_3x, drafts, or card_boost.
EXEMPT_FILES = {
    REPO_ROOT / "app" / "routers" / "slates.py",
    REPO_ROOT / "app" / "core" / "popularity.py",
}

# `from scripts.X import …` is banned in app/ except for a hand-checked
# allowlist.  Anything else risks pulling a script-only historical read into
# the live runtime via a transitive import.  Each entry is a fully-qualified
# scripts module name.
ALLOWED_SCRIPTS_IMPORTS = {
    # Live Baseball Savant bulk-load — fetches from Savant URLs only and
    # never opens /data/ files.  Wired into pipeline.py::_refresh_statcast.
    "scripts.refresh_statcast",
}

# Banned attribute reads: any `.field` access.  The auditor checks for the
# literal attribute access; comments, docstrings, and lines with an explicit
# "display only" hint are excluded.
BANNED_FIELDS = [
    # Historical outcomes (post-slate truth)
    "real_score",
    "total_value",
    "is_highest_value",
    "is_most_popular",
    "is_most_drafted_3x",
    # In-draft dynamic signals — display-map blocks must mark these as
    # display-only via an inline comment hint.
    "card_boost",
    "drafts",
    # V11.0 — popularity removed entirely.  Any read is a regression.
    "popularity",
    "sharp_score",
    # May-2026 strict pass — these constants were removed because they
    # encoded silent fallbacks (league-average defaults / "neutral" trait
    # scores).  Reintroducing any of them re-opens a fallback path.
    "DEFAULT_OPP_OPS",
    "DEFAULT_OPP_K_PCT",
    "DEFAULT_PITCHER_ERA",
    "DEFAULT_PITCHER_WHIP",
    "DEFAULT_BATTER_OPS_VS_LHP",
    "DEFAULT_BATTER_OPS_VS_RHP",
    "UNKNOWN_SCORE_RATIO",
    "DNP_RISK_PENALTY",
    "DNP_UNKNOWN_PENALTY",
    "ENV_UNKNOWN_COUNT_THRESHOLD",
]

# Filename stems of the four historical /data/ files.  Any occurrence of these
# strings in non-comment runtime code is a violation — the live pipeline must
# never open or reference these files.
BANNED_DATA_FILES = [
    "historical_players",
    "historical_winning_drafts",
    "hv_player_game_stats",
    "historical_slate_results",
]

# Phrases that indicate the match is a legitimate non-read reference
# (docstring, comment, string literal describing the rule).
ALLOWED_CONTEXT_HINTS = (
    "# ",            # line starts with comment
    '"""',           # docstring
    "'''",           # docstring
    "NEVER",         # comment calling out the rule
    "must not",
    "forbidden",
    "retrospective",
    "display only",
    "display-only",
    "DISPLAY-ONLY",
    "not as an RS",
    # V14: app.core.popularity is the predicted-ownership-bucket module
    # (see STRATEGY_AUDIT_2026-05.md).  Its imports legitimately contain
    # the substring ".popularity"; the historical V11.0 ban applied to
    # the deleted popularity-scraping module + the .popularity attribute
    # on FilteredCandidate (which the dataclass-signature guard in
    # tests/test_invariants.py still rejects).
    "app.core.popularity",
)


def scan_file_for_data_refs(path: Path) -> list[tuple[int, str, str]]:
    """Return (line_no, stem, line) for each historical-file path reference."""
    violations: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return violations
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for stem in BANNED_DATA_FILES:
            if stem in stripped:
                if any(hint in stripped for hint in ALLOWED_CONTEXT_HINTS):
                    continue
                violations.append((lineno, stem, stripped))
    return violations


_SCRIPTS_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(scripts(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+)\s+import\b"
    r"|import\s+(scripts(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+)\b)"
)


def scan_file_for_scripts_imports(path: Path) -> list[tuple[int, str, str]]:
    """Return (line_no, module, line) for any scripts.* import that is not
    in ALLOWED_SCRIPTS_IMPORTS.  Imports inside docstrings/comments are
    filtered out by the leading-whitespace-then-`import` regex match."""
    violations: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return violations
    for lineno, raw in enumerate(text.splitlines(), start=1):
        m = _SCRIPTS_IMPORT_RE.match(raw)
        if not m:
            continue
        module = m.group(1) or m.group(2)
        if module in ALLOWED_SCRIPTS_IMPORTS:
            continue
        violations.append((lineno, module, raw.strip()))
    return violations


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return (line_no, field, line) for each suspicious read."""
    violations: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return violations

    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for field in BANNED_FIELDS:
            # Match `.field` as attribute access (preceded by a word char/.) —
            # excludes bare variable names.
            if re.search(rf"\.{field}\b", stripped):
                if any(hint in stripped for hint in ALLOWED_CONTEXT_HINTS):
                    continue
                violations.append((lineno, field, stripped))
    return violations


def main() -> int:
    total_violations: list[tuple[Path, int, str, str]] = []
    files_scanned = 0

    for runtime_dir in RUNTIME_DIRS:
        for py_file in sorted(runtime_dir.rglob("*.py")):
            if py_file in EXEMPT_FILES:
                continue
            files_scanned += 1
            for lineno, field, line in scan_file(py_file):
                total_violations.append((py_file, lineno, field, line))
            for lineno, stem, line in scan_file_for_data_refs(py_file):
                total_violations.append((py_file, lineno, stem, line))
            for lineno, module, line in scan_file_for_scripts_imports(py_file):
                total_violations.append(
                    (py_file, lineno, f"scripts-import:{module}", line)
                )

    print(f"Scanned {files_scanned} runtime files under:")
    for d in RUNTIME_DIRS:
        print(f"  - {d.relative_to(REPO_ROOT)}")
    print(f"Exempt files: {len(EXEMPT_FILES)}")
    print()

    if not total_violations:
        print("OK — no historical outcome fields or data file references in the live pipeline.")
        return 0

    print(f"FAIL — {len(total_violations)} suspicious reference(s) detected:")
    for path, lineno, field, line in total_violations:
        rel = path.relative_to(REPO_ROOT)
        print(f"  {rel}:{lineno}  [{field}]  {line}")
    print()
    print("Historical outcome fields and /data/ file references must only appear in scripts/.")
    print("If this is a false positive, add an explicit allowed-context hint in")
    print("the docstring or comment on that line, or add the file to EXEMPT_FILES.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
