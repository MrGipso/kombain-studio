"""UI-independent shot planning for the desktop and web pipelines.

The language model proposes a shot list; this module then applies deterministic
timeline, route-budget, prompt-repair, and style rules.  It intentionally has
no dependency on the desktop application, the web backend, Tk, or global
settings.  Runtime policy such as reference-card density is supplied by the
caller through explicit arguments.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from core import lm
from core.models import (
    Scene,
    Shot,
    normalize_timestamp,
    normalize_numeric_screen_text,
    seconds_to_timecode,
    timecode_to_seconds,
)
from core.shots import (
    collapse_redundant_shot_runs,
    consolidate_sfx_queries,
    enforce_minimum_shot_durations,
    diversify_repeated_shots,
    enforce_ai_route_budget,
    enforce_route_budget,
)
from core.style_presets import (
    DEFAULT_VIDEO_STYLE,
    compose_image_prompt,
    compose_video_prompt,
    get_video_style,
    style_director_brief,
)


SHOT_TARGET_SECONDS = 4
SHOT_MAX_SECONDS = 15
# Upper bound on shots the LM is asked to write for one scene.  Kept low on
# purpose: each shot costs one image plus one video generation downstream.
SHOT_MAX_PER_SCENE = 6
GLOBAL_VISUAL_FAMILIES = (
    "observational_broll",
    "human_activity",
    "physical_process",
    "institutional_location",
    "archive_document",
    "historical_reconstruction",
    "object_detail",
    "map_spatial",
    "data_graphic",
    "environmental_transition",
)


@dataclass(frozen=True)
class _LmSettings:
    """Minimal explicit settings adapter expected by :func:`core.lm.chat`."""

    lm_url: str
    lm_model: str
    lm_auto_unload: bool = False


@dataclass(frozen=True)
class GlobalVisualPlanItem:
    """One scene's role in the visual dramaturgy of the complete film."""

    scene_index: int
    narrative_role: str
    visual_family: str
    location: str
    primary_subject: str
    shot_progression: str
    palette: str
    transition: str
    avoid: str

    def as_dict(self) -> dict[str, object]:
        return {
            "scene_index": self.scene_index,
            "narrative_role": self.narrative_role,
            "visual_family": self.visual_family,
            "location": self.location,
            "primary_subject": self.primary_subject,
            "shot_progression": self.shot_progression,
            "palette": self.palette,
            "transition": self.transition,
            "avoid": self.avoid,
        }

    def prompt_block(self) -> str:
        return (
            f"Роль в фильме: {self.narrative_role}\n"
            f"Визуальное семейство: {self.visual_family}\n"
            f"Главная локация: {self.location}\n"
            f"Главный субъект: {self.primary_subject}\n"
            f"Развитие планов: {self.shot_progression}\n"
            f"Палитра/свет: {self.palette}\n"
            f"Монтажный переход: {self.transition}\n"
            f"Не повторять: {self.avoid}"
        )


def clean_lm_json_content(content: str) -> str:
    """Keep the monolith's lenient cleanup while using core.lm for fences."""

    cleaned = lm.strip_fences(content)
    cleaned = re.sub(r':\s*\[([^\]]+)\]"\s*([,\}\]])', r': [\1]\2', cleaned)
    return cleaned.strip()


def parse_lm_json_array(content: str, label: str = "LM Studio") -> list:
    """Extract an array and recover a response truncated after a full object."""

    cleaned = clean_lm_json_content(content)
    start = cleaned.find("[")
    if start < 0:
        raise ValueError(
            f"{label} не вернул JSON-массив. Попробуйте повторить запрос "
            "или уменьшить длину транскрипции."
        )
    candidate = cleaned[start:]

    try:
        parsed = lm.extract_json(candidate)
        if isinstance(parsed, list):
            return parsed
    except (ValueError, json.JSONDecodeError):
        pass

    decoder = json.JSONDecoder()
    lenient_decoder = json.JSONDecoder(strict=False)

    def _try_decode(text: str):
        for active_decoder in (decoder, lenient_decoder):
            try:
                return active_decoder.raw_decode(text)
            except json.JSONDecodeError:
                pass
            try:
                parsed_value = json.loads(text, strict=active_decoder.strict)
                return parsed_value, len(text)
            except json.JSONDecodeError:
                pass
        return None

    result = _try_decode(candidate)
    if result is not None and isinstance(result[0], list):
        return result[0]

    end = candidate.rfind("]")
    if end > 0:
        result = _try_decode(candidate[: end + 1])
        if result is not None and isinstance(result[0], list):
            return result[0]

    fixed = candidate.rstrip()
    if fixed:
        if fixed[-1] != "]":
            last_object_end = fixed.rfind("}")
            if last_object_end > 0:
                fixed = fixed[: last_object_end + 1] + "]"
        result = _try_decode(fixed)
        if result is not None and isinstance(result[0], list):
            return result[0]

    raise ValueError(
        f"{label} вернул поврежденный JSON (len={len(candidate)}). "
        f"HEAD: {candidate[:300]!r} ... TAIL: {candidate[-300:]!r}. "
        "Повторите анализ; если повторяется, уменьшите длину транскрипции "
        "или выберите более стабильную модель."
    )


_DEFAULT_NARRATIVE_ROLE = "Развитие аргумента и продвижение истории"
_DEFAULT_NARRATIVE_ROLE_KEY = " ".join(_DEFAULT_NARRATIVE_ROLE.lower().split())
_GLOBAL_PLAN_FIELDS = (
    "narrative_role",
    "visual_family",
    "location",
    "primary_subject",
    "shot_progression",
    "palette",
    "transition",
    "avoid",
)
_VISUAL_FAMILY_ALIASES = {
    "archive": "archive_document",
    "archival_document": "archive_document",
    "document_archive": "archive_document",
    "data_visualization": "data_graphic",
    "financial_data": "data_graphic",
    "infographic": "data_graphic",
    "industrial_location": "institutional_location",
    "institutional_exterior": "institutional_location",
    "location_establishing": "institutional_location",
    "industrial_process": "physical_process",
    "manufacturing_process": "physical_process",
    "process": "physical_process",
    "human_observation": "observational_broll",
    "observational": "observational_broll",
    "reconstruction": "historical_reconstruction",
    "historical_archive": "historical_reconstruction",
    "map": "map_spatial",
    "spatial_map": "map_spatial",
    "detail": "object_detail",
    "macro_detail": "object_detail",
    "transition": "environmental_transition",
}
_CLICHE_MOTIFS: dict[str, tuple[str, ...]] = {
    "серверные/дата-центры": (
        "server",
        "data center",
        "datacenter",
        "сервер",
        "дата-центр",
    ),
    "экраны/терминалы": (
        "screen",
        "monitor",
        "terminal",
        "interface",
        "экран",
        "монитор",
        "терминал",
        "интерфейс",
    ),
    "трейдеры/биржевые залы": (
        "trader",
        "trading floor",
        "stock exchange floor",
        "трейдер",
        "торговый зал",
        "биржевой зал",
    ),
}


