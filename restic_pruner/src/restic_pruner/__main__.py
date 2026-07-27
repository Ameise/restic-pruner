"""Entry point: ``python -m restic_pruner`` / ``restic-pruner``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .app import async_main, configure_logging
from .config import ConfigError, load_settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="restic-pruner",
        description="Scheduled restic forget/prune/check with healthchecks.io reporting.",
    )
    parser.add_argument("--version", action="version", version=f"restic-pruner {__version__}")
    parser.add_argument(
        "--options",
        type=Path,
        default=None,
        help="path to a Home Assistant options.json (defaults to /data/options.json)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate the configuration and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings(args.options)
    except ConfigError as exc:
        configure_logging("info")
        logging.getLogger(__name__).error("Configuration error: %s", exc)
        return 2

    if args.check_config:
        configure_logging(settings.log_level)
        logging.getLogger(__name__).info("Configuration is valid")
        return 0

    try:
        return asyncio.run(async_main(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
