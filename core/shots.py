"""Deterministic post-processing of the LLM-generated shot list.

Strangler slice #2 out of content_pipeline_gui.py (see docs/DEVELOPMENT.md).
The philosophy of this module: the LLM proposes, code enforces. Textual route
biases and framing rules in the prompt are advisory at best — models
"alternate" routes by shot COUNT while expensive AI shots stay long, and they
repeat framings across scene boundaries. These passes make the guarantees
real, deterministically and idempotently.

All functions are duck-typed over shot objects (attribute access only), so the
monolith's Shot dataclass and core.models.Shot both work.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import math
import random
import re

from core.models import Shot, seconds_to_timecode, timecode_to_seconds

# Max share of RUNTIME that ai_image_then_i2v may occupy per route priority
# (hybrid mode: stock / kenburns / ai). None = no cap.
ROUTE_AI_RUNTIME_CAP: dict[str, float | None] = {"economy": 0.20, "balanced": 0.45, "rich": None}

# AI mode (no stock): max share of runtime that LTX animation may occupy.
AI_MIX_RUNTIME_CAP: dict[str, float | None] = {"economy": 0.30, "balanced": 0.55, "rich": None}

# More than this many consecutive Ken Burns stills reads as a slideshow.
MAX_KENBURNS_RUN = 3

# Sound is spot work, not wall-to-wall coverage. Under narration and a music
# bed a sustained ambience is effectively inaudible, while short transients cut
# through — so ambience beds are capped hard and the accent layer carries the
# audible detail. The cap also bounds generation time: each bed is a separate
# model run, and a long video would otherwise queue dozens of them for nothing.
MAX_AMBIENCE_BEDS = 5
# At most one accent per scene: two events inside one scene fight each other.
MAX_ACCENTS_PER_SCENE = 1

# A one-second generated shot cannot establish readable motion and wastes an
# image/video generation.  Stock may be used as a deliberate two-second
# accent; generated and reference material needs at least three seconds.
MIN_STOCK_SHOT_SECONDS = 2
MIN_GENERATED_SHOT_SECONDS = 3

_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _snap_points_from_segments(segments) -> list[int]:
    """Return sorted Whisper segment end-times suitable for edit-point snapping."""
    points: set[int] = set()
    for segment in segments or []:
        try:
            points.add(
                timecode_to_seconds(
                    getattr(segment, "end", "") or segment["end"]
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return sorted(points)


def _cut_bounds(
    start: int,
    end: int,
    limit: int,
    snap: list[int] | None = None,
    seed: int = 0,
    variable: bool = True,
) -> list[int]:
    """Build deterministic cut points for one timeline slot."""
    limit = max(1, int(limit))
    duration = end - start
    if duration <= limit:
        return [start, end]
    if not variable:
        pieces = max(1, math.ceil(duration / limit))
        return [start + round(duration * index / pieces) for index in range(pieces + 1)]

    rng = random.Random(seed)
    minimum = max(2, int(round(limit * 0.5)))
    bounds = [start]
    position = start
    while end - position > limit:
        target = position + rng.randint(minimum, limit)
        if end - target < minimum:
            target = end - minimum
        if snap:
            window = [point for point in snap if position + minimum <= point <= position + limit]
            if window:
                nearest = min(window, key=lambda point: abs(point - target))
                if abs(nearest - target) <= 2:
                    target = nearest
        if target <= position:
            target = position + minimum
        bounds.append(target)
        position = target
    bounds.append(end)
    return bounds


def _split_shot_slot(
    shot: Shot,
    limit: int,
    index: int,
    output: list[Shot],
    snap: list[int] | None,
    variable: bool,
    force_stock: bool,
) -> int:
    start = timecode_to_seconds(shot.start)
    end = timecode_to_seconds(shot.end)
    if end <= start:
        end = start + 1
    bounds = _cut_bounds(
        start,
        end,
        limit,
        snap=snap,
        seed=shot.index * 7919 + start,
        variable=variable,
    )
    for cut_start, cut_end in zip(bounds, bounds[1:]):
        if cut_end <= cut_start:
            cut_end = cut_start + 1
        data = dict(vars(shot))
        data.update(
            index=index,
            start=seconds_to_timecode(cut_start),
            end=seconds_to_timecode(cut_end),
            image_model="",
            video_model="",
        )
        if force_stock:
            data["visual_type"] = "stock_video"
        output.append(Shot(**data))
        index += 1
    return index


def split_shots_for_stock(
    shots: list[Shot],
    max_seconds: int = 5,
    segments=None,
    variable: bool = True,
) -> list[Shot]:
    """Convert every shot to stock and split it into bounded edit slots."""
    limit = max(1, int(max_seconds or 5))
    snap = _snap_points_from_segments(segments)
    output: list[Shot] = []
    index = 1
    for shot in shots:
        index = _split_shot_slot(
            shot,
            limit,
            index,
            output,
            snap,
            variable,
            force_stock=True,
        )
    return output


def split_stock_shots_only(
    shots: list[Shot],
    max_seconds: int = 5,
    segments=None,
    variable: bool = True,
) -> list[Shot]:
    """Split stock shots only, preserving planned AI and Ken Burns slots."""
    limit = max(1, int(max_seconds or 5))
    snap = _snap_points_from_segments(segments)
    output: list[Shot] = []
    index = 1
    for shot in shots:
        if (shot.visual_type or "").lower() != "stock_video":
            data = dict(vars(shot))
            data["index"] = index
            output.append(Shot(**data))
            index += 1
            continue
        index = _split_shot_slot(
            shot,
            limit,
            index,
            output,
            snap,
            variable,
            force_stock=False,
        )
    return output


def _dur(shot) -> int:
    return max(1, timecode_to_seconds(shot.end) - timecode_to_seconds(shot.start))


def _is_type(shot, visual_type: str) -> bool:
    return (shot.visual_type or "").lower() == visual_type


def _searchable(shot) -> bool:
    """True when asset_query is usable for an English stock search."""
    query = (getattr(shot, "asset_query", "") or "").strip()
    letters = [c for c in query if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isascii()) / len(letters) >= 0.8


def _semantic_key(shot) -> tuple[str, ...]:
    return tuple(
        " ".join(str(getattr(shot, field, "") or "").lower().split())
        for field in ("scene_index", "visual_type", "description", "asset_query")
    )


def _move_editorial_accents(source, target) -> None:
    """Preserve sparse overlays/SFX when a non-viable slot is removed."""

    for field in ("screen_text", "sfx_accent_query"):
        value = (getattr(source, field, "") or "").strip()
        if value and not (getattr(target, field, "") or "").strip():
            setattr(target, field, value)
    if _PRIORITY_RANK.get((getattr(source, "priority", "") or "medium").lower(), 1) > _PRIORITY_RANK.get(
        (getattr(target, "priority", "") or "medium").lower(),
        1,
    ):
        target.priority = source.priority


def collapse_redundant_shot_runs(shots: list, log_callback=None) -> list:
    """Collapse mechanical copies into one source-media card.

    Stock rhythm is an edit/timeline concern.  Splitting one concept into four
    cards with the same query only downloads or generates the same visual four
    times.  Intentional continuation shots remain distinct because their
    description/query differs.
    """

    if not shots:
        return shots
    ordered = sorted(shots, key=lambda item: (timecode_to_seconds(item.start), item.index))
    collapsed: list = []
    removed = 0
    for shot in ordered:
        if (
            collapsed
            and _semantic_key(collapsed[-1]) == _semantic_key(shot)
            and timecode_to_seconds(collapsed[-1].end) == timecode_to_seconds(shot.start)
        ):
            previous = collapsed[-1]
            previous.end = shot.end
            _move_editorial_accents(shot, previous)
            removed += 1
            continue
        collapsed.append(shot)
    for index, shot in enumerate(collapsed, start=1):
        shot.index = index
    if removed and log_callback:
        log_callback(
            f"Монтажный ритм: объединено {removed} механически повторных "
            "карточек; один визуальный концепт теперь использует один источник."
        )
    return collapsed


def _minimum_duration(shot) -> int:
    visual_type = (getattr(shot, "visual_type", "") or "").lower()
    return (
        MIN_STOCK_SHOT_SECONDS
        if visual_type == "stock_video"
        else MIN_GENERATED_SHOT_SECONDS
    )


def enforce_minimum_shot_durations(shots: list, scenes: list, log_callback=None) -> list:
    """Remove non-viable 1–2 second generation slots and keep exact coverage."""

    if not shots:
        return shots
    scene_ranges = {
        int(scene.index): (
            timecode_to_seconds(scene.start),
            timecode_to_seconds(scene.end),
        )
        for scene in scenes
    }
    grouped: dict[int, list] = {}
    for shot in shots:
        grouped.setdefault(int(getattr(shot, "scene_index", 0)), []).append(shot)

    output: list = []
    removed = 0
    for scene_index, scene_shots in sorted(
        grouped.items(),
        key=lambda item: min(timecode_to_seconds(shot.start) for shot in item[1]),
    ):
        group = sorted(scene_shots, key=lambda item: timecode_to_seconds(item.start))
        scene_start, scene_end = scene_ranges.get(
            scene_index,
            (
                timecode_to_seconds(group[0].start),
                timecode_to_seconds(group[-1].end),
            ),
        )

        def retile() -> None:
            if not group:
                return
            group[0].start = seconds_to_timecode(scene_start)
            for previous, current in zip(group, group[1:]):
                previous.end = current.start
            group[-1].end = seconds_to_timecode(scene_end)

        retile()
        while len(group) > 1:
            bad_position = next(
                (
                    position
                    for position, shot in enumerate(group)
                    if _dur(shot) < _minimum_duration(shot)
                ),
                None,
            )
            if bad_position is None:
                break
            dropped = group.pop(bad_position)
            recipient = group[bad_position - 1] if bad_position > 0 else group[0]
            _move_editorial_accents(dropped, recipient)
            removed += 1
            retile()
        output.extend(group)

    output.sort(key=lambda item: timecode_to_seconds(item.start))
    for index, shot in enumerate(output, start=1):
        shot.index = index
    if removed and log_callback:
        log_callback(
            f"Монтажный ритм: удалено {removed} слишком коротких карточек "
            f"(сток <{MIN_STOCK_SHOT_SECONDS}с, AI/Ken Burns/референс "
            f"<{MIN_GENERATED_SHOT_SECONDS}с); соседние планы продлены."
        )
    return output


def enforce_route_budget(shots: list, route_priority: str = "balanced",
                         log_callback=None) -> None:
    """Hybrid mode: demote excess AI shots until their runtime share fits.

    Demotion order: lowest priority first, then longest first — the expensive
    route is kept for the shots the LLM itself marked as important. A demoted
    shot becomes stock_video when it has a usable English asset_query (the
    stock splitter downstream then cuts it to short pieces), otherwise
    still_kenburns (one Krea still + ffmpeg zoom — always possible)."""
    cap = ROUTE_AI_RUNTIME_CAP.get((route_priority or "balanced").strip().lower(), 0.45)
    if cap is None or not shots:
        return

    total = sum(_dur(s) for s in shots)
    ai = [s for s in shots if _is_type(s, "ai_image_then_i2v")]
    ai_seconds = sum(_dur(s) for s in ai)
    budget = int(total * cap)
    if ai_seconds <= budget:
        return

    ai.sort(key=lambda s: (_PRIORITY_RANK.get((s.priority or "medium").lower(), 1), -_dur(s)))
    demoted_stock = demoted_kb = 0
    for s in ai:
        if ai_seconds <= budget:
            break
        if _searchable(s):
            s.visual_type = "stock_video"
            s.image_model = ""
            s.video_model = ""
            demoted_stock += 1
        else:
            s.visual_type = "still_kenburns"
            s.video_model = ""
            demoted_kb += 1
        ai_seconds -= _dur(s)
    if log_callback and (demoted_stock or demoted_kb):
        log_callback(
            f"Бюджет маршрутов ({route_priority}): AI превышал {cap:.0%} хронометража — "
            f"понижено кадров: в сток {demoted_stock}, в Ken Burns {demoted_kb}; "
            f"AI теперь {ai_seconds}s из {total}s."
        )


def enforce_ai_route_budget(shots: list, route_priority: str = "balanced",
                            log_callback=None) -> None:
    """AI-only distribution budget (no stock in play).

    1) Cap the LTX runtime share for the priority (demote excess ai shots to
       still_kenburns — same Krea still, just no LTX pass; lowest priority and
       longest first, so hero shots keep their animation).
    2) Anti-slideshow: a run of more than MAX_KENBURNS_RUN consecutive stills
       gets its middle shot promoted back to LTX (budget permitting) — four
       zooms in a row read as a slide deck, not a video."""
    priority = (route_priority or "balanced").strip().lower()
    cap = AI_MIX_RUNTIME_CAP.get(priority, 0.55)
    if not shots:
        return

    total = sum(_dur(s) for s in shots)
    ai_seconds = sum(_dur(s) for s in shots if _is_type(s, "ai_image_then_i2v"))
    demoted = 0

    def _run_if_demoted(target) -> int:
        """Length of the consecutive Ken Burns run that would exist around
        `target` if it were demoted right now."""
        pos = shots.index(target)
        run = 1
        i = pos - 1
        while i >= 0 and _is_type(shots[i], "still_kenburns"):
            run += 1
            i -= 1
        i = pos + 1
        while i < len(shots) and _is_type(shots[i], "still_kenburns"):
            run += 1
            i += 1
        return run

    if cap is not None:
        budget = int(total * cap)
        remaining = sorted(
            [s for s in shots if _is_type(s, "ai_image_then_i2v")],
            key=lambda s: (_PRIORITY_RANK.get((s.priority or "medium").lower(), 1), -_dur(s)),
        )
        while ai_seconds > budget and remaining:
            # Run-aware pick: naive in-order demotion under a tight budget used
            # to produce 30+ second slideshow blocks (nine stills in a row).
            # Prefer the best-ranked candidate whose demotion keeps every Ken
            # Burns run within the limit; fall back to the best-ranked one
            # only when the budget leaves no compliant choice.
            pick = next(
                (s for s in remaining if _run_if_demoted(s) <= MAX_KENBURNS_RUN),
                remaining[0],
            )
            pick.visual_type = "still_kenburns"
            pick.video_model = ""
            ai_seconds -= _dur(pick)
            demoted += 1
            remaining.remove(pick)

    # Anti-slideshow pass (timeline order). Promote the middle of each long
    # still run back to animation while the budget allows a little headroom.
    promoted = 0
    if cap is not None:
        headroom = int(total * cap * 1.1)
        changed = True
        while changed:
            changed = False
            run: list = []
            for s in [*shots, None]:
                if s is not None and _is_type(s, "still_kenburns"):
                    run.append(s)
                    continue
                if len(run) > MAX_KENBURNS_RUN:
                    mid = run[len(run) // 2]
                    if ai_seconds + _dur(mid) <= headroom:
                        mid.visual_type = "ai_image_then_i2v"
                        mid.video_model = "ltx2.3_i2v"
                        ai_seconds += _dur(mid)
                        promoted += 1
                        changed = True
                        break  # rescan: the split may still leave long runs
                run = []

    if log_callback and (demoted or promoted):
        log_callback(
            f"AI-бюджет ({priority}): в Ken Burns понижено {demoted}, "
            f"обратно в LTX против слайдшоу {promoted}; "
            f"LTX теперь {ai_seconds}s из {total}s ({ai_seconds / max(1, total):.0%})."
        )


# One-shot event sounds cannot be looped as ambience: a "phone vibration"
# stretched over a 5-second span turns into meaningless buzzing.
_EVENT_SFX_PATTERN = re.compile(
    r"\b(beep|beeps|ring|ringtone|vibration|vibrate|knock|alarm|notification|"
    r"ding|chime|snap|slam|click|clicks|clicking|pop|whoosh|honk|jingle|"
    r"shutter|swoosh|thud|bang|buzzer)\b"
)
_AMBIENT_SFX_HINTS = (
    "ambience", "ambient", "hum", "noise", "atmosphere", "rain", "wind",
    "traffic", "crowd", "waves", "birds", "room tone", "murmur", "rumble",
    "drone", "static", "fan", "chatter", "forest", "ocean", "fire", "engine",
)


def _sfx_query_is_ambient(query: str) -> bool:
    """True only when the query POSITIVELY reads as a sustained bed.

    Deliberately stricter than :func:`_sfx_query_is_loopable`, which treats
    "not recognised as an event" as loopable. That default is right when
    choosing an ambience, but wrong when deciding whether to DELETE an accent:
    an unrecognised phrase like "glass shatter" would be discarded as
    sustained. Here an explicit ambient hint is required, so anything unknown
    is kept as an accent.
    """
    lowered = (query or "").lower()
    if not lowered.strip():
        return False
    if _EVENT_SFX_PATTERN.search(lowered):
        return False
    return any(hint in lowered for hint in _AMBIENT_SFX_HINTS)


def _sfx_query_is_loopable(query: str) -> bool:
    """True when the query describes a continuous bed rather than an event."""
    lowered = (query or "").lower()
    if not lowered.strip():
        return False
    if any(hint in lowered for hint in _AMBIENT_SFX_HINTS):
        return True
    return not _EVENT_SFX_PATTERN.search(lowered)


def consolidate_sfx_queries(shots: list, log_callback=None) -> None:
    """Enforce the film rule 'sound follows the location, not the cut'.

    The prompt asks the LLM to reuse one sfx_query for neighbouring shots of a
    location, but models routinely emit a new sound per shot — four different
    ambiences over a 14-second scene read as chaos, and one-shot event sounds
    (phone vibration, beeps) cannot be looped as a bed at all.

    Deterministic pass, same philosophy as the route budgets: every scene gets
    at most one ambience — the first loopable query among its shots. Scenes
    with only event-like or empty queries get an empty query (the export's
    room-tone layer covers them). Runs before any generation, so it also cuts
    the number of clips to generate from "per shot" down to "per scene".

    Two further caps keep sound spot-work rather than wall-to-wall coverage:
    ambience beds across the whole video are limited to ``MAX_AMBIENCE_BEDS``
    (longest scenes win, since a bed pays off only when it plays long enough
    to register), and each scene keeps at most ``MAX_ACCENTS_PER_SCENE``
    accents. Misfiled values are corrected rather than generated: an event
    written into the ambience field becomes an accent, and an ambience phrase
    written into the accent field is dropped instead of being fired as a
    one-shot.
    """
    by_scene: dict[int, list] = {}
    for shot in shots:
        by_scene.setdefault(getattr(shot, "scene_index", 0), []).append(shot)

    changed = 0
    dropped_events = 0
    dropped_accents = 0
    for scene_shots in by_scene.values():
        chosen = ""
        for shot in scene_shots:
            query = (getattr(shot, "sfx_query", "") or "").strip()
            if query and _sfx_query_is_loopable(query):
                chosen = query
                break
        accents_kept = 0
        for shot in scene_shots:
            query = (getattr(shot, "sfx_query", "") or "").strip()
            if query and not _sfx_query_is_loopable(query):
                # Event sounds move to the accent layer: played once at the
                # shot start over the ambience instead of being looped.
                if not (getattr(shot, "sfx_accent_query", "") or "").strip():
                    shot.sfx_accent_query = query
                dropped_events += 1
            if query != chosen:
                shot.sfx_query = chosen
                changed += 1

            accent = (getattr(shot, "sfx_accent_query", "") or "").strip()
            if not accent:
                continue
            # A sustained phrase cannot work as a one-shot accent, and a second
            # accent inside one scene just fights the first. Only drop on a
            # POSITIVE ambience match so unfamiliar event words are preserved.
            if _sfx_query_is_ambient(accent) or accents_kept >= MAX_ACCENTS_PER_SCENE:
                shot.sfx_accent_query = ""
                dropped_accents += 1
            else:
                accents_kept += 1

    # Global ambience budget: keep the longest scenes, clear the rest.
    scored: list[tuple[int, int, list]] = []
    for scene_index, scene_shots in by_scene.items():
        if not (getattr(scene_shots[0], "sfx_query", "") or "").strip():
            continue
        span = sum(
            max(
                0,
                timecode_to_seconds(getattr(shot, "end", "") or "00:00:00")
                - timecode_to_seconds(getattr(shot, "start", "") or "00:00:00"),
            )
            for shot in scene_shots
        )
        scored.append((span, scene_index, scene_shots))
    dropped_beds = 0
    if len(scored) > MAX_AMBIENCE_BEDS:
        # Longest first; ties fall back to scene order so the pass is stable.
        scored.sort(key=lambda item: (-item[0], item[1]))
        for _, _, scene_shots in scored[MAX_AMBIENCE_BEDS:]:
            for shot in scene_shots:
                if (getattr(shot, "sfx_query", "") or "").strip():
                    shot.sfx_query = ""
                    changed += 1
            dropped_beds += 1

    if (changed or dropped_accents) and log_callback:
        log_callback(
            f"Звук: точечная раскладка — сцен {len(by_scene)}, "
            f"фонов оставлено {min(len(scored), MAX_AMBIENCE_BEDS)}"
            f"{f' (снято {dropped_beds} сверх лимита)' if dropped_beds else ''}, "
            f"событий перенесено в акценты: {dropped_events}, "
            f"лишних акцентов убрано: {dropped_accents}."
        )


def diversify_repeated_shots(shots: list, log_callback=None) -> None:
    """Enforce the 'no two identical framings in a row' rule with code.

    The prompt asks the LLM to alternate shot types, but it routinely ignores
    that across scene boundaries and inside monotone scenes ('the wall of
    glowing monitors'). When two consecutive shots read nearly the same, the
    second one gets an explicit re-framing appended to its image prompt —
    deterministic by index, same philosophy as the route budgets."""
    reframes = [
        "a low angle looking up at the subject",
        "a tight detail close-up of the key object",
        "a wider view from the opposite side of the scene",
        "an over-the-shoulder perspective towards the subject",
    ]

    def sig(s) -> str:
        return " ".join(((s.description or "") + " " + (s.asset_query or "")).lower().split())[:200]

    changed = 0
    for prev, cur in zip(shots, shots[1:]):
        marker = (cur.image_prompt or "").lower()
        if "continuation shot" in marker or "framed differently" in marker:
            continue  # intentional continuations are already re-framed
        a, b = sig(prev), sig(cur)
        if not a or not b:
            continue
        if SequenceMatcher(None, a, b).ratio() > 0.8:
            variant = reframes[cur.index % len(reframes)]
            cur.image_prompt = (cur.image_prompt or cur.description).strip() \
                + f" Framed differently from the previous shot: {variant}."
            changed += 1
    if changed and log_callback:
        log_callback(f"Разнообразие планов: {changed} кадр(а/ов) подряд повторяли предыдущий — ракурс скорректирован.")
