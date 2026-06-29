import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


DEFAULT_PALETTE = {
    "background": "#0b1020",
    "panel": "#111827",
    "panel_alt": "#172033",
    "text": "#f8fafc",
    "muted": "#94a3b8",
    "accent": "#38bdf8",
    "accent_2": "#22c55e",
    "warning": "#f97316",
    "danger": "#ef4444",
    "line": "#475569",
}


@dataclass
class Canvas:
    width: int = 1920
    height: int = 1080
    palette: dict | None = None

    def __post_init__(self):
        self.palette = {**DEFAULT_PALETTE, **(self.palette or {})}
        self.items: list[str] = []

    def add(self, markup: str):
        self.items.append(markup)

    def text(self, x, y, text, size=42, fill=None, weight=500, anchor="middle", max_chars=None):
        fill = fill or self.palette["text"]
        lines = wrap_text(str(text), max_chars or max(14, int(44 * 42 / size)))
        line_height = size * 1.22
        start_y = y - (len(lines) - 1) * line_height / 2
        for index, line in enumerate(lines):
            self.add(
                f'<text x="{x}" y="{start_y + index * line_height:.1f}" '
                f'fill="{fill}" font-size="{size}" font-weight="{weight}" '
                f'font-family="Inter, Segoe UI, Arial, sans-serif" text-anchor="{anchor}" '
                f'dominant-baseline="middle">{escape(line)}</text>'
            )

    def rect(self, x, y, width, height, fill=None, stroke=None, radius=18, stroke_width=3, opacity=1):
        attrs = [
            f'x="{x}"',
            f'y="{y}"',
            f'width="{width}"',
            f'height="{height}"',
            f'rx="{radius}"',
            f'fill="{fill or self.palette["panel"]}"',
            f'opacity="{opacity}"',
        ]
        if stroke:
            attrs.extend([f'stroke="{stroke}"', f'stroke-width="{stroke_width}"'])
        self.add(f'<rect {" ".join(attrs)} />')

    def circle(self, cx, cy, r, fill=None, stroke=None, stroke_width=3, opacity=1):
        attrs = [
            f'cx="{cx}"',
            f'cy="{cy}"',
            f'r="{r}"',
            f'fill="{fill or self.palette["panel"]}"',
            f'opacity="{opacity}"',
        ]
        if stroke:
            attrs.extend([f'stroke="{stroke}"', f'stroke-width="{stroke_width}"'])
        self.add(f'<circle {" ".join(attrs)} />')

    def line(self, x1, y1, x2, y2, stroke=None, width=5, dash=None, marker=True, opacity=1):
        marker_attr = ' marker-end="url(#arrow)"' if marker else ""
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{stroke or self.palette["line"]}" stroke-width="{width}" '
            f'stroke-linecap="round" opacity="{opacity}"{dash_attr}{marker_attr} />'
        )

    def polyline(self, points, stroke=None, width=5, dash=None, marker=True, opacity=1):
        point_text = " ".join(f"{x},{y}" for x, y in points)
        marker_attr = ' marker-end="url(#arrow)"' if marker else ""
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(
            f'<polyline points="{point_text}" fill="none" '
            f'stroke="{stroke or self.palette["line"]}" stroke-width="{width}" '
            f'stroke-linecap="round" stroke-linejoin="round" opacity="{opacity}"{dash_attr}{marker_attr} />'
        )

    def title(self, title, subtitle=""):
        if title:
            self.text(self.width / 2, 92, title, size=54, weight=750, max_chars=42)
        if subtitle:
            self.text(self.width / 2, 152, subtitle, size=28, fill=self.palette["muted"], max_chars=80)

    def svg(self):
        bg = self.palette["background"]
        defs = f"""
  <defs>
    <marker id="arrow" markerWidth="14" markerHeight="14" refX="12" refY="7" orient="auto" markerUnits="strokeWidth">
      <path d="M2,2 L12,7 L2,12 Z" fill="{self.palette['accent']}" />
    </marker>
    <filter id="softShadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="12" stdDeviation="16" flood-color="#000000" flood-opacity="0.28"/>
    </filter>
  </defs>
"""
        body = "\n  ".join(self.items)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}">\n'
            f'  <rect width="100%" height="100%" fill="{bg}" />\n'
            f'{defs}\n  {body}\n</svg>\n'
        )


