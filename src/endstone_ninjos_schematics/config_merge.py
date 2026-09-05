"""Config migration helpers that preserve administrator values."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import MutableMapping
from typing import Any


def merge_missing(current: MutableMapping[str, Any], defaults: MutableMapping[str, Any]) -> bool:
    """Recursively add missing keys from *defaults* without replacing current values."""

    changed = False
    for key, default_value in defaults.items():
        if key not in current:
            current[key] = deepcopy(default_value)
            changed = True
            continue
        current_value = current[key]
        if isinstance(current_value, MutableMapping) and isinstance(default_value, MutableMapping):
            changed = merge_missing(current_value, default_value) or changed
    return changed
