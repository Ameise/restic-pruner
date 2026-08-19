#!/usr/bin/env python3
"""Fail if the version is not the same in all three places that carry it.

``config.yaml`` is what Home Assistant compares to decide an update is
available, ``pyproject.toml`` is what a pip install reports, and
``__init__.__version__`` is what the add-on log, ``/api/health``, ``/api/status``
and the Home Assistant device ``sw_version`` report. Nothing links them.

They drifted once already: the package said 0.1.0 for two releases, so every
diagnostic that quoted a version quoted the wrong one -- the sort of thing that
sends someone chasing a phantom failed update. ``release.yaml`` already refuses a
tag that disagrees with ``config.yaml``; this refuses the same disagreement on
every push, before a tag exists.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {
    "restic_pruner/config.yaml": re.compile(r'^version:\s*"(?P<version>[^"]+)"', re.MULTILINE),
    "pyproject.toml": None,  # parsed as TOML below
    "restic_pruner/src/restic_pruner/__init__.py": re.compile(
        r'^__version__\s*=\s*"(?P<version>[^"]+)"', re.MULTILINE
    ),
    # The page compares this against what the API reports, to notice when a
    # browser is showing it from a cache older than the running add-on.
    "restic_pruner/src/restic_pruner/web/index.html": re.compile(
        r'<meta name="build-version" content="(?P<version>[^"]+)">'
    ),
}


def _read(path: str, pattern: re.Pattern[str] | None) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    if pattern is None:
        return str(tomllib.loads(text)["project"]["version"])
    match = pattern.search(text)
    if match is None:
        raise SystemExit(f"error: no version found in {path}")
    return match["version"]


def main() -> int:
    found = {path: _read(path, pattern) for path, pattern in SOURCES.items()}
    if len(set(found.values())) == 1:
        print(f"ok: version {next(iter(found.values()))} in all {len(found)} places")  # noqa: T201
        return 0

    print("error: the version disagrees between files")  # noqa: T201
    for path, version in found.items():
        print(f"  {version:<12} {path}")  # noqa: T201
    return 1


if __name__ == "__main__":
    sys.exit(main())