def wrap_text(text: str, max_chars: int) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return [""]
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_schema(spec: dict) -> str:
    canvas = Canvas(
        width=int(spec.get("width", 1920)),
        height=int(spec.get("height", 1080)),
        palette=spec.get("palette"),
    )
    schema_type = spec.get("type", "flow")
    if schema_type == "flow":
        render_flow(canvas, spec)
    elif schema_type == "comparison":
        render_comparison(canvas, spec)
    elif schema_type == "stat_card":
        render_stat_card(canvas, spec)
    elif schema_type == "timeline":
        render_timeline(canvas, spec)
    elif schema_type == "layered_cutaway":
        render_layered_cutaway(canvas, spec)
    elif schema_type == "reroute_network":
        render_reroute_network(canvas, spec)
    else:
        raise ValueError(f"Unsupported schema type: {schema_type}")
    return canvas.svg()


def render_flow(canvas: Canvas, spec: dict):
    canvas.title(spec.get("title", ""), spec.get("subtitle", ""))
    nodes = spec.get("nodes", [])
    if not nodes:
        return
    y = canvas.height * 0.56
    left = 170
    right = canvas.width - 170
    gap = (right - left) / max(1, len(nodes) - 1)
    positions = []
    for index, node in enumerate(nodes):
        x = left + index * gap
        positions.append((x, y))
        color = node.get("color") or canvas.palette["panel"]
        accent = node.get("accent") or canvas.palette["accent"]
        canvas.circle(x, y, 82, fill=color, stroke=accent, stroke_width=4)
        canvas.text(x, y, node.get("label", ""), size=32, weight=700, max_chars=13)
        if node.get("caption"):
            canvas.text(x, y + 128, node["caption"], size=24, fill=canvas.palette["muted"], max_chars=18)
    for index in range(len(positions) - 1):
        x1, y1 = positions[index]
        x2, y2 = positions[index + 1]
        canvas.line(x1 + 92, y1, x2 - 92, y2, stroke=canvas.palette["accent"], width=6)


def render_comparison(canvas: Canvas, spec: dict):
    canvas.title(spec.get("title", ""), spec.get("subtitle", ""))
    items = spec.get("items", [])[:2]
    if len(items) < 2:
        return
    margin = 150
    top = 235
    card_w = (canvas.width - margin * 2 - 60) / 2
    card_h = 650
    for index, item in enumerate(items):
        x = margin + index * (card_w + 60)
        accent = item.get("accent") or (canvas.palette["warning"] if index == 0 else canvas.palette["accent_2"])
        canvas.rect(x, top, card_w, card_h, fill=canvas.palette["panel"], stroke=accent, radius=22)
        canvas.text(x + card_w / 2, top + 72, item.get("label", ""), size=46, weight=760, max_chars=20)
        canvas.text(x + card_w / 2, top + 155, item.get("headline", ""), size=34, fill=accent, weight=700, max_chars=26)
        bullets = item.get("bullets", [])
        for bullet_index, bullet in enumerate(bullets[:6]):
            by = top + 250 + bullet_index * 70
            canvas.circle(x + 70, by, 9, fill=accent)
            canvas.text(x + 105, by, bullet, size=28, fill=canvas.palette["text"], anchor="start", max_chars=34)


def render_stat_card(canvas: Canvas, spec: dict):
    canvas.title(spec.get("title", ""), spec.get("subtitle", ""))
    value = spec.get("value", "95%+")
    label = spec.get("label", "")
    note = spec.get("note", "")
    canvas.rect(285, 280, 1350, 520, fill=canvas.palette["panel"], stroke=canvas.palette["accent"], radius=28)
    canvas.text(canvas.width / 2, 460, value, size=160, fill=canvas.palette["accent"], weight=850, max_chars=8)
    canvas.text(canvas.width / 2, 625, label, size=48, weight=700, max_chars=44)
    canvas.text(canvas.width / 2, 725, note, size=30, fill=canvas.palette["muted"], max_chars=70)


