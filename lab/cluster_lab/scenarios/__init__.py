"""Importing a scenario module registers it. `load()` pulls in every one of them."""

from __future__ import annotations

import importlib
import pkgutil


def load() -> None:
    for module in pkgutil.iter_modules(__path__):
        if module.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{module.name}")
