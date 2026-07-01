from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class VideoStyle:
    id: str
    name: str
    description: str
    scene_brief: str
    base_prompt: str
    character_rules: str
    shot_planning_rules: list[str]
    visual_translation_rules: list[str]
    negative_prompt: str
    example_bad: str = ""
    example_good: str = ""

    def to_public_dict(self) -> dict:
        return asdict(self)


KNITTED = VideoStyle(
    id="knitted",
    name="Вязанный",
    description=(
        "Вязаная stop-motion диорама: amigurumi-персонажи, видимые петли, "
        "шерстяные волокна, миниатюрные handmade-декорации и мягкий макро-свет."
    ),
    scene_brief=(
        "Показывай РЕАЛЬНОЕ узнаваемое место, объект или сцену из текста — но целиком "
        "связанное из пряжи: вязаные здания, техника, предметы и окружение, видимые петли "
        "и текстура вязания, а люди — amigurumi-куклы. НЕ превращай тему в безликую "
        "абстрактную миниатюру и не уходи в сплошное макро — зритель должен УЗНАВАТЬ "
        "конкретную локацию (улица города, серверная, корабль, биржа, кабинет, побережье и т.п.) "
        "и понимать действие даже без озвучки. Реальные ориентиры и объекты должны быть "
        "распознаваемы, просто в вязаном исполнении. Без символов и метафор — конкретная сцена."
    ),
    base_prompt=(
        "A recognizable real-world location rendered entirely in crochet and knitted yarn: "
        "amigurumi characters, knitted buildings, vehicles, props and environment, visible "
        "stitches and soft wool fibers, tactile textile surfaces, the real place clearly "
        "identifiable by its key landmarks, soft cinematic lighting, mostly wide and medium "
        "framing, handmade stop-motion look, 16:9 frame."
    ),
    character_rules=(
        "If a recurring character is present, keep the same amigurumi proportions, yarn colors, "
        "face stitching, outfit colors, and silhouette in every shot."
    ),
    shot_planning_rules=[
        "Use 1-5 concrete shots per semantic scene.",
        "Favor establishing and medium shots that clearly show the REAL, recognizable place; use close-ups sparingly, NOT as the default. Avoid defaulting to extreme macro of textile.",
        "Each shot depicts a recognizable real location or object from the script — just knitted.",
        "Keep each shot simple: one clear action and 1-3 main objects, in an identifiable setting.",
        "Represent conflict as tangled yarn, frayed stitches, dimmer light, or uneven fabric — but inside a recognizable real scene.",
        "Represent progress as neat stitching, brighter warm light — inside a recognizable real scene.",
    ],
    visual_translation_rules=[
        "Humans become amigurumi dolls doing ONE clear action in a real, recognizable setting.",
        "Animals become crocheted plush figures.",
        "Real places (a city street, a server room, a cable-laying ship, a stock exchange, an office, a coastline) become the SAME place, still recognizable by its landmarks, but knitted: yarn buildings, woolen vehicles, stitched signage shapes.",
        "Objects and machines keep their real, recognizable form, made of yarn — do not abstract them into generic textile blobs.",
        "Abstract ideas: pick ONE concrete, recognizable scene that shows the idea through action with named objects.",
        "Emotions: show via a clear pose and stitched facial expression in a concrete situation.",
    ],
    negative_prompt=(
        "Avoid photorealistic human skin, smooth plastic, glossy CGI, flat digital cartoon, "
        "realistic face, metallic surfaces, latex, readable text, logos, extra limbs, distorted hands."
    ),
    example_bad="Хрупкость интернета, показанная распускающейся нитью (абстракция — так НЕ делай).",
    example_good=(
        "Вязаная рука осторожно держит одну тонкую светящуюся нить над миниатюрной "
        "океанской диорамой, кончик нити начинает распускаться."
    ),
)


VIDEO_STYLES: dict[str, VideoStyle] = {
    KNITTED.id: KNITTED,
}


DEFAULT_VIDEO_STYLE = "knitted"


def get_video_style(style_id: str | None) -> VideoStyle:
    return VIDEO_STYLES.get((style_id or "").strip().lower(), VIDEO_STYLES[DEFAULT_VIDEO_STYLE])


def list_video_styles() -> list[dict]:
    return [style.to_public_dict() for style in VIDEO_STYLES.values()]


def style_director_brief(style_id: str | None, custom_notes: str = "") -> str:
    style = get_video_style(style_id)
    lines = [
        f"ВЫБРАННЫЙ СТИЛЬ: {style.name}",
        style.scene_brief,
        "ПРАВИЛО КОНКРЕТНОСТИ (важнее стиля):",
        "- Каждый кадр — буквальная физическая сцена: конкретный субъект + одно понятное действие + место + 1-3 предмета.",
        "- Запрещены метафоры и слова: символизирует, олицетворяет, представляет, концепция, abstract, represents, symbolizes, metaphor.",
        "- Зритель должен понять кадр без дикторского текста. Если не можешь представить кадр буквально — выбери более простую конкретную сцену.",
        "Правила планирования кадров:",
        *[f"- {rule}" for rule in style.shot_planning_rules],
        "Правила визуального перевода:",
        *[f"- {rule}" for rule in style.visual_translation_rules],
    ]
    if style.example_bad or style.example_good:
        lines.append("Пример конкретности:")
        if style.example_bad:
            lines.append(f"- ПЛОХО (абстрактно): {style.example_bad}")
        if style.example_good:
            lines.append(f"- ХОРОШО (конкретно): {style.example_good}")
    if custom_notes.strip():
        lines.extend(["Дополнительные заметки пользователя:", custom_notes.strip()])
    return "\n".join(lines)


def compose_image_prompt(
    style_id: str | None,
    shot_prompt: str,
    character_description: str = "",
    custom_notes: str = "",
) -> str:
    style = get_video_style(style_id)
    body = " ".join((shot_prompt or "").split()).strip() or "A simple handmade miniature scene."
    # POSITIVE prompt only: style look + concrete scene + character. We do NOT
    # append any negative text here — Krea Turbo zeroes the negative via
    # ConditioningZeroOut, so negative words in the positive prompt only pull the
    # model toward what it should avoid. Prompt-writing rules live in the LLM shot
    # prompt (INSTRUCTIONS), not in the image prompt.
    parts = [
        style.base_prompt,
        f"Shot: {body}",
    ]
    if character_description.strip():
        parts.append(f"Character consistency: {character_description.strip()}. {style.character_rules}")
    if custom_notes.strip():
        parts.append(f"Additional style notes: {custom_notes.strip()}")
    return "\n\n".join(parts)


def compose_video_prompt(style_id: str | None, motion_prompt: str, shot_description: str = "") -> str:
    # POSITIVE video prompt only: the motion + a short positive style cue + shot
    # context. We don't append "avoid morphing/melting/extra limbs" — those are
    # negative-style instructions that only pull the model toward those failures
    # when placed in a positive prompt.
    motion = " ".join((motion_prompt or "").split()).strip()
    if not motion:
        motion = "The camera slowly pushes in while the handmade subject makes a small gentle movement."
    shot = " ".join((shot_description or "").split()).strip()
    parts = [
        motion,
        "Animation style: subtle handmade stop-motion motion, gentle puppet-like movement, stable knit textures.",
    ]
    if shot:
        parts.append(f"Shot context: {shot}")
    return " ".join(parts)