def render_timeline(canvas: Canvas, spec: dict):
    canvas.title(spec.get("title", ""), spec.get("subtitle", ""))
    events = spec.get("events", [])
    if not events:
        return
    left = 180
    right = canvas.width - 180
    y = canvas.height * 0.56
    canvas.line(left, y, right, y, stroke=canvas.palette["line"], width=6, marker=False)
    gap = (right - left) / max(1, len(events) - 1)
    for index, event in enumerate(events):
        x = left + index * gap
        accent = event.get("accent") or canvas.palette["accent"]
        canvas.circle(x, y, 24, fill=accent, stroke=canvas.palette["background"], stroke_width=6)
        label_y = y - 125 if index % 2 == 0 else y + 125
        canvas.line(x, y, x, label_y - (34 if index % 2 == 0 else -34), stroke=accent, width=3, marker=False)
        canvas.text(x, label_y, event.get("date", ""), size=34, fill=accent, weight=800, max_chars=12)
        canvas.text(x, label_y + (55 if index % 2 == 0 else 55), event.get("label", ""), size=25, max_chars=19)


def render_layered_cutaway(canvas: Canvas, spec: dict):
    canvas.title(spec.get("title", ""), spec.get("subtitle", ""))
    layers = spec.get("layers", [])
    cx = canvas.width * 0.36
    cy = canvas.height * 0.58
    max_r = 260
    colors = [canvas.palette["line"], "#64748b", "#94a3b8", "#f59e0b", canvas.palette["accent"], "#e2e8f0"]
    for index, layer in enumerate(layers):
        r = max_r - index * (max_r / max(1, len(layers)))
        if r <= 0:
            continue
        color = layer.get("color") or colors[index % len(colors)]
        canvas.circle(cx, cy, r, fill=color, opacity=0.92, stroke=canvas.palette["background"], stroke_width=4)
    legend_x = canvas.width * 0.63
    legend_y = canvas.height * 0.34
    for index, layer in enumerate(layers):
        y = legend_y + index * 78
        color = layer.get("color") or colors[index % len(colors)]
        canvas.rect(legend_x, y - 24, 46, 46, fill=color, radius=10)
        canvas.text(legend_x + 70, y, layer.get("label", ""), size=31, anchor="start", max_chars=28)
        if layer.get("note"):
            canvas.text(legend_x + 70, y + 34, layer["note"], size=21, fill=canvas.palette["muted"], anchor="start", max_chars=44)


def render_reroute_network(canvas: Canvas, spec: dict):
    canvas.title(spec.get("title", ""), spec.get("subtitle", ""))
    nodes = spec.get("nodes", [])
    if not nodes:
        return
    center_x = canvas.width / 2
    center_y = canvas.height * 0.58
    radius = 310
    positions = {}
    for index, node in enumerate(nodes):
        angle = -math.pi / 2 + index * (2 * math.pi / len(nodes))
        x = center_x + radius * math.cos(angle)
        y = center_y + radius * math.sin(angle)
        positions[node["id"]] = (x, y)
    for edge in spec.get("edges", []):
        start = positions.get(edge[0])
        end = positions.get(edge[1])
        if start and end:
            canvas.line(*start, *end, stroke=canvas.palette["line"], width=4, marker=False, opacity=0.42)
    for route in spec.get("routes", []):
        points = [positions[item] for item in route.get("path", []) if item in positions]
        if len(points) >= 2:
            canvas.polyline(
                points,
                stroke=route.get("color") or canvas.palette["accent"],
                width=8,
                dash=route.get("dash"),
                marker=True,
                opacity=route.get("opacity", 1),
            )
    for node in nodes:
        x, y = positions[node["id"]]
        accent = node.get("accent") or canvas.palette["accent"]
        canvas.circle(x, y, 58, fill=canvas.palette["panel"], stroke=accent, stroke_width=4)
        canvas.text(x, y, node.get("label", node["id"]), size=25, weight=750, max_chars=10)


