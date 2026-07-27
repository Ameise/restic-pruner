#!/usr/bin/env python3
"""Fail if the image's pinned requirements drift out of pyproject's ranges.

The add-on image installs from ``restic_pruner/requirements.txt`` (pinned, so a
rebuild is reproducible) while development installs from ``pyproject.toml``
(ranged). Nothing stops those two from describing different dependency sets, so
CI checks that every runtime dependency appears in both and that the pin
satisfies the declared minimum.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS = ROOT / "restic_pruner" / "requirements.txt"

SPEC_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)\s*(?P<op>[<>=!~]+)?\s*(?P<version>[\w.]+)?")


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-")


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _parse_pyproject() -> dict[str, str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for spec in data["project"]["dependencies"]:
        match = SPEC_RE.match(spec.strip())
        if not match:
            raise SystemExit(f"cannot parse dependency {spec!r}")
        result[_normalise(match["name"])] = match["version"] or "0"
    return result


def _parse_requirements() -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "==" not in line:
            raise SystemExit(f"{REQUIREMENTS.name}: {line!r} is not pinned with ==")
        name, _, version = line.partition("==")
        result[_normalise(name)] = version.strip()
    return result


def main() -> int:
    declared = _parse_pyproject()
    pinned = _parse_requirements()
    problems: list[str] = []

    for name, minimum in declared.items():
        if name not in pinned:
            problems.append(f"{name} is in pyproject.toml but not in requirements.txt")
        elif _version_tuple(pinned[name]) < _version_tuple(minimum):
            problems.append(
                f"{name} is pinned to {pinned[name]} but pyproject.toml requires >= {minimum}"
            )
    for name in pinned:
        if name not in declared:
            problems.append(f"{name} is pinned in requirements.txt but not declared in pyproject")

    for problem in problems:
        print(f"error: {problem}")  # noqa: T201
    if problems:
        return 1
    print(f"ok: {len(pinned)} runtime dependencies agree")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
