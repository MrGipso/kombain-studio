import json
from pathlib import Path
from typing import Any


class Locale:
    def __init__(self, data: dict[str, Any], lang: str = "ru"):
        self._data = data
        self.lang = lang

    def get(self, key: str, default: str = "") -> str:
        parts = key.split(".")
        obj = self._data
        for part in parts:
            if isinstance(obj, dict):
                obj = obj.get(part)
            else:
                return default
            if obj is None:
                return default
        return str(obj) if not isinstance(obj, (dict, list)) else default

    def get_dict(self, key: str) -> dict:
        parts = key.split(".")
        obj = self._data
        for part in parts:
            if isinstance(obj, dict):
                obj = obj.get(part)
            else:
                return {}
            if obj is None:
                return {}
        return obj if isinstance(obj, dict) else {}

    def format(self, key: str, **kwargs) -> str:
        template = self.get(key)
        if not template:
            return ""
        try:
            return template.format(**kwargs)
        except KeyError:
            return template

    def prompt(self, name: str) -> str:
        return self.get(f"prompts.{name}")

    def ui(self, name: str) -> str:
        return self.get(f"ui.{name}")

    def heuristic(self, section: str) -> list[dict]:
        return self.get_dict(f"heuristics.{section}")


_DEFAULT_LOCALE: Locale | None = None


def load_locale(preset_path: Path, lang: str = "ru") -> Locale:
    locale_file = preset_path / "locale" / f"{lang}.json"
    if locale_file.exists():
        try:
            data = json.loads(locale_file.read_text(encoding="utf-8"))
            return Locale(data, lang=lang)
        except (json.JSONDecodeError, OSError):
            pass
    return Locale({}, lang=lang)


def set_default_locale(locale: Locale) -> None:
    global _DEFAULT_LOCALE
    _DEFAULT_LOCALE = locale


def t(key: str, default: str = "") -> str:
    if _DEFAULT_LOCALE is not None:
        return _DEFAULT_LOCALE.get(key, default)
    return default
