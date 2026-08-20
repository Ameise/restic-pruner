"""Scheduled restic maintenance for Home Assistant and plain Docker."""

#: Kept in step with restic_pruner/config.yaml and pyproject.toml by
#: scripts/check_versions.py, which CI runs on every push.
__version__ = "0.4.3"

__all__ = ["__version__"]
