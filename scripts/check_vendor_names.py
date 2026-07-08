"""CI gate: reject vendor names in shipped artifacts.

Why: OrchX is sold as a vendor-neutral orchestrator and must stay that
way in code, descriptors, tests, and docs. This script enforces that.

How: builds a regex from a static FORBIDDEN list, scans all text files
(excluding the policy / gate scripts themselves and vendor_references/),
exits non-zero with a per-file report on any hit.

The FORBIDDEN list is intentionally maintained in this file (not in
docs/NAMING_GUIDELINES.md) so that the policy doc itself remains safe
to ship to customers, and so that adding a forbidden name is a localized
config-only change.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Canonical, case-insensitive list of substrings that must never appear
# in shipped artifacts. Add entries here only with project-lead approval.
FORBIDDEN: tuple[str, ...] = (
    # East Asian vendor of an integrated ERP product
    "zbintel",
    "zb-",
    # Specifics about that product's file/dir layout
    "sysa/",
    "sysc/",
    "sysn/",
    ".pkgbak",
    # Token strings from that product's own config keys
    "zbservices",
    "zbintel.com",
    # Sample blocked vendor (replace / extend with real entries as needed)
    "samplevendor",
)

# Files in which it is acceptable to see these tokens: the policy itself
# and the gate that lists them.
ALLOWLIST_FILES: set[str] = {
    "scripts/check_vendor_names.py",
    "docs/NAMING_GUIDELINES.md",
}

# Directories to skip entirely.
SKIP_DIRS: set[str] = {
    ".git", ".venv", "venv", "build", "dist", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
    "vendor_references", "node_modules",
}


def should_scan(path: Path) -> bool:
    rel = path.as_posix()
    if rel in ALLOWLIST_FILES:
        return False
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    # Only text-ish files; skip binaries and large assets.
    if path.suffix.lower() in {
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".tar",
        ".gz", ".exe", ".dll", ".so", ".bin", ".woff", ".woff2", ".ttf",
        ".eot", ".mp4", ".mp3", ".wav",
    }:
        return False
    return True


def main(root: Path = Path(".")) -> int:
    pattern = re.compile(
        "|".join(re.escape(t) for t in FORBIDDEN),
        re.IGNORECASE,
    )

    hits: list[tuple[Path, int, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or not should_scan(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append((path, lineno, line.strip()[:200]))

    if not hits:
        print("check_vendor_names: OK — no forbidden tokens found.")
        return 0

    print("check_vendor_names: FAIL", file=sys.stderr)
    for path, lineno, line in hits:
        print(f"  {path}:{lineno}: {line}", file=sys.stderr)
    print(
        f"\n{len(hits)} hit(s). See docs/NAMING_GUIDELINES.md for policy.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
