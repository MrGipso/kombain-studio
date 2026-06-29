import importlib
import pkgutil
from pathlib import Path
from typing import Any


class Registry:
    def __init__(self):
        self._generators: dict[str, type] = {}
        self._analyzers: dict[str, type] = {}
        self._transcribers: dict[str, type] = {}
        self._remotion_templates: dict[str, type] = {}

    def register_generator(self, name: str, cls: type) -> None:
        self._generators[name] = cls

    def register_analyzer(self, name: str, cls: type) -> None:
        self._analyzers[name] = cls

    def register_transcriber(self, name: str, cls: type) -> None:
        self._transcribers[name] = cls

    def register_remotion_template(self, name: str, cls: type) -> None:
        self._remotion_templates[name] = cls

    def get_generator(self, name: str):
        return self._generators.get(name)

    def get_analyzer(self, name: str):
        return self._analyzers.get(name)

    def get_transcriber(self, name: str):
        return self._transcribers.get(name)

    def get_remotion_template(self, name: str):
        return self._remotion_templates.get(name)

    @property
    def generators(self) -> dict[str, type]:
        return dict(self._generators)

    @property
    def analyzers(self) -> dict[str, type]:
        return dict(self._analyzers)

    @property
    def transcribers(self) -> dict[str, type]:
        return dict(self._transcribers)

    @property
    def remotion_templates(self) -> dict[str, type]:
        return dict(self._remotion_templates)


# ─── Global registry ────────────────────────────────────────────────

_GLOBAL_REGISTRY = Registry()


def get_registry() -> Registry:
    return _GLOBAL_REGISTRY


# ─── Plugin discovery ────────────────────────────────────────────────

PLUGIN_TYPES = {
    "generators": ("register_generator", "GeneratorPlugin"),
    "analyzers": ("register_analyzer", "AnalyzerPlugin"),
    "transcribers": ("register_transcriber", "TranscriberPlugin"),
}

_PLUGIN_BASE_PKG = "plugins"


def _iter_plugin_modules(package_name: str):
    """Yield (module_name, module_path) for all .py files in a plugin package."""
    try:
        package = importlib.import_module(package_name)
    except ImportError:
        return

    package_path = getattr(package, "__path__", None)
    if not package_path:
        return

    for finder, name, is_pkg in pkgutil.iter_modules(package_path):
        if not name.startswith("_"):
            full_name = f"{package_name}.{name}"
            yield full_name, name


def discover_and_register():
    """Scan plugins/ subpackages and register all plugin classes."""
    registry = get_registry()

    for subpkg, (register_method, base_class_name) in PLUGIN_TYPES.items():
        full_pkg = f"{_PLUGIN_BASE_PKG}.{subpkg}"
        for module_name, short_name in _iter_plugin_modules(full_pkg):
            try:
                mod = importlib.import_module(module_name)
            except Exception as exc:
                print(f"  [registry] Failed to import {module_name}: {exc}")
                continue

            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and attr.__module__ == mod.__name__
                    and hasattr(attr, "name")
                    and attr.name != "base"
                ):
                    getattr(registry, register_method)(attr.name, attr)

    return registry
