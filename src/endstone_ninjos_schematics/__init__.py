"""Ninj-OS Schematics plugin package."""

__version__ = "1.7.0"

try:
    from .plugin import NinjOSSchematicsPlugin
except ModuleNotFoundError as exc:
    if exc.name != "endstone":
        raise
    NinjOSSchematicsPlugin = None  # Allows pure codec/rotation tests outside Endstone.

__all__ = ["NinjOSSchematicsPlugin", "__version__"]