def parse_global_visual_plan(
    content: str,
    scenes: list[Scene],
) -> list[GlobalVisualPlanItem]:
    """Parse an all-scenes LM result without silently inventing missing rows."""

    raw_items = parse_lm_json_array(content, "Глобальный визуальный план LM Studio")
    expected = [scene.index for scene in scenes]
    scene_by_index = {scene.index: scene for scene in scenes}
    if len(raw_items) != len(expected):
        raise ValueError(
            f"ожидалось {len(expected)} сцен, получено {len(raw_items)}"
        )

    def plan_text(value: object, field: str) -> str:
        optional_defaults = {
            "narrative_role": _DEFAULT_NARRATIVE_ROLE,
            "shot_progression": "Общий план → действие → предметная деталь",
            "palette": "Натуральный документальный свет и цвет локации",
            "transition": "Смысловой монтажный стык со следующей сценой",
            "avoid": "Не повторять визуальные мотивы соседних сцен",
        }
        if isinstance(value, str):
            text = value
        elif isinstance(value, list):
            text = "; ".join(str(item) for item in value if item not in (None, ""))
        elif isinstance(value, dict):
            text = "; ".join(
                f"{key}: {item}" for key, item in value.items() if item not in (None, "")
            )
        elif value is None and field in optional_defaults:
            text = optional_defaults[field]
        else:
            text = str(value or "")
        text = re.sub(r"\s+", " ", text).strip()
        if not text and field in optional_defaults:
            return optional_defaults[field]
        return text

    items: list[GlobalVisualPlanItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("каждая строка глобального плана должна быть JSON-объектом")
        scene_index = raw.get("scene_index")
        if isinstance(scene_index, bool) or not isinstance(scene_index, int):
            raise ValueError("scene_index глобального плана должен быть целым числом")
        source_scene = scene_by_index.get(scene_index)
        values: dict[str, str] = {}
        for field in _GLOBAL_PLAN_FIELDS:
            raw_value = raw.get(field)
            if raw_value in (None, "") and source_scene is not None:
                if field == "primary_subject":
                    raw_value = source_scene.title
                elif field == "location":
                    raw_value = (source_scene.visual_direction or source_scene.title)[:300]
            value = plan_text(raw_value, field)
            if not value or len(value) > 320:
                raise ValueError(f"поле {field} пустое или длиннее 320 символов")
            values[field] = value
        family_key = (
            values["visual_family"]
            .lower()
            .strip()
            .replace("-", "_")
            .replace(" ", "_")
        )
        values["visual_family"] = _VISUAL_FAMILY_ALIASES.get(
            family_key,
            family_key,
        )
        if values["visual_family"] not in GLOBAL_VISUAL_FAMILIES:
            raise ValueError(
                f"неизвестное visual_family: {values['visual_family']}"
            )
        items.append(GlobalVisualPlanItem(scene_index=scene_index, **values))

    actual = [item.scene_index for item in items]
    if actual != expected or len(actual) != len(set(actual)):
        raise ValueError(
            "глобальный план должен содержать все scene_index ровно один раз и по порядку"
        )
    return items


def audit_global_visual_plan(plan: list[GlobalVisualPlanItem]) -> list[str]:
    """Return professional-diversity violations which require an LM retry."""

    if not plan:
        return ["глобальный визуальный план пуст"]

    violations: list[str] = []
    family_counts: dict[str, int] = {}
    location_counts: dict[str, int] = {}
    composite_counts: dict[str, int] = {}
    for item in plan:
        family_counts[item.visual_family] = family_counts.get(item.visual_family, 0) + 1
        location_key = " ".join(item.location.lower().split())
        location_counts[location_key] = location_counts.get(location_key, 0) + 1
        composite = " | ".join(
            (
                item.visual_family,
                location_key,
                " ".join(item.primary_subject.lower().split()),
            )
        )
        composite_counts[composite] = composite_counts.get(composite, 0) + 1

    role_counts: dict[str, int] = {}
    for item in plan:
        role_key = " ".join(item.narrative_role.lower().split())
        # A role the parser substituted because the model omitted the field is
        # our own placeholder. Counting it as repetition made the audit punish
        # the model for the parser's default and guaranteed a rejection on any
        # film where narrative_role was left out more than a few times.
        if role_key == _DEFAULT_NARRATIVE_ROLE_KEY:
            continue
        role_counts[role_key] = role_counts.get(role_key, 0) + 1
    role_cap = max(2, math.ceil(len(plan) * 0.20))
    for role, count in sorted(role_counts.items()):
        if count > role_cap:
            violations.append(
                f"одинаковая narrative_role «{role}» повторяется {count} раз; "
                f"максимум {role_cap}. Каждая сцена должна иметь собственную "
                "функцию в драматургии"
            )

    minimum_families = min(len(plan), 5)
    if len(family_counts) < minimum_families:
        violations.append(
            f"использовано только {len(family_counts)} визуальных семейств; нужно минимум {minimum_families}"
        )

    for family, count in sorted(family_counts.items()):
        # Observational B-roll is an umbrella grammar, not one repeated motif:
        # a factory floor, a household desk and a city street may all use it.
        # Concrete device families remain substantially tighter.
        family_ratio = 0.45 if family == "observational_broll" else 0.30
        family_cap = max(3, math.ceil(len(plan) * family_ratio))
        if count > family_cap:
            violations.append(
                f"семейство {family} повторяется {count} раз; максимум {family_cap}"
            )

    location_cap = max(2, math.ceil(len(plan) * 0.14))
    for location, count in sorted(location_counts.items()):
        if count > location_cap:
            violations.append(
                f"локация «{location}» повторяется {count} раз; максимум {location_cap}"
            )

    repeated_composites = [
        key for key, count in composite_counts.items() if count > 1
    ]
    if repeated_composites:
        violations.append(
            "повторяются одинаковые сочетания семейства/локации/субъекта: "
            + "; ".join(repeated_composites[:4])
        )

    for first, second, third in zip(plan, plan[1:], plan[2:]):
        locations = {
            " ".join(item.location.lower().split())
            for item in (first, second, third)
        }
        families = {
            item.visual_family for item in (first, second, third)
        }
        if len(locations) == 1 and len(families) == 1:
            violations.append(
                f"сцены {first.scene_index}–{third.scene_index} трижды подряд повторяют "
                "одно визуальное семейство и локацию"
            )

    # The creative target in the prompt is 20%.  A 25% validation ceiling keeps
    # a single borderline scene from discarding an otherwise diverse 45-scene
    # plan, while still rejecting the 40–60% cliché concentration seen in the
    # old project.
    motif_cap = max(2, math.ceil(len(plan) * 0.25))
    for label, markers in _CLICHE_MOTIFS.items():
        matching = [
            item.scene_index
            for item in plan
            if any(
                marker in " ".join(
                    (
                        item.location,
                        item.primary_subject,
                        item.shot_progression,
                    )
                ).lower()
                for marker in markers
            )
        ]
        if len(matching) > motif_cap:
            violations.append(
                f"{label} встречаются в {len(matching)} сценах {matching}; максимум {motif_cap}"
            )
    return violations


def build_global_visual_plan_prompt(
    scenes: list[Scene],
    *,
    video_style: str = DEFAULT_VIDEO_STYLE,
    style_anchor: str = "",
    character_description: str = "",
    retry_feedback: str = "",
) -> str:
    """Build one compact all-film directing request for LM Studio."""

    selected_style = get_video_style(video_style)
    scene_rows = [
        {
            "scene_index": scene.index,
            "start": scene.start,
            "end": scene.end,
            "title": scene.title,
            "voice_excerpt": re.sub(r"\s+", " ", scene.voice_text).strip()[:700],
            "numeric_overlay": normalize_numeric_screen_text(
                scene.voice_text,
                scene.screen_text,
            ),
        }
        for scene in scenes
    ]
    feedback_block = (
        "\nПРЕДЫДУЩИЙ ВАРИАНТ ОТКЛОНЁН. Исправь все нарушения:\n"
        f"{retry_feedback}\n"
        if retry_feedback
        else ""
    )
    character_block = (
        f"\nПостоянный персонаж/объект: {character_description}\n"
        if character_description
        else ""
    )
    return (
        "Ты главный visual director полного документального YouTube-фильма. "
        "Сначала распредели визуальную драматургию ПО ВСЕМ сценам; кадры пока не создавай.\n"
        f"Стиль: {selected_style.name}. {style_director_brief(video_style, style_anchor)}"
        f"{character_block}{feedback_block}\n"
        "Верни ТОЛЬКО JSON-массив, ровно один объект на каждую входную сцену, "
        "в исходном порядке. Поля каждого объекта:\n"
        "scene_index, narrative_role, visual_family, location, primary_subject, "
        "shot_progression, palette, transition, avoid.\n"
        "scene_index — целое число; ВСЕ остальные поля — короткие JSON-строки, "
        "не массивы и не вложенные объекты.\n"
        f"visual_family — строго одно из: {', '.join(GLOBAL_VISUAL_FAMILIES)}.\n\n"
        "ПРОФЕССИОНАЛЬНЫЕ ПРАВИЛА:\n"
        "- Каждая сцена получает одну главную визуальную функцию и физически снимаемую идею.\n"
        "- Распредели по фильму разные локации, субъекты и визуальные семейства; "
        "одинаковая комбинация не может повторяться.\n"
        "- Не ставь одинаковое visual_family и локацию в соседних сценах.\n"
        "- Не используй серверные, мониторы, терминалы, биржевые залы и синие LED "
        "как универсальную метафору денег или технологий. Каждый такой мотив допустим "
        "только когда он буквально нужен тексту, и не более чем примерно в 20% сцен.\n"
        "- shot_progression описывает развитие внутри сцены: общий → действие → деталь "
        "или другую осмысленную последовательность, а не список случайных кадров.\n"
        "- transition связывает конец текущей сцены со следующей через движение, форму, "
        "звук или смысловой контраст.\n"
        "- avoid перечисляет конкретные мотивы из соседних сцен, которые нельзя повторять.\n"
        "- Экранные надписи не придумывай: numeric_overlay уже содержит единственный "
        "разрешённый числовой акцент либо пустую строку.\n"
        "- Не выдумывай факты и не меняй тайминги.\n"
        "- Все описательные поля пиши по-русски; visual_family оставь как заданный enum.\n\n"
        f"СЦЕНЫ JSON:\n{json.dumps(scene_rows, ensure_ascii=False)}"
    )


def request_global_visual_plan(
    base_url: str,
    model: str,
    prompt: str,
    ttl_seconds: int | None = None,
) -> str:
    """Request the mandatory all-film visual analysis through LM Studio."""

    settings = _LmSettings(
        lm_url=(base_url or lm.DEFAULT_URL).rstrip("/"),
        lm_model=model.strip() or "local-model",
    )
    return lm.chat(
        settings,
        [
            {
                "role": "system",
                "content": (
                    "Верни только валидный JSON-массив без markdown. "
                    "Ты планируешь глобальную визуальную драматургию всего фильма."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        timeout=300,
        ttl=ttl_seconds,
        max_tokens=16_000,
    )


def create_global_visual_plan(
    base_url: str,
    model: str,
    scenes: list[Scene],
    *,
    ttl_seconds: int | None = None,
    video_style: str = DEFAULT_VIDEO_STYLE,
    style_anchor: str = "",
    character_description: str = "",
    log_callback: Callable[[str], None] | None = None,
) -> list[GlobalVisualPlanItem]:
    """Run a required global LM pass and reject structurally monotonous plans."""

    if not scenes:
        return []
    feedback = ""
    last_error = ""
    best_plan: list[GlobalVisualPlanItem] | None = None
    best_violations = 0
    for attempt in range(1, 4):
        if log_callback:
            log_callback(
                f"LM Studio: глобальная режиссура видеоряда, попытка {attempt}/3..."
            )
        try:
            prompt = build_global_visual_plan_prompt(
                scenes,
                video_style=video_style,
                style_anchor=style_anchor,
                character_description=character_description,
                retry_feedback=feedback,
            )
            content = request_global_visual_plan(
                base_url,
                model,
                prompt,
                ttl_seconds=ttl_seconds,
            )
            plan = parse_global_visual_plan(content, scenes)
            violations = audit_global_visual_plan(plan)
            if not violations:
                if log_callback:
                    log_callback(
                        f"Глобальный визуальный план принят: {len(plan)} сцен, "
                        f"{len({item.visual_family for item in plan})} визуальных семейств."
                    )
                return plan
            last_error = "; ".join(violations)
            # Keep the least-bad plan seen so far. Diversity is a matter of
            # taste and the model may never fully satisfy it, but a complete
            # plan with recorded debt is worth far more than no film at all.
            if best_plan is None or len(violations) < best_violations:
                best_plan, best_violations = plan, len(violations)
        except Exception as exc:
            last_error = str(exc)
        feedback = last_error[:4_000]
        if log_callback:
            log_callback(
                f"Глобальный визуальный план отклонён: {last_error}"
            )
    if best_plan is not None:
        # Creative critique must never be fatal: a rejected plan used to fail
        # the whole shots stage and leave the project unfinishable without
        # manual intervention.
        if log_callback:
            log_callback(
                "Глобальный визуальный план принят с замечаниями после 3 попыток "
                f"({best_violations} нарушений): {last_error}. Шот-лист будет "
                "построен; замечания можно исправить точечно."
            )
        return best_plan
    raise ValueError(
        "LM Studio не вернула разбираемый глобальный визуальный план после "
        f"3 попыток. Старый шот-лист сохранён. Причина: {last_error}"
    )


def preferred_realistic_image_model(image_preference: str) -> str:
    return (
        "ideogram4"
        if (image_preference or "").strip().lower() == "ideogram"
        else "krea2_turbo"
    )


def make_asset_query(text: str) -> str:
    keywords: list[str] = []
    for key, label in (
        ("океан", "deep ocean"),
        ("кабель", "submarine cable"),
        ("дата", "data center"),
        ("сервер", "server room"),
        ("кораб", "cable laying ship"),
        ("судно", "cable laying ship"),
        ("спут", "satellite orbit"),
        ("бирж", "stock exchange"),
        ("телеграф", "telegraph archive"),
    ):
        if key in text:
            keywords.append(label)
    return " ".join(dict.fromkeys(keywords)) or "documentary technology broll"


def clean_visual_prompt_source(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip().replace("->", ", then ")
    cleaned = re.sub(
        r"\b(?:Supporting B-roll or graphic for scene|Closing visual for scene|Scene idea):\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" .,:;") or "documentary technology visual"


def suggest_shot_type(text: str) -> str:
    lower = text.lower()
    rules = (
        (("macro", "detail", "волокн", "cable", "close", "microscop"), "extreme macro detail shot"),
        (("aerial", "вид сверху", "overview", "data center", "city", "город"), "wide aerial establishing shot"),
        (("портрет", "face", "portrait", "person", "человек", "инженер", "учён"), "medium close-up portrait with environmental context"),
        (("underwater", "океан", "под водой", "ocean floor", "море", "дно"), "underwater wide shot"),
        (("ship", "судно", "кораб", "vessel", "лодк"), "dramatic low angle wide shot"),
        (("building", "здани", "architecture", "архитектур", "factory", "завод"), "low angle architectural shot"),
        (("server", "сервер", "data center", "datacenter"), "symmetrical wide shot of server rows"),
        (("hand", "рука", "палец", "fingers", "инструмент", "tool"), "extreme close-up of hands at work"),
        (("историческ", "archive", "архив", "старин", "old", "antique", "винтаж"), "vintage archival still"),
        (("terminal", "screen", "бирж", "stock"), "medium shot of glowing screens"),
    )
    for keywords, shot_type in rules:
        if any(word in lower for word in keywords):
            return shot_type
    return "medium establishing shot"


def make_realistic_prompt(
    text: str,
    shot_type: str = "",
    style_anchor: str = "",
) -> str:
    """Build a neutral concrete prompt body; style is composed exactly once later."""

    del style_anchor  # accepted for desktop-call compatibility
    hint = clean_visual_prompt_source(text)
    if len(hint) > 300:
        hint = hint[:297].rsplit(" ", 1)[0] + "..."
    framing = shot_type or suggest_shot_type(hint)
    return (
        f"{framing} of {hint}, a real recognizable place shown with clear shapes, "
        "colours and its own landmarks"
    )


def suggest_motion(text: str) -> str:
    lower = text.lower()
    if any(
        word in lower
        for word in ("wide", "aerial", "establishing", "landscape", "overview", "panorama", "vista")
    ):
        return "slowly drifts across the scene, letting the space and its landmarks read"
    if any(
        word in lower
        for word in ("close-up", "close up", "macro", "extreme", "detail")
    ):
        return "gently pushes in on the detail and holds, keeping the subject stable"
    return "slowly pushes in and holds steady, letting the scene settle"


def make_ltx_prompt(image_prompt: str, motion_hint: str = "") -> str:
    motion = motion_hint or suggest_motion(image_prompt)
    return (
        f"The camera {motion}. "
        "Subtle handmade movement brings the shot to life while the composition stays stable. "
        "Small environmental motion is visible where appropriate: light shifts, particles "
        "drift, fabric or water moves softly."
    )


def motion_is_weak(shot: Shot) -> bool:
    text = (
        (getattr(shot, "video_prompt", "") or "")
        + " "
        + (getattr(shot, "motion", "") or "")
    ).strip()
    return not text or len(text.split()) < 4


def motions_mostly_weak(shots: Iterable[Shot]) -> bool:
    shot_list = list(shots)
    return bool(shot_list) and sum(motion_is_weak(shot) for shot in shot_list) > len(shot_list) / 2


def prompt_has_too_much_transcript(prompt: str) -> bool:
    if not prompt.strip():
        return True
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", prompt))
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", prompt))
    if letters and cyrillic / letters > 0.18:
        return True
    markers = (
        "вот вы отправили",
        "если спросить",
        "в этот момент ваши данные",
        "как образом",
        "каким образом",
        "по дну океана",
    )
    return any(marker in prompt.lower() for marker in markers)


def rebuild_image_prompt_for_shot(shot: Shot) -> str:
    source = clean_visual_prompt_source(
        shot.description or shot.asset_query or shot.screen_text or "a simple real scene"
    )
    if shot.asset_query and shot.asset_query.lower() not in source.lower():
        source = f"{source}, {shot.asset_query}"
    if shot.visual_type == "ai_typography_card":
        return (
            "Clean cinematic editorial title card, strong composition, high contrast, "
            "16:9 layout, minimal visual clutter. "
            f'Visible text, exactly: "{shot.screen_text}". Visual idea: {source}'
        )
    return (
        f"{suggest_shot_type(source)} of {source}, a real recognizable place shown "
        "with clear shapes, colours and its own landmarks"
    )


def repair_shot_prompts(shots: list[Shot]) -> None:
    for shot in shots:
        if shot.image_model and prompt_has_too_much_transcript(shot.image_prompt):
            shot.image_prompt = rebuild_image_prompt_for_shot(shot)
        if shot.video_model and prompt_has_too_much_transcript(shot.video_prompt):
            shot.video_prompt = make_ltx_prompt(shot.image_prompt)


def make_shot_description(scene: Scene, local_index: int, shot_count: int) -> str:
    if local_index == 0:
        return scene.visual_direction or scene.title
    if local_index == shot_count - 1:
        return f"Closing visual for scene: {scene.title}"
    return f"Supporting B-roll or graphic for scene: {scene.title}"


def choose_visual_route(
    text: str,
    screen_text: str = "",
    image_preference: str = "Auto",
    style_anchor: str = "",
    video_style: str = DEFAULT_VIDEO_STYLE,
    character_description: str = "",
) -> dict:
    del style_anchor, video_style, character_description
    lower = f"{text} {screen_text}".lower()
    body = make_realistic_prompt(text)
    return {
        "visual_type": "ai_image_then_i2v",
        "asset_query": make_asset_query(lower),
        "image_model": preferred_realistic_image_model(image_preference),
        "video_model": "ltx2.3_i2v",
        "image_prompt": body,
        "video_prompt": make_ltx_prompt(body),
        "motion": "slow documentary camera movement",
        "fallback": "AI image-to-video generation",
    }


def build_heuristic_shots(
    scenes: list[Scene],
    image_preference: str = "Auto",
    style_anchor: str = "",
    video_style: str = DEFAULT_VIDEO_STYLE,
    character_description: str = "",
    compose: bool = True,
) -> list[Shot]:
    shots: list[Shot] = []
    for scene in scenes:
        scene_start = timecode_to_seconds(scene.start)
        scene_end = max(scene_start + 1, timecode_to_seconds(scene.end))
        duration = scene_end - scene_start
        shot_count = max(1, round(duration / SHOT_TARGET_SECONDS))
        step = duration / shot_count
        for local_index in range(shot_count):
            start_seconds = round(scene_start + local_index * step)
            end_seconds = round(scene_start + (local_index + 1) * step)
            if local_index == shot_count - 1:
                end_seconds = scene_end
            text_hint = scene.voice_text if local_index == 0 else scene.visual_direction
            route = choose_visual_route(
                text_hint,
                scene.screen_text,
                image_preference=image_preference,
                style_anchor=style_anchor,
                video_style=video_style,
                character_description=character_description,
            )
            description = make_shot_description(scene, local_index, shot_count)
            shots.append(
                Shot(
                    index=len(shots) + 1,
                    scene_index=scene.index,
                    start=seconds_to_timecode(start_seconds),
                    end=seconds_to_timecode(end_seconds),
                    visual_type=route["visual_type"],
                    description=description,
                    asset_query=route["asset_query"],
                    image_model=route["image_model"],
                    video_model=route["video_model"],
                    image_prompt=route["image_prompt"],
                    video_prompt=route["video_prompt"],
                    screen_text=scene.screen_text if local_index == 0 else "",
                    motion=route["motion"],
                    priority="high" if local_index == 0 else "medium",
                    fallback=route["fallback"],
                )
            )
    repair_shot_prompts(shots)
    if compose:
        for shot in shots:
            shot.image_prompt = compose_image_prompt(
                video_style,
                shot.image_prompt or shot.description,
                character_description=character_description,
                custom_notes=style_anchor,
            )
            shot.video_prompt = compose_video_prompt(
                video_style,
                shot.video_prompt or shot.motion,
                shot_description=shot.description,
            )
    return shots


def parse_shots(raw_shots: list[dict], offset: int = 0) -> list[Shot]:
    parsed: list[Shot] = []
    for raw in raw_shots:
        parsed.append(
            Shot(
                index=len(parsed) + 1 + offset,
                scene_index=int(raw.get("scene_index", 0)),
                start=normalize_timestamp(raw.get("start", "00:00:00")),
                end=normalize_timestamp(
                    raw.get("end", raw.get("start", "00:00:00"))
                ),
                visual_type=str(
                    raw.get("visual_type", "ai_image_then_i2v")
                ).strip(),
                description=str(raw.get("description", "")).strip(),
                asset_query=str(raw.get("asset_query", "")).strip(),
                image_model=str(raw.get("image_model", "")).strip(),
                video_model=str(raw.get("video_model", "")).strip(),
                image_prompt=str(raw.get("image_prompt", "")).strip(),
                video_prompt=str(raw.get("video_prompt", "")).strip(),
                screen_text=str(raw.get("screen_text", "")).strip(),
                motion=str(raw.get("motion", "")).strip(),
                priority=str(raw.get("priority", "medium")).strip(),
                fallback=str(raw.get("fallback", "")).strip(),
                sfx_query=str(raw.get("sfx_query", "")).strip(),
                sfx_accent_query=str(raw.get("sfx_accent_query", "")).strip(),
                reference_query=str(raw.get("reference_query", "")).strip(),
                reference_source=(
                    str(raw.get("reference_source", "wikipedia")).strip()
                    or "wikipedia"
                ),
            )
        )
    return parsed


_CONTINUATION_VARIANTS = (
    (
        "a closer medium view of the same subject",
        "The camera holds a closer framing of the same subject; small natural motion continues.",
    ),
    (
        "the same scene from a noticeably different side angle",
        "The camera observes from a new side angle; gentle drift, the action continues.",
    ),
    (
        "a detail close-up of the key object in the same scene",
        "A tight detail view; subtle ambient motion, slow push-in.",
    ),
    (
        "a slightly wider view of the same scene",
        "The camera pulls back slightly to a wider view; calm ambient motion.",
    ),
)


def continuation_shot(
    shot: Shot,
    start_seconds: int,
    end_seconds: int,
    depth: int = 0,
) -> Shot:
    angle, motion = _CONTINUATION_VARIANTS[
        (shot.index + depth) % len(_CONTINUATION_VARIANTS)
    ]
    data = dict(vars(shot))
    data.update(
        start=seconds_to_timecode(start_seconds),
        end=seconds_to_timecode(end_seconds),
        description=(
            shot.description + " (продолжение, другой ракурс)"
        ).strip(),
        image_prompt=(shot.image_prompt or shot.description).strip()
        + f" Continuation shot of the same scene and location: {angle}.",
        video_prompt=motion,
        motion=motion,
        screen_text="",
    )
    return Shot(**data)


def split_long_shot(
    shot: Shot,
    scene: Scene,
    scene_segments: list,
    depth: int = 0,
) -> list[Shot]:
    """Split an over-limit slot on nearby transcript boundaries."""

    start_seconds = timecode_to_seconds(shot.start)
    end_seconds = timecode_to_seconds(shot.end)
    duration = end_seconds - start_seconds
    if duration <= SHOT_MAX_SECONDS:
        return [shot]

    shot_segments = [
        segment
        for segment in scene_segments
        if timecode_to_seconds(segment.start) < end_seconds
        and timecode_to_seconds(segment.end) > start_seconds
    ]

    max_end = start_seconds + SHOT_MAX_SECONDS + 2
    split_at = start_seconds + SHOT_MAX_SECONDS
    if len(shot_segments) >= 2:
        for segment in shot_segments:
            segment_end = timecode_to_seconds(segment.end)
            if segment_end <= max_end and segment_end > split_at:
                split_at = segment_end
    if split_at <= start_seconds or split_at >= end_seconds - 1:
        # Sparse/imported subtitles may not expose a useful boundary.  The
        # duration limit is still a hard postcondition, so split at the exact
        # limit instead of leaving a multi-minute final shot.
        split_at = min(start_seconds + SHOT_MAX_SECONDS, end_seconds - 1)
    if split_at <= start_seconds or split_at >= end_seconds:
        return [shot]

    part_a_data = dict(vars(shot))
    part_a_data.update(end=seconds_to_timecode(split_at))
    part_a = Shot(**part_a_data)
    part_b = continuation_shot(
        shot,
        split_at,
        end_seconds,
        depth=depth,
    )
    return [part_a, *split_long_shot(part_b, scene, scene_segments, depth + 1)]


def tiled_intervals(
    starts_seconds: list[int],
    total_end: int,
    min_duration: int = 1,
) -> list[tuple[int, int]]:
    """Pure timing helper equivalent to the exporter's timeline partition."""

    if not starts_seconds:
        return []
    bounds = [0]
    previous = 0
    for start in starts_seconds[1:]:
        value = int(start)
        if value < previous + min_duration:
            value = previous + min_duration
        bounds.append(value)
        previous = value
    end = int(total_end)
    if end < previous + min_duration:
        end = previous + min_duration
    bounds.append(end)
    return [(bounds[index], bounds[index + 1]) for index in range(len(starts_seconds))]


def retile_shots_contiguous(
    shots: list[Shot],
    total_end: int | None = None,
) -> list[Shot]:
    """Make the ordered shot list a gapless, non-overlapping timeline."""

    if not shots:
        return shots
    ordered = sorted(shots, key=lambda shot: timecode_to_seconds(shot.start))
    starts = [timecode_to_seconds(shot.start) for shot in ordered]
    if total_end is None:
        last_end = max(
            (timecode_to_seconds(shot.end) for shot in ordered),
            default=0,
        )
        total_end = max(last_end, starts[-1] + 1)
    intervals = tiled_intervals(starts, total_end)
    for index, (shot, (start, end)) in enumerate(
        zip(ordered, intervals),
        start=1,
    ):
        shot.start = seconds_to_timecode(start)
        shot.end = seconds_to_timecode(end)
        shot.index = index
    return ordered


def clamp_shots_to_scenes(shots: list[Shot], scenes: list[Scene]) -> list[Shot]:
    scene_by_index = {scene.index: scene for scene in scenes}
    clamped: list[Shot] = []
    for shot in shots:
        scene = scene_by_index.get(shot.scene_index)
        if not scene:
            shot.index = len(clamped) + 1
            clamped.append(shot)
            continue

        scene_start = timecode_to_seconds(scene.start)
        scene_end = max(scene_start + 1, timecode_to_seconds(scene.end))
        start = min(
            max(timecode_to_seconds(shot.start), scene_start),
            scene_end - 1,
        )
        end = min(
            max(timecode_to_seconds(shot.end), start + 1),
            scene_end,
        )
        if end <= start:
            end = min(scene_end, start + 1)
        shot.index = len(clamped) + 1
        shot.start = seconds_to_timecode(start)
        shot.end = seconds_to_timecode(end)
        clamped.append(shot)
    return clamped


def finalize_shot_timeline(
    shots: list[Shot],
    scenes: list[Scene],
    segments: list | None = None,
) -> list[Shot]:
    """Build a gapless per-scene timeline and enforce the shot duration limit.

    The LM proposes visual concepts and approximate starts.  This scheduler is
    the sole owner of final timing: it tiles each real scene range, then
    deterministically adds continuation coverage wherever a proposed concept
    would otherwise be stretched past the LTX-safe duration.
    """
    if not shots or not scenes:
        return clamp_shots_to_scenes(shots, scenes)

    clamped = clamp_shots_to_scenes(shots, scenes)
    scene_by_index = {scene.index: scene for scene in scenes}
    grouped: dict[int, list[Shot]] = {}
    for shot in clamped:
        if shot.scene_index in scene_by_index:
            grouped.setdefault(shot.scene_index, []).append(shot)

    source_segments = list(segments or [])
    finalized: list[Shot] = []
    for scene in sorted(scenes, key=lambda item: timecode_to_seconds(item.start)):
        scene_shots = sorted(
            grouped.get(scene.index, []),
            key=lambda item: timecode_to_seconds(item.start),
        )
        if not scene_shots:
            continue

        scene_start = timecode_to_seconds(scene.start)
        scene_end = max(scene_start + 1, timecode_to_seconds(scene.end))
        scene_duration = scene_end - scene_start

        # A one-second timeline cannot contain more than one distinct
        # one-second slot.  This is only a malformed-input guard; normal edit
        # scenes are at least several seconds long.
        if len(scene_shots) > scene_duration:
            scene_shots = scene_shots[:scene_duration]

        local_starts = [0]
        for position, shot in enumerate(scene_shots[1:], start=1):
            remaining = len(scene_shots) - position - 1
            latest = scene_duration - remaining - 1
            proposed = timecode_to_seconds(shot.start) - scene_start
            local_starts.append(
                max(local_starts[-1] + 1, min(proposed, latest))
            )

        intervals = [
            (
                scene_start + local_starts[index],
                scene_start + (
                    local_starts[index + 1]
                    if index + 1 < len(local_starts)
                    else scene_duration
                ),
            )
            for index in range(len(local_starts))
        ]
        for shot, (start, end) in zip(scene_shots, intervals):
            shot.start = seconds_to_timecode(start)
            shot.end = seconds_to_timecode(end)

        scene_segments = [
            segment
            for segment in source_segments
            if timecode_to_seconds(segment.start) < scene_end
            and timecode_to_seconds(segment.end) > scene_start
        ]
        for shot in scene_shots:
            finalized.extend(split_long_shot(shot, scene, scene_segments))

    for index, shot in enumerate(finalized, start=1):
        shot.index = index
    return finalized


def normalize_shot_routes(
    shots: list[Shot],
    image_preference: str = "Auto",
) -> None:
    preferred_model = preferred_realistic_image_model(image_preference)
    preference = (image_preference or "").strip().lower()
    for shot in shots:
        visual_type = (shot.visual_type or "").lower()
        if visual_type == "stock_video":
            continue
        if visual_type == "still_kenburns":
            if shot.image_model != "ideogram4" or preference in {"krea", "ideogram"}:
                shot.image_model = preferred_model
            shot.video_model = ""
            continue
        if visual_type == "ai_typography_card":
            continue
        if visual_type == "web_reference_card":
            shot.image_model = ""
            shot.video_model = ""
            continue
        if shot.visual_type not in {"ai_image", "ai_image_then_i2v"}:
            shot.visual_type = "ai_image_then_i2v"
        if shot.image_model != "ideogram4" or preference in {"krea", "ideogram"}:
            shot.image_model = preferred_model
        shot.video_model = shot.video_model or "ltx2.3_i2v"
        if not shot.fallback or "Remotion" in shot.fallback or "SVG" in shot.fallback:
            shot.fallback = "AI image-to-video generation"
            if shot.visual_type == "ai_image":
                shot.visual_type = "ai_image_then_i2v"


def normalize_route_priority(route_priority: str) -> str:
    value = (route_priority or "balanced").strip().lower()
    return value if value in {"economy", "balanced", "rich"} else "balanced"


def build_route_block(
    hybrid: bool,
    route_priority: str,
    ai_route_mix: bool,
) -> str:
    priority = normalize_route_priority(route_priority)
    if priority == "economy":
        route_bias = (
            "ПРИОРИТЕТ МАРШРУТА: ЭКОНОМНО. По умолчанию предпочитай stock_video для любой реально снимаемой сцены. "
            "still_kenburns используй для явной статики, объектов, зданий, портретов, схем и исторических сцен без нужного движения. "
            "ai_image_then_i2v — крайний случай: только повторяющийся персонаж, выбранная стилистика, неснимаемое или кадр, где живое движение критично. "
            "ЖЁСТКИЙ БЮДЖЕТ: не больше 1 кадра из 5 и не больше 20% общей длительности — ai_image_then_i2v; "
            "считай длительность, а не количество: один длинный AI-кадр дороже трёх коротких стоковых. "
            "Самолёты, небо, грозы, города, люди, техника, природа — это ВСЕГДА stock_video, даже в драматичные моменты. "
            "Не выбирай AI-видео ради красоты, если сток или зум передадут смысл.\n"
        )
    elif priority == "rich":
        route_bias = (
            "ПРИОРИТЕТ МАРШРУТА: БОГАТО. Чаще выбирай ai_image_then_i2v для эмоционально важных, стилевых, персонажных и визуально выразительных кадров. "
            "stock_video используй для нейтрального b-roll и очевидно реальных сцен без требований к персонажу/стилю. "
            "still_kenburns используй только когда статичная картинка с медленным зумом действительно лучше движения. "
            "Сохраняй разнообразие, но не экономь AI-видео на ключевых моментах.\n"
        )
    else:
        route_bias = (
            "ПРИОРИТЕТ МАРШРУТА: СБАЛАНСИРОВАННО. Выбирай самый дешёвый маршрут, который хорошо передаёт смысл: "
            "снимаемое — stock_video, статичное — still_kenburns, стилевое/неснимаемое/важное движение — ai_image_then_i2v.\n"
        )

    if hybrid:
        return (
            "РЕЖИМ РАСПРЕДЕЛЕНИЯ (гибрид). image_model=krea2_turbo. Для КАЖДОГО кадра САМ выбери visual_type:\n"
            f"{route_bias}"
            "- stock_video — реальная нейтральная СНИМАЕМАЯ сцена без конкретного персонажа "
            "(город, природа, руки за работой, техника, толпа, транспорт). Длина такого кадра ≤5 секунд.\n"
            "asset_query ОБЯЗАТЕЛЕН для КАЖДОГО кадра, включая ai_image_then_i2v и still_kenburns — "
            "короткая английская поисковая фраза (субъект + место): она нужна, чтобы кадр можно было "
            "перевести в сток при нехватке бюджета.\n"
            "- still_kenburns — статичный субъект, где движение не нужно (пейзаж, портрет, объект, здание, "
            "историческая сцена, число/схема): будет Krea-картинка с медленным зумом. video_model оставь пустым.\n"
            "- ai_image_then_i2v — стилистика, конкретный повторяющийся персонаж или НЕснимаемое, где реально "
            "нужно живое движение. Самый дорогой маршрут — только там, где он оправдан. video_model=ltx2.3_i2v.\n"
            "Чередуй маршруты для разнообразия; не ставь один и тот же подряд без нужды.\n\n"
        )

    if ai_route_mix and priority != "rich":
        if priority == "economy":
            ai_bias = (
                "ПРИОРИТЕТ: ЭКОНОМНО. Не больше 1 кадра из 4 и не больше ~25% общей длительности — "
                "ai_image_then_i2v. Считай длительность, а не количество: один длинный AI-кадр дороже "
                "трёх коротких. Всё статичное по природе — still_kenburns.\n"
            )
        else:
            ai_bias = (
                "ПРИОРИТЕТ: СБАЛАНСИРОВАННО. Примерно половина хронометража — still_kenburns "
                "(статика с зумом), половина — ai_image_then_i2v там, где движение реально работает.\n"
            )
        return (
            "РЕЖИМ РАСПРЕДЕЛЕНИЯ (только AI, БЕЗ стока). image_model=krea2_turbo для каждого кадра. "
            "Для КАЖДОГО кадра САМ выбери visual_type:\n"
            f"{ai_bias}"
            "- still_kenburns — кадр статичен по природе: установочный план локации, объект, здание, "
            "портрет без действия, схема, «историческое фото». Krea-картинка с медленным зумом/паном. "
            "video_model оставь пустым.\n"
            "- ai_image_then_i2v — движение несёт СМЫСЛ или ЭМОЦИЮ: субъект совершает важное действие, "
            "среда в движении — суть кадра (огонь, дождь, вода, дым, толпа), хук, кульминация, "
            "кадры с priority=high. video_model=ltx2.3_i2v.\n"
            "Не ставь больше ТРЁХ still_kenburns подряд — между сериями статики вставляй живой кадр, "
            "иначе видео превращается в слайдшоу.\n\n"
        )

    return (
        "Каждый shot — AI image-to-video: Krea2 генерирует изображение, LTX 2.3 анимирует его. "
        "Для КАЖДОГО кадра image_model=krea2_turbo, video_model=ltx2.3_i2v, visual_type=ai_image_then_i2v.\n\n"
    )


def build_reference_block(reference_density: str = "normal") -> str:
    density = (reference_density or "normal").strip().lower()
    if density == "off":
        return ""
    cap_line = (
        "МАКСИМУМ 1 такой кадр на 2 сцены, только для САМОЙ важной названной сущности во всём видео-фрагменте."
        if density == "sparse"
        else "МАКСИМУМ ~1 такой кадр на сцену, и только когда сущность действительно называется в озвучке."
    )
    return (
        "ДОПОЛНИТЕЛЬНЫЙ ТИП КАДРА — web_reference_card (реальный скрин источника, выезжает сбоку "
        "на размытом фоне). Ставь его ТОЛЬКО когда в озвучке названа ИЗВЕСТНАЯ проверяемая сущность, "
        "у которой ТОЧНО есть статья в энциклопедии: крупная компания, известный человек, "
        "устоявшаяся технология или стандарт, научная статья/paper, общепринятый термин или понятие. "
        "Это заодно даёт настоящий читаемый текст и ссылку-пруф.\n"
        "НЕ используй его для нишевых приложений, CLI-утилит и небольших опенсорс-инструментов "
        "(например LM Studio, Ollama, llama.cpp, GGUF, конкретные репозитории) — у них обычно нет "
        "статьи в Википедии, и выйдет случайный нерелевантный результат. Для таких — обычный визуал "
        "(скрин интерфейса как Krea-картинка).\n"
        f"{cap_line} Не используй его для абстракций, эмоций и общих сцен — там обычный визуал.\n"
        "Для такого кадра задай: visual_type=web_reference_card; reference_query = точное название "
        "сущности для поиска (для Википедии — на языке озвучки или по-английски; для статьи — её "
        "название); reference_source = \"wikipedia\" (по умолчанию) или \"arxiv\" для научной статьи. "
        "image_model и video_model оставь пустыми. ОБЯЗАТЕЛЬНО всё равно заполни asset_query и "
        "image_prompt — это резерв, если источник не найдётся.\n\n"
    )


def build_shot_instructions(
    image_preference: str = "Auto",
    style_anchor: str = "",
    video_style: str = DEFAULT_VIDEO_STYLE,
    hybrid: bool = False,
    route_priority: str = "balanced",
    ai_route_mix: bool = False,
    reference_density: str = "normal",
) -> str:
    selected_style = get_video_style(video_style)
    route_block = build_route_block(hybrid, route_priority, ai_route_mix)
    reference_block = build_reference_block(reference_density)
    style_brief = style_director_brief(video_style, style_anchor)
    return (
        "Ты режиссёр: раскладываешь озвучку на кадры.\n\n"
        "Верни только валидный JSON-массив. Каждый объект должен иметь поля: "
        "scene_index, start, end, visual_type, description, asset_query, image_model, video_model, "
        "image_prompt, video_prompt, screen_text, motion, priority, fallback, sfx_query, "
        "sfx_accent_query, reference_query, reference_source.\n\n"
        f"{route_block}"
        f"{reference_block}"
        f"Настройка пользователя: {image_preference}.\n\n"
        f"ВЫБРАННЫЙ СТИЛЬ: {selected_style.name}\n"
        f"{style_brief}\n\n"
        "ПРАВИЛА ДЛЯ image_prompt (Krea 2 — текстовое изображение):\n"
        "- Пиши на английском, длинные детальные промпты (3-6 предложений), естественным языком, а не тегами.\n"
        "- Krea 2 понимает описательные предложения лучше, чем список ключевых слов через запятую.\n"
        "- Описывай: shot type, subject, texture/material, lighting, color palette, composition, atmosphere.\n"
        "- Сначала опиши конкретный объект/сцену, затем детали выбранного стиля. Не используй LoRA/trigger слова.\n"
        "- Каждая абстрактная мысль из текста должна стать видимым предметом или конкретным действием внутри выбранного стиля — без символов и метафор.\n"
        "- Показывай УЗНАВАЕМОЕ реальное место/объект; не полагайся на читаемый текст или логотипы в кадре (модель их коверкает) — передавай узнаваемость формами, цветами и ориентирами.\n"
        "- Чередуй планы между соседними кадрами. Предпочитай ОБЩИЙ и СРЕДНИЙ план, на котором видно место; крупные планы и макро — изредка, не по умолчанию. Не ставь два одинаковых плана подряд.\n"
        "- Конкретные материалы и текстуры выбирай из выбранного стиля. Для вязаного стиля: yarn, wool fibers, visible stitches, crocheted props, handmade textile surfaces.\n"
        "- Конкретное освещение: 'golden hour backlight', 'cold fluorescent overhead', "
        "'warm tungsten desk lamp', 'harsh noon sun casting hard shadows'.\n"
        "- ЗАПРЕЩЕНО начинать промпт с абстрактного жанра: 'Documentary...', 'Cinematic...', "
        "'Photorealistic...', 'Editorial...', 'Observational...' — это абстракция, а не промпт.\n"
        "- Промт должен начинаться с КОНКРЕТНОГО ОБЪЕКТА ИЛИ СЦЕНЫ: "
        "'A weathered wooden door...', 'Rain streaming down a window pane...', "
        "'A middle-aged man in a stained apron...', 'Dust motes floating in...'.\n"
        "- Избегай клише: 'cinematic lighting' и 'photorealistic' — "
        "используй конкретные названия источников света и материалов.\n"
        "- Не описывай жанр видео. Опиши ЧТО мы видим, а не КАК это снято.\n"
        "- Используй visual_direction из сцены как подсказку для визуального стиля.\n"
        "- Не добавляй текст, логотипы или субтитры на изображение.\n"
        "- Не заставляй кадр быть реалистичной съёмкой реального мира, если выбранный стиль декоративный или handmade.\n\n"
        "ПРИМЕР ХОРОШЕГО image_prompt (начинается с объекта, а не жанра):\n"
        "\"A 40-year-old man in a wrinkled denim shirt sits at a cluttered wooden desk, "
        "morning light streams through a dusty window casting long diagonal shadows, "
        "a cold cup of coffee and scattered papers cover the desk surface, "
        "his tired eyes focused on a glowing laptop screen, "
        "warm tungsten light from the desk lamp mixes with cool daylight, "
        "rough concrete wall texture visible behind him, documentary realism.\"\n\n"
        "ПРИМЕР ПЛОХОГО image_prompt (абстрактный, начинается с жанра — НЕ ДЕЛАЙ ТАК):\n"
        "\"Cinematic documentary scene of a man working at his desk, moody lighting, photorealistic.\"\n\n"
        "ПРАВИЛА ДЛЯ video_prompt (LTX 2.3 image-to-video):\n"
        "- Опиши ДВИЖЕНИЕ, которое реально подходит ЭТОМУ кадру. Картинка уже задаёт вид — не описывай внешность.\n"
        "- ГЛАВНОЕ ПРАВИЛО: если в кадре есть субъект/объект, который логично что-то делает — дай ОДНО маленькое конкретное действие "
        "(вязаная кукла наклоняет голову; индикатор загрузки крутится; рука поправляет предмет; лампа мигает). "
        "Если кадр по смыслу статичный (пейзаж, общий план, объект без действия) — НЕ выдумывай действие: опиши движение среды "
        "(свет, ткань, вода, частицы, пар) и лёгкое движение камеры. И то и другое — правильный ответ.\n"
        "- Всегда добавляй ОДНО движение среды и ОДНО движение камеры (present tense): "
        "'the camera slowly pushes in', 'gentle pan left', 'subtle tracking shot', 'slow dolly in'.\n"
        "- Движение маленькое и естественное: избегай резких жестов, бега, прыжков, сильной деформации объекта и всего, что может вызвать морфинг или плавление деталей.\n"
        "- НЕ оставляй video_prompt пустым и не пиши общими словами вроде 'brings the shot to life' — привяжи движение к тому, что видно в кадре.\n"
        "- НЕ описывай звук в video_prompt — для звука есть отдельное поле sfx_query.\n"
        "- 2-4 предложения в одном абзаце, на английском.\n\n"
        "ПРАВИЛА МАРШРУТИЗАЦИИ:\n"
        "- screen_text — ПО УМОЛЧАНИЮ ПУСТАЯ СТРОКА (\"\"). Заполняй ТОЛЬКО "
        "для явной цифры, суммы, процента, даты или измеримого значения, которое "
        "дословно присутствует в voice_text. Надпись обязана содержать эту же цифру, "
        "быть законченным фактом из 2-7 слов и оставаться на языке диктора. "
        "Имена, названия, термины, главы и пересказ диктора экранной надписью НЕ являются. "
        "Не больше ОДНОЙ надписи на сцену.\n"
        "  ХОРОШО: '1 удар на 1000 часов', '8 декабря 1963', '95% — замена купюр'.\n"
        "  ПЛОХО: 'Клетка Фарадея', 'Федеральная резервная система', "
        "'И вот что интересно', любая надпись без цифры.\n"
        "- description — заметка монтажёру на том же языке, что и текст диктора.\n"
        "- asset_query — короткая ЛИТЕРАЛЬНАЯ английская поисковая фраза (2-5 слов) для поиска СТОКОВОГО ВИДЕО: "
        "конкретный субъект + место/действие. БЕЗ стиля, БЕЗ метафор, БЕЗ жанровых слов "
        "(documentary/cinematic/photorealistic). Только по-английски, реальные снимаемые объекты. "
        "Примеры: 'server room rows of racks', 'ocean waves crashing rocks', 'scientist microscope laboratory', "
        "'busy city street traffic', 'hands typing on laptop'.\n"
        "- image_prompt и video_prompt на английском.\n"
        "ПРАВИЛА ЗВУКА (важно: под голосом и музыкой слышны КОРОТКИЕ звуки, а ровные "
        "подложки — почти нет; поэтому звук ставим ТОЧЕЧНО, а не сплошным ковром):\n"
        "- sfx_accent_query — ГЛАВНОЕ звуковое поле. Короткая английская фраза (2-5 слов) для ОДНОГО "
        "короткого звука-события, который слышно в начале кадра: 'phone vibration buzz', 'door knock', "
        "'keyboard typing burst', 'camera shutter click', 'glass shatter', 'server rack power on'. "
        "Ставь его ТОЛЬКО когда в кадре происходит конкретное физическое действие, у которого есть "
        "естественный звук, и этот звук усиливает момент. В большинстве кадров оставляй пустую строку — "
        "молчание это нормальный и правильный ответ. Ориентир: не больше ОДНОГО акцента на сцену, "
        "и в среднем 1 акцент на 4-6 кадров по всему ролику.\n"
        "- sfx_query — ФОНОВАЯ атмосфера места (непрерывная, зацикливаемая): 'server room fan hum', "
        "'city traffic ambience', 'waves crashing on rocks', 'quiet forest birds', 'rain on window'. "
        "Это РЕДКОЕ поле: заполняй его только для сцен с ЯРКО выраженной звуковой средой, которая сама "
        "по себе несёт смысл (шторм, море, оживлённая улица, цех, лес). Для нейтральных сцен — "
        "кабинет, стол, интерфейс, схема, портрет — оставляй пустую строку. По умолчанию под роликом "
        "играет музыка, и лишний фон под ней всё равно не слышен, только съедает время генерации.\n"
        "- Соседние кадры одной локации должны иметь ОДИНАКОВЫЙ sfx_query, чтобы фон не дёргался.\n"
        "- В sfx_accent_query не пиши атмосферу (hum, ambience, rain), а в sfx_query не пиши события "
        "(knock, click, beep): это разные слои, и перепутанные значения будут отброшены.\n"
        "- Ни в одном звуковом поле не должно быть музыки, речи и закадрового голоса.\n"
        "- Любые схемы, маршруты, карты и технические объяснения показывай как реалистичный AI-видеоряд.\n"
        "- Не выдумывай факты, работай только с текстом сегментов.\n"
        "- Таймкод кадра: start = первого сегмента в группе, end = последнего.\n"
    )


def request_lm_shots(
    base_url: str,
    model: str,
    prompt: str,
    ttl_seconds: int | None = None,
) -> list[dict]:
    """Request one shot array through the canonical LM Studio client."""

    settings = _LmSettings(
        lm_url=(base_url or lm.DEFAULT_URL).rstrip("/"),
        lm_model=model.strip() or "local-model",
    )
    content = lm.chat(
        settings,
        [
            {
                "role": "system",
                "content": (
                    "Отвечай только валидным JSON. Не добавляй markdown. "
                    "Описывай только буквальные физические сцены, без метафор и символов."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.25,
        timeout=300,
        ttl=ttl_seconds,
    )
    parsed = parse_lm_json_array(content, "LM Studio frame list")
    if not all(isinstance(item, dict) for item in parsed):
        raise ValueError("LM Studio frame list должен содержать JSON-объекты")
    return parsed


def enforce_numeric_screen_text(
    shots: list[Shot],
    scenes: list[Scene],
    log_callback: Callable[[str], None] | None = None,
) -> None:
    """Keep at most one narration-supported numeric overlay per scene."""

    scene_text = {scene.index: scene.voice_text for scene in scenes}
    used_scenes: set[int] = set()
    removed = 0
    for shot in shots:
        candidate = normalize_numeric_screen_text(
            scene_text.get(shot.scene_index, ""),
            shot.screen_text,
        )
        if candidate and shot.scene_index not in used_scenes:
            shot.screen_text = candidate
            used_scenes.add(shot.scene_index)
        else:
            if (shot.screen_text or "").strip():
                removed += 1
            shot.screen_text = ""
    if removed and log_callback:
        log_callback(
            f"Экранные надписи: удалено {removed} нечисловых, неподтверждённых "
            "или повторных надписей."
        )


def _shot_creative_text(shot: Shot) -> str:
    """Every field that can carry a visual motif into the finished frame.

    video_prompt and fallback were previously excluded, so a motif could pass
    the critics through those fields: on a 45-scene film the audit saw screens
    in 14 scenes while they were actually present in 36.
    """

    return " ".join(
        str(value or "").lower()
        for value in (
            shot.description,
            shot.asset_query,
            shot.image_prompt,
            shot.video_prompt,
            shot.fallback,
        )
    )


def _matching_motifs(shots: list[Shot]) -> dict[str, list[Shot]]:
    matches: dict[str, list[Shot]] = {}
    for label, markers in _CLICHE_MOTIFS.items():
        matches[label] = [
            shot
            for shot in shots
            if any(marker in _shot_creative_text(shot) for marker in markers)
        ]
    return matches


def audit_scene_shots_against_visual_plan(
    shots: list[Shot],
    strategy: GlobalVisualPlanItem,
    forbidden_motifs: set[str] | None = None,
) -> list[str]:
    """Reject a scene that ignores its assigned visual grammar."""

    if not shots:
        return ["сцена не содержит кадров"]
    violations: list[str] = []
    positive_plan = " ".join(
        (
            strategy.location,
            strategy.primary_subject,
            strategy.shot_progression,
        )
    ).lower()
    avoid_plan = strategy.avoid.lower()
    globally_forbidden = forbidden_motifs or set()
    for label, matching in _matching_motifs(shots).items():
        if not matching:
            continue
        markers = _CLICHE_MOTIFS[label]
        explicitly_planned = any(marker in positive_plan for marker in markers)
        explicitly_forbidden = any(marker in avoid_plan for marker in markers)
        ratio = 0.50 if explicitly_planned else 0.25
        cap = max(1, math.ceil(len(shots) * ratio))
        if label in globally_forbidden or explicitly_forbidden or len(matching) > cap:
            reason = (
                "глобальный лимит этого мотива уже исчерпан"
                if label in globally_forbidden
                else (
                    "этот мотив указан в avoid"
                    if explicitly_forbidden
                    else f"допустимо максимум {cap}"
                )
            )
            violations.append(
                f"{label}: {len(matching)} из {len(shots)} кадров, {reason}"
            )

    seen: dict[tuple[str, str], int] = {}
    for shot in shots:
        key = (
            " ".join((shot.description or "").lower().split()),
            " ".join((shot.asset_query or "").lower().split()),
        )
        if not any(key):
            continue
        seen[key] = seen.get(key, 0) + 1
    duplicates = [key for key, count in seen.items() if count > 1]
    if duplicates:
        violations.append(
            f"повторяются {len(duplicates)} одинаковых визуальных концепта"
        )
    return violations


def audit_final_shot_list(
    shots: list[Shot],
    scenes: list[Scene],
    *,
    include_creative: bool = True,
) -> list[str]:
    """Audit the actual edit plan after all LM and deterministic passes.

    Timeline and structural violations are blocking because they can produce a
    broken export.  Creative variety is useful feedback, but it must not throw
    away a complete shot list after a long LM run.
    """

    if not shots:
        return ["финальный шот-лист пуст"]
    violations: list[str] = []
    ordered = sorted(shots, key=lambda shot: timecode_to_seconds(shot.start))
    for previous, current in zip(ordered, ordered[1:]):
        previous_end = timecode_to_seconds(previous.end)
        current_start = timecode_to_seconds(current.start)
        if previous_end != current_start:
            violations.append(
                f"между кадрами {previous.index} и {current.index} "
                f"разрыв/пересечение {current_start - previous_end}с"
            )

    scene_ranges = {
        scene.index: (
            timecode_to_seconds(scene.start),
            timecode_to_seconds(scene.end),
        )
        for scene in scenes
    }
    for scene_index, (scene_start, scene_end) in scene_ranges.items():
        scene_shots = [
            shot for shot in ordered if shot.scene_index == scene_index
        ]
        if not scene_shots:
            violations.append(f"сцена {scene_index} не имеет кадров")
            continue
        actual = (
            timecode_to_seconds(scene_shots[0].start),
            timecode_to_seconds(scene_shots[-1].end),
        )
        if actual != (scene_start, scene_end):
            violations.append(
                f"кадры сцены {scene_index} покрывают {actual}, "
                f"ожидалось {(scene_start, scene_end)}"
            )

    for shot in ordered:
        duration = timecode_to_seconds(shot.end) - timecode_to_seconds(shot.start)
        minimum = 2 if (shot.visual_type or "").lower() == "stock_video" else 3
        if len([item for item in ordered if item.scene_index == shot.scene_index]) > 1 and duration < minimum:
            violations.append(
                f"кадр {shot.index} длится {duration}с; для "
                f"{shot.visual_type or 'generated'} нужно минимум {minimum}с"
            )

    for previous, current in zip(ordered, ordered[1:]):
        if (
            previous.scene_index == current.scene_index
            and " ".join((previous.description or "").lower().split())
            == " ".join((current.description or "").lower().split())
            and " ".join((previous.asset_query or "").lower().split())
            == " ".join((current.asset_query or "").lower().split())
        ):
            violations.append(
                f"кадры {previous.index} и {current.index} дублируют один концепт"
            )

    if include_creative:
        violations.extend(audit_final_visual_variety(ordered, scenes))
    return violations


def audit_final_visual_variety(
    shots: list[Shot],
    scenes: list[Scene],
) -> list[str]:
    """Return non-blocking whole-film visual repetition warnings."""

    warnings: list[str] = []
    motif_cap = max(2, math.ceil(len(scenes) * 0.25))
    motif_matches = _matching_motifs(shots)
    for label, matching in motif_matches.items():
        matching_scenes = sorted({shot.scene_index for shot in matching})
        if len(matching_scenes) > motif_cap:
            warnings.append(
                f"{label} возвращаются в {len(matching_scenes)} сценах "
                f"{matching_scenes}; максимум {motif_cap}"
            )
    return warnings


def call_lm_studio_for_shots(
    base_url: str,
    model: str,
    scenes: list[Scene],
    image_preference: str = "Auto",
    ttl_seconds: int | None = None,
    segments: list | None = None,
    on_scene_shots: Callable[[list[Shot]], None] | None = None,
    style_anchor: str = "",
    character_description: str = "",
    niche: str = "documentary",
    video_style: str = DEFAULT_VIDEO_STYLE,
    log_callback: Callable[[str], None] | None = None,
    hybrid: bool = False,
    route_priority: str = "balanced",
    ai_route_mix: bool = False,
    reference_density: str | None = "normal",
    on_visual_plan: Callable[[list[dict[str, object]]], None] | None = None,
) -> list[Shot]:
    """Plan, repair, route and style a shot list without reading global config.

    ``reference_density`` defaults to the historical effective value ``normal``.
    Callers that expose a user setting must pass their already-loaded snapshot;
    this function deliberately never calls ``load_settings``.
    """

    del niche  # preserved for call-site compatibility
    selected_style = get_video_style(video_style)
    route_priority = normalize_route_priority(route_priority)
    density = (reference_density or "normal").strip().lower()
    instructions = build_shot_instructions(
        image_preference=image_preference,
        style_anchor=style_anchor,
        video_style=video_style,
        hybrid=hybrid,
        route_priority=route_priority,
        ai_route_mix=ai_route_mix,
        reference_density=density,
    )
    global_visual_plan = create_global_visual_plan(
        base_url,
        model,
        scenes,
        ttl_seconds=ttl_seconds,
        video_style=video_style,
        style_anchor=style_anchor,
        character_description=character_description,
        log_callback=log_callback,
    )
    visual_plan_by_scene = {
        item.scene_index: item for item in global_visual_plan
    }
    if on_visual_plan:
        on_visual_plan([item.as_dict() for item in global_visual_plan])

    def _request(prompt: str) -> list[dict]:
        return request_lm_shots(
            base_url,
            model,
            prompt,
            ttl_seconds=ttl_seconds,
        )

    if segments and hasattr(segments[0], "start") and hasattr(segments[0], "text"):
        all_shots: list[Shot] = []
        recent_tail: list[str] = []
        location_bank: list[str] = []
        concept_bank: list[str] = []
        motif_scene_cap = max(2, math.ceil(len(scenes) * 0.25))
        motif_scene_counts = {label: 0 for label in _CLICHE_MOTIFS}
        global_scene_map = "\n".join(
            (
                f"  · {scene.index}. {scene.start}–{scene.end} | {scene.title} | "
                f"{visual_plan_by_scene[scene.index].visual_family} | "
                f"{visual_plan_by_scene[scene.index].location} | "
                f"{visual_plan_by_scene[scene.index].primary_subject}"
            )
            for scene in scenes
        )
        first_scene_index = scenes[0].index if scenes else None
        for scene_position, scene in enumerate(scenes, start=1):
            scene_start = timecode_to_seconds(scene.start)
            scene_end = timecode_to_seconds(scene.end)
            scene_segments = [
                segment
                for segment in segments
                if scene_start <= timecode_to_seconds(segment.start) < scene_end
            ]
            if not scene_segments:
                continue
            segment_lines = "\n".join(
                f'  [{index}] {segment.start} - {segment.end} | "{segment.text}"'
                for index, segment in enumerate(scene_segments, start=1)
            )
            style_line = (
                f"ОБЯЗАТЕЛЬНЫЙ ВИЗУАЛЬНЫЙ СТИЛЬ: {selected_style.name}. "
                "Каждый image_prompt и video_prompt должен подчиняться этому стилю. "
                "Никаких LoRA и trigger-фраз, только текстовый prompt.\n"
            )
            character_line = (
                f"Персонаж: {character_description}\n"
                if character_description
                else ""
            )
            memory_block = ""
            if recent_tail:
                memory_block += (
                    "ПОСЛЕДНИЕ КАДРЫ ПРЕДЫДУЩЕЙ СЦЕНЫ (для монтажного стыка):\n"
                    + "\n".join(f"  - {text}" for text in recent_tail)
                    + "\nПервый кадр ЭТОЙ сцены не должен повторять план и субъект "
                    "последнего кадра выше — смени крупность или ракурс.\n"
                )
            if location_bank:
                memory_block += (
                    "УЖЕ ПОКАЗАННЫЕ ЛОКАЦИИ РОЛИКА (если действие происходит там же — "
                    "описывай локацию ТЕМИ ЖЕ словами, не выдумывай её заново):\n"
                    + "\n".join(f"  - {location}" for location in location_bank[-8:])
                    + "\n"
                )
            if concept_bank:
                memory_block += (
                    "УЖЕ ИСПОЛЬЗОВАННЫЕ ВИЗУАЛЬНЫЕ КОНЦЕПТЫ РОЛИКА "
                    "(не повторяй без сюжетной необходимости):\n"
                    + "\n".join(f"  - {concept}" for concept in concept_bank[-20:])
                    + "\n"
                )
            if memory_block:
                memory_block += "\n"

            hook_block = ""
            if scene.index == first_scene_index:
                hook_block = (
                    "ЭТО ПЕРВАЯ СЦЕНА — ХУК РОЛИКА. Первые кадры решают, останется ли зритель:\n"
                    "- Первый кадр — самый сильный, интригующий визуал сцены, СРАЗУ по сути. НЕ нейтральная establishing-заставка.\n"
                    "- В первых кадрах обязательно движение или действие.\n"
                    "- Поставь priority=high первым 2-3 кадрам.\n\n"
                )
            scene_length = max(1, scene_end - scene_start)
            # Density drives the whole cost of a run: every shot needs its own
            # image AND its own video, and roughly a third of LTX renders come
            # back frozen.  Dividing by SHOT_TARGET_SECONDS with a cap of 24
            # produced 468 shots for a 35-minute film, which no unattended run
            # can finish.  The pre-refactor rule (~1-6 shots per scene) keeps a
            # documentary cutting rhythm at a third of the generation cost;
            # split_long_shot() still breaks up anything genuinely too long.
            recommended = max(1, min(SHOT_MAX_PER_SCENE, round(scene_length / 5)))
            consistency_line = (
                " - Сохраняй консистентность: персонаж/объект/стиль одинаковы во всех кадрах\n"
                if style_anchor or character_description
                else ""
            )
            strategy = visual_plan_by_scene[scene.index]
            # Spend the motif budget evenly across the film instead of
            # first-come-first-served. Previously the opening scenes could burn
            # the entire allowance, after which late scenes were rejected for a
            # single cutaway they legitimately needed.
            allowed_so_far = max(
                1,
                math.ceil(motif_scene_cap * scene_position / max(1, len(scenes))),
            )
            forbidden_motifs = {
                label
                for label, count in motif_scene_counts.items()
                if count >= min(motif_scene_cap, allowed_so_far)
            }
            motif_budget_block = ""
            if forbidden_motifs:
                motif_budget_block = (
                    "ГЛОБАЛЬНЫЙ ЛИМИТ ИСЧЕРПАН — в этой и следующих сценах "
                    "полностью запрещены мотивы: "
                    + ", ".join(sorted(forbidden_motifs))
                    + ". Не показывай их даже одним перебивочным кадром.\n"
                )
            numeric_screen_text = normalize_numeric_screen_text(
                scene.voice_text,
                scene.screen_text,
            )
            prompt = (
                "Ты visual director для YouTube-ролика.\n"
                f'Сцена {scene.index}: "{scene.title}"\n'
                f"Направление: {scene.visual_direction}\n"
                f"Разрешённая числовая надпись: {numeric_screen_text or '[нет]'}\n"
                "ОБЯЗАТЕЛЬНАЯ ГЛОБАЛЬНАЯ СТРАТЕГИЯ ЭТОЙ СЦЕНЫ:\n"
                f"{strategy.prompt_block()}\n"
                "Кадры обязаны реализовать эту стратегию; не заменяй её привычными "
                "серверами, мониторами, графиками или трейдерами.\n"
                f"{motif_budget_block}"
                "ГЛОБАЛЬНАЯ КАРТА РОЛИКА. Текущая сцена — часть одного "
                "последовательного фильма; учитывай предыдущие и будущие темы:\n"
                f"{global_scene_map}\n\n"
                f"{style_line}{character_line}\n"
                f"{memory_block}{hook_block}"
                f"Сегменты этой сцены ({len(scene_segments)} шт.):\n{segment_lines}\n\n"
                "ШАГ 1 — РАЗВЕРНИ текст в полную визуальную сцену:\n"
                "  Из короткой фразы сделай конкретную сцену: какая локация, время суток, "
                "освещение, кто/что в кадре, какие материалы и текстуры, какая атмосфера.\n"
                "  Пример: 'он просыпается и берёт телефон' → спальня, утро, солнечный "
                "свет сквозь шторы, мужчина 30 лет в футболке тянется к телефону на "
                "тумбочке, мятое одеяло, деревянная мебель, тёплый свет.\n\n"
                "ШАГ 2 — создай кадры с РАЗНЫМИ ракурсами:\n"
                "- Каждый кадр = уникальная визуальная идея, пригодная для короткой image-to-video анимации\n"
                "- Чередуй shot types (общий → деталь → средний → неожиданный)\n"
                f"{consistency_line}"
                "- Не повторяй один концепт дважды в одной сцене\n"
                "- Не пытайся покрыть каждый сегмент — покрывай каждую СМЫСЛОВУЮ ЕДИНИЦУ\n"
                f"- Эта сцена длится {scene_length}с — сделай примерно {recommended} кадр(а/ов), ±1. "
                f"Не делай кадры длиннее {SHOT_MAX_SECONDS}с (LTX limit): лучше больше коротких, чем один длинный\n"
                "- Порядок кадров: от общего к частному или по логике повествования\n\n"
                f"{instructions}"
            )

            scene_shots: list[Shot] | None = None
            weak_motion = False
            creative_feedback = ""
            creative_retry_used = False
            for attempt in range(3):
                try:
                    extra = ""
                    if attempt >= 1:
                        extra = (
                            "\n\nВЕРНИ СТРОГО валидный JSON-массив и НИЧЕГО больше: "
                            "без markdown, без текста до или после массива, все строки в кавычках."
                        )
                        if weak_motion:
                            extra += (
                                "\n\nВАЖНО про video_prompt: в КАЖДОМ кадре опиши конкретное "
                                "движение, подходящее именно этому кадру — одно маленькое действие "
                                "субъекта, если в кадре есть кто/что действует, иначе движение среды "
                                "(свет, ткань, вода, частицы) плюс лёгкое движение камеры. Не оставляй "
                                "поле пустым и не пиши общими фразами."
                            )
                        if creative_feedback:
                            extra += (
                                "\n\nПРЕДЫДУЩАЯ РАСКАДРОВКА ОТКЛОНЕНА. "
                                "Полностью исправь нарушения, сохранив тайминг и "
                                "глобальную стратегию сцены:\n- "
                                + "\n- ".join(creative_feedback.splitlines())
                            )
                    parsed = parse_shots(_request(prompt + extra), offset=len(all_shots))
                    if parsed:
                        if attempt == 0 and motions_mostly_weak(parsed):
                            scene_shots = parsed
                            weak_motion = True
                            continue
                        creative_violations = audit_scene_shots_against_visual_plan(
                            parsed,
                            strategy,
                            forbidden_motifs=forbidden_motifs,
                        )
                        if creative_violations:
                            creative_feedback = "\n".join(creative_violations)
                            can_retry = not creative_retry_used and attempt < 2
                            if can_retry and log_callback:
                                log_callback(
                                    f"Сцена {scene.index}: раскадровка отклонена — "
                                    + "; ".join(creative_violations)
                                )
                            if can_retry:
                                creative_retry_used = True
                                continue
                            if log_callback:
                                log_callback(
                                    f"Сцена {scene.index}: после повторной попытки "
                                    "остались творческие замечания; принимаю лучший "
                                    "вариант и продолжу, чтобы не потерять весь результат — "
                                    + "; ".join(creative_violations)
                                )
                        scene_shots = parsed
                        break
                except Exception:
                    pass

            if not scene_shots:
                if log_callback:
                    log_callback(
                        f'Сцена {scene.index} "{scene.title}": LLM вернул битый JSON — '
                        "использую запасную раскадровку."
                    )
                scene_shots = build_heuristic_shots(
                    [scene],
                    image_preference=image_preference,
                    style_anchor=style_anchor,
                    video_style=video_style,
                    character_description=character_description,
                    compose=False,
                )

            for shot in scene_shots:
                shot.scene_index = scene.index
            split_shots: list[Shot] = []
            for shot in scene_shots:
                split_shots.extend(
                    split_long_shot(shot, scene, scene_segments)
                )
            for index, shot in enumerate(split_shots):
                shot.index = len(all_shots) + index + 1
            all_shots.extend(split_shots)
            scene_motifs = _matching_motifs(split_shots)
            for label, matching in scene_motifs.items():
                if matching:
                    motif_scene_counts[label] += 1

            recent_tail = [
                (shot.description or shot.image_prompt)[:110].strip()
                for shot in split_shots[-2:]
                if shot.description or shot.image_prompt
            ]
            for shot in split_shots:
                first_sentence = (shot.image_prompt or "").split(".")[0].strip()[:120]
                if first_sentence and "continuation shot" not in first_sentence.lower():
                    key = first_sentence.lower()
                    if all(key != location.lower() for location in location_bank):
                        location_bank.append(first_sentence)
                concept = " — ".join(
                    value.strip()
                    for value in (shot.description, shot.asset_query)
                    if value and value.strip()
                )[:160]
                concept_key = concept.lower()
                if concept and all(
                    concept_key != existing.lower()
                    for existing in concept_bank
                ):
                    concept_bank.append(concept)
            if on_scene_shots:
                on_scene_shots(split_shots)
        shots = all_shots
    else:
        compact_scenes = [
            {
                "scene_index": scene.index,
                "start": scene.start,
                "end": scene.end,
                "title": scene.title,
                "voice_text": scene.voice_text,
                "visual_direction": scene.visual_direction,
                "screen_text": normalize_numeric_screen_text(
                    scene.voice_text,
                    scene.screen_text,
                ),
                "global_visual_strategy": visual_plan_by_scene[scene.index].as_dict(),
            }
            for scene in scenes
        ]
        prompt = (
            "Ты visual director для документального YouTube-ролика. На основе сцен "
            "создай shot list: конкретные кадры видеоряда длительностью 3-5 секунд.\n"
            "Главный принцип: один shot = один визуальный прием. Для документального "
            "стиля нужны частые смены кадров — минимум 2-3 кадра на каждые 10 секунд сцены.\n"
            f"{instructions}"
            f"Сцены JSON:\n{json.dumps(compact_scenes, ensure_ascii=False)}"
        )
        shots = parse_shots(_request(prompt))

    shots = finalize_shot_timeline(shots, scenes, segments)
    shots = collapse_redundant_shot_runs(
        shots,
        log_callback=log_callback,
    )
    enforce_numeric_screen_text(shots, scenes, log_callback=log_callback)
    diversify_repeated_shots(shots, log_callback=log_callback)
    if hybrid:
        enforce_route_budget(
            shots,
            route_priority,
            log_callback=log_callback,
        )
    elif ai_route_mix:
        enforce_ai_route_budget(
            shots,
            route_priority,
            log_callback=log_callback,
        )
    normalize_shot_routes(shots, image_preference=image_preference)
    shots = enforce_minimum_shot_durations(
        shots,
        scenes,
        log_callback=log_callback,
    )
    repair_shot_prompts(shots)
    # Sound follows the location, not the cut: one ambience per scene,
    # event-like queries dropped (the export's room tone covers the rest).
    consolidate_sfx_queries(shots, log_callback=log_callback)
    final_violations = audit_final_shot_list(
        shots,
        scenes,
        include_creative=False,
    )
    if final_violations:
        raise ValueError(
            "Финальный аудит видеоряда отклонил шот-лист. "
            "Старый результат сохранён. Нарушения: "
            + "; ".join(final_violations[:12])
        )
    creative_warnings = audit_final_visual_variety(shots, scenes)
    if creative_warnings and log_callback:
        log_callback(
            "Финальный творческий контроль: "
            + "; ".join(creative_warnings[:6])
            + ". Это не ломает таймлайн: шот-лист сохранён, замечания можно "
            "исправить точечно."
        )
    for shot in shots:
        shot.image_prompt = compose_image_prompt(
            video_style,
            shot.image_prompt or shot.description,
            character_description=character_description,
            custom_notes=style_anchor,
        )
        shot.video_prompt = compose_video_prompt(
            video_style,
            shot.video_prompt or shot.motion or make_ltx_prompt(shot.image_prompt),
            shot_description=shot.description,
        )
    return shots


# Private-name aliases keep the desktop's existing helper imports trivial while
# the core API exposes readable public names for new call sites.
_suggest_shot_type = suggest_shot_type
_suggest_motion = suggest_motion
_motion_is_weak = motion_is_weak
_motions_mostly_weak = motions_mostly_weak


__all__ = [
    "GLOBAL_VISUAL_FAMILIES",
    "GlobalVisualPlanItem",
    "SHOT_MAX_SECONDS",
    "SHOT_TARGET_SECONDS",
    "audit_final_shot_list",
    "audit_final_visual_variety",
    "audit_global_visual_plan",
    "audit_scene_shots_against_visual_plan",
    "build_global_visual_plan_prompt",
    "build_heuristic_shots",
    "build_reference_block",
    "build_route_block",
    "build_shot_instructions",
    "call_lm_studio_for_shots",
    "clamp_shots_to_scenes",
    "clean_lm_json_content",
    "clean_visual_prompt_source",
    "continuation_shot",
    "create_global_visual_plan",
    "choose_visual_route",
    "enforce_numeric_screen_text",
    "finalize_shot_timeline",
    "make_asset_query",
    "make_ltx_prompt",
    "make_realistic_prompt",
    "make_shot_description",
    "motion_is_weak",
    "motions_mostly_weak",
    "normalize_shot_routes",
    "normalize_route_priority",
    "parse_global_visual_plan",
    "parse_lm_json_array",
    "parse_shots",
    "preferred_realistic_image_model",
    "prompt_has_too_much_transcript",
    "rebuild_image_prompt_for_shot",
    "repair_shot_prompts",
    "request_global_visual_plan",
    "request_lm_shots",
    "retile_shots_contiguous",
    "split_long_shot",
    "suggest_motion",
    "suggest_shot_type",
    "tiled_intervals",
]