def sample_specs() -> dict[str, dict]:
    return {
        "5g-to-ocean-cable": {
            "type": "flow",
            "title": "5G не отменяет кабели",
            "subtitle": "Беспроводным остается только последний участок до вышки",
            "nodes": [
                {"label": "Телефон", "caption": "радиосигнал"},
                {"label": "5G вышка", "caption": "базовая станция"},
                {"label": "Оптоволокно", "caption": "городская сеть"},
                {"label": "Дата-центр", "caption": "обработка"},
                {"label": "Океанский кабель", "caption": "межконтинентальный путь"},
            ],
        },
        "satellite-vs-cable": {
            "type": "comparison",
            "title": "Миф против реальности",
            "subtitle": "Почему международный интернет в основном идет под водой",
            "items": [
                {
                    "label": "Спутники",
                    "headline": "Видимая часть",
                    "bullets": ["полезны вне инфраструктуры", "выше задержка", "меньше доля трафика"],
                    "accent": "#f97316",
                },
                {
                    "label": "Подводные кабели",
                    "headline": "Главная магистраль",
                    "bullets": ["низкая задержка", "огромная пропускная способность", "соединяют дата-центры"],
                    "accent": "#22c55e",
                },
            ],
        },
        "traffic-stat": {
            "type": "stat_card",
            "title": "Физический интернет",
            "value": "95%+",
            "label": "международного трафика идет по подводным кабелям",
            "note": "точная доля зависит от методики оценки, но порядок величины именно такой",
        },
        "cable-cutaway": {
            "type": "layered_cutaway",
            "title": "Что внутри подводного кабеля",
            "subtitle": "Световые импульсы идут по тонким оптическим волокнам",
            "layers": [
                {"label": "Внешняя защита", "note": "пластик и броня", "color": "#334155"},
                {"label": "Стальная броня", "note": "защита на мелководье", "color": "#64748b"},
                {"label": "Медные элементы", "note": "питание усилителей", "color": "#f59e0b"},
                {"label": "Гидроизоляция", "note": "барьер от воды", "color": "#0ea5e9"},
                {"label": "Оптические волокна", "note": "данные в виде света", "color": "#e0f2fe"},
            ],
        },
        "telegraph-timeline": {
            "type": "timeline",
            "title": "Интернет начался до интернета",
            "events": [
                {"date": "1840-е", "label": "телеграф меняет скорость связи"},
                {"date": "1858", "label": "первый трансатлантический кабель"},
                {"date": "XX век", "label": "оптоволоконные магистрали"},
                {"date": "сегодня", "label": "глобальная сеть под океаном"},
            ],
        },
        "bgp-reroute": {
            "type": "reroute_network",
            "title": "Интернет ищет обходной путь",
            "subtitle": "Если одна линия повреждена, трафик может пойти другим маршрутом",
            "nodes": [
                {"id": "A", "label": "Вы"},
                {"id": "B", "label": "ISP"},
                {"id": "C", "label": "Кабель 1", "accent": "#ef4444"},
                {"id": "D", "label": "Узел"},
                {"id": "E", "label": "Кабель 2"},
                {"id": "F", "label": "Сайт"},
            ],
            "edges": [["A", "B"], ["B", "C"], ["C", "F"], ["B", "D"], ["D", "E"], ["E", "F"], ["D", "F"]],
            "routes": [
                {"path": ["A", "B", "C", "F"], "color": "#ef4444", "dash": "18 18", "opacity": 0.55},
                {"path": ["A", "B", "D", "E", "F"], "color": "#38bdf8"},
            ],
        },
    }


def write_schema(spec: dict, output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_schema(spec), encoding="utf-8")


def write_samples(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, spec in sample_specs().items():
        (output_dir / f"{name}.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        write_schema(spec, output_dir / f"{name}.svg")


def main():
    parser = argparse.ArgumentParser(description="Generate documentary SVG schemes from JSON specs.")
    parser.add_argument("input", nargs="?", help="Schema JSON file")
    parser.add_argument("output", nargs="?", help="Output SVG file")
    parser.add_argument("--samples", action="store_true", help="Write sample specs and SVGs")
    parser.add_argument("--output-dir", default="schema_samples", help="Directory for --samples")
    args = parser.parse_args()

    if args.samples:
        write_samples(Path(args.output_dir))
        return
    if not args.input or not args.output:
        parser.error("input and output are required unless --samples is used")
    spec = json.loads(Path(args.input).read_text(encoding="utf-8"))
    write_schema(spec, Path(args.output))


if __name__ == "__main__":
    main()
