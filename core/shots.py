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

from core.models import Shot, seconds_to_timecode, timecode_to_seconds

# Max share of RUNTIME that ai_image_then_i2v may occupy per route priority
# (hybrid mode: stock / kenburns / ai). None = no cap.
ROUTE_AI_RUNTIME_CAP: dict[str, float | None] = {"economy": 0.20, "balanced": 0.45, "rich": None}

# AI mode (no stock): max share of runtime that LTX animation may occupy.
AI_MIX_RUNTIME_CAP: dict[str, float | None] = {"economy": 0.30, "balanced": 0.55, "rich": None}

# More than this many consecutive Ken Burns stills reads as a slideshow.
MAX_KENBURNS_RUN = 3

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
