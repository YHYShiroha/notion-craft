#!/usr/bin/env python3
"""Fail if a credential looks like it leaked into the skill directory.

Reports file paths and match counts only -- never the secret itself -- so the
output is safe to paste into a transcript or a CI log.

Usage:
  python check_no_secrets.py [root ...]        # default: this skill directory

Exit codes: 0 = clean, 1 = suspected leak found, 2 = usage error.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ROOTS = [SKILL_DIR]

# Structural patterns: a Notion secret, a bearer header with a literal token,
# or an obvious assignment of a credential-looking value.
PATTERNS = [
    ("notion-secret", re.compile(r"\b(?:ntn|secret)_[A-Za-z0-9]{20,}\b")),
    ("bearer-literal", re.compile(r"Bearer\s+[A-Za-z0-9_\-]{20,}")),
    ("assigned-secret", re.compile(
        r"(?i)\b(?:token|secret|api[_-]?key|password)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{24,}")),
]

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache"}
SKIP_FILES = {".audit.log"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini",
                 ".sh", ".ps1", ".cmd", ".bat", ".js", ".ts", ""}


def scan_file(path: Path) -> list[tuple[str, int]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for name, pattern in PATTERNS:
        count = len(pattern.findall(text))
        if count:
            found.append((name, count))
    return found


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.name in SKIP_FILES or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            yield path


def main(argv: list[str]) -> int:
    roots = [Path(arg).resolve() for arg in argv[1:]] or DEFAULT_ROOTS
    for root in roots:
        if not root.exists():
            print(f"error: {root} does not exist", file=sys.stderr)
            return 2
    total = 0
    checked = 0
    for root in roots:
        for path in walk(root):
            checked += 1
            hits = scan_file(path)
            if hits:
                total += len(hits)
                summary = ", ".join(f"{name} x{count}" for name, count in hits)
                try:
                    shown = path.relative_to(root)
                except ValueError:
                    shown = path
                print(f"LEAK? {shown}: {summary}")
    print(f"scanned {checked} files under {', '.join(str(r) for r in roots)}")
    if total:
        print(f"FAIL: {total} suspicious match(es). Remove the secret and rotate it.")
        return 1
    print("PASS: no credential patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
