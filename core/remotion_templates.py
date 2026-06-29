"""
Python-side registry mirroring the TypeScript templateRegistry.ts.
New templates added on the TS side should be registered here too.
"""

from typing import Callable

_TEMPLATES: dict[str, dict] = {}

TEMPLATE_INFO: dict[str, dict] = {
    "ChapterTitle": {
        "description": "Chapter/section title card with accent line",
        "keywords": ["глава", "часть", "раздел", "заключение", "введение", "chapter", "section"],
        "props": ["chapter", "title", "subtitle", "accent"],
        "duration_seconds": 6,
    },
    "StatCard": {
        "description": "Large statistic display with glow effect",
        "keywords": ["%", "процент", "миллион", "тысяч", "миллиард", "stat"],
        "props": ["title", "value", "label", "note", "accent"],
        "duration_seconds": 8,
    },
    "FlowDiagram": {
        "description": "Connected node flow diagram",
        "keywords": ["5g", "flow", "схема работы", "процесс", "этап", "pipeline"],
        "props": ["title", "subtitle", "nodes", "accent"],
        "duration_seconds": 10,
    },
    "MapRoute": {
        "description": "Map with animated route line and moving dot",
        "keywords": ["маршрут", "путь", "кабель идет", "проходит", "route"],
        "props": ["title", "subtitle", "route", "zones", "accent"],
        "duration_seconds": 10,
    },
    "MapSplit": {
        "description": "Side-by-side comparison split screen",
        "keywords": ["миф", "реальность", "правда", "vs", "сравнение", "split"],
        "props": ["title", "subtitle", "leftLabel", "rightLabel", "accent"],
        "duration_seconds": 8,
    },
    "RerouteMap": {
        "description": "Network reroute animation with break point",
        "keywords": ["bgp", "обход", "reroute", "авария", "повреждение", "обрыв"],
        "props": ["title", "subtitle", "route", "alternateRoute", "accent"],
        "duration_seconds": 12,
    },
    "TextOverlay": {
        "description": "Simple centered text overlay with fade-in",
        "keywords": ["текст", "надпись", "заголовок", "text", "overlay", "caption"],
        "props": ["title", "subtitle", "accent"],
        "duration_seconds": 5,
    },
}


def register_template(
    name: str,
    description: str = "",
    keywords: list[str] | None = None,
    props: list[str] | None = None,
    duration_seconds: int = 6,
) -> None:
    """Register a new Remotion template from Python."""
    _TEMPLATES[name] = {
        "name": name,
        "description": description,
        "keywords": keywords or [],
        "props": props or [],
        "duration_seconds": duration_seconds,
    }


def get_template(name: str) -> dict | None:
    return TEMPLATE_INFO.get(name) or _TEMPLATES.get(name)


def list_templates() -> list[str]:
    all_names = list(TEMPLATE_INFO.keys()) + [
        k for k in _TEMPLATES.keys() if k not in TEMPLATE_INFO
    ]
    return all_names


def match_template(text: str) -> str | None:
    """Find best matching template by keywords in text.
    Prefers longer/more specific keyword matches."""
    lower = text.lower()
    candidates: list[tuple[int, str]] = []

    all_info = dict(TEMPLATE_INFO)
    all_info.update(_TEMPLATES)

    for name, info in all_info.items():
        for keyword in info.get("keywords", []):
            kw = keyword.lower()
            if kw in lower:
                candidates.append((len(kw), name))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][1]


def suggest_template(text: str, visual_type: str | None = None) -> str:
    """Suggest a template based on text and visual type.
    Uses keyword matching with preference for longer matches."""
    if visual_type == "motion_text":
        return "ChapterTitle"

    matched = match_template(text)
    if matched:
        return matched

    if visual_type == "remotion_graphic":
        return "FlowDiagram"

    return "ChapterTitle"
