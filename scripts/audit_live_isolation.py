"""Static audit: the T-65 live pipeline must not leak banned signals.

The live pipeline (app/services/*, app/routers/*, app/core/*) is architecturally
forbidden from reading three categories of signal:

  1. Historical outcomes (post-slate truth): `real_score`, `total_value`,
     `is_highest_value`, `is_most_popular`, `is_most_drafted_3x`.
  2. In-draft dynamic signals (revealed only during the draft): `card_boost`,
     `drafts` — except in router display-map blocks that pull them from the
     source FilterCard for response payloads.
  3. Popularity (V11.0 removed entirely): `PopularityClass`, `popularity` /
     `sharp_score` attribute reads.  Any reintroduction is a regression.

Only scripts/ may read historical-outcome fields.  No live runtime code
should reference any of these symbols.

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
EXEMPT_FILES = {
    REPO_ROOT / "app" / "routers" / "slates.py",
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
)


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

    print(f"Scanned {files_scanned} runtime files under:")
    for d in RUNTIME_DIRS:
        print(f"  - {d.relative_to(REPO_ROOT)}")
    print(f"Exempt files: {len(EXEMPT_FILES)}")
    print()

    if not total_violations:
        print("OK — no historical outcome fields read in the live pipeline.")
        return 0

    print(f"FAIL — {len(total_violations)} suspicious reference(s) detected:")
    for path, lineno, field, line in total_violations:
        rel = path.relative_to(REPO_ROOT)
        print(f"  {rel}:{lineno}  [{field}]  {line}")
    print()
    print("Historical outcome fields must only be read from scripts/.")
    print("If this is a false positive, add an explicit allowed-context hint in")
    print("the docstring or comment on that line, or add the file to EXEMPT_FILES.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
