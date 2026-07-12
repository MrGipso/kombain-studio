"""Tests for core.shots — deterministic shot-list post-processing (slice #2)."""

from dataclasses import dataclass

from core.models import seconds_to_timecode
from core.shots import (
    diversify_repeated_shots,
    enforce_ai_route_budget,
    enforce_route_budget,
)


@dataclass
class FakeShot:
    index: int
    start: str
    end: str
    visual_type: str
    priority: str = "medium"
    asset_query: str = ""
    description: str = ""
    image_prompt: str = ""
    image_model: str = "krea2_turbo"
    video_model: str = "ltx2.3_i2v"


def make_shots(spec):
    """spec: list of (duration_sec, visual_type, priority, asset_query)."""
    shots, t = [], 0
    for i, (dur, vt, prio, aq) in enumerate(spec, start=1):
        shots.append(FakeShot(
            index=i,
            start=seconds_to_timecode(t),
            end=seconds_to_timecode(t + dur),
            visual_type=vt,
            priority=prio,
            asset_query=aq,
            description=f"shot {i}",
            image_prompt=f"prompt {i}",
        ))
        t += dur
    return shots


def ai_share(shots):
    from core.models import timecode_to_seconds as tc
    total = sum(tc(s.end) - tc(s.start) for s in shots)
    ai = sum(tc(s.end) - tc(s.start) for s in shots
             if s.visual_type == "ai_image_then_i2v")
    return ai / total


class TestHybridBudget:
    def test_economy_caps_ai_runtime(self):
        shots = make_shots([(10, "ai_image_then_i2v", "medium", "city street")] * 8
                           + [(5, "stock_video", "medium", "sky")] * 4)
        enforce_route_budget(shots, "economy")
        assert ai_share(shots) <= 0.20 + 0.01

    def test_demotes_to_stock_when_query_english(self):
        shots = make_shots([(10, "ai_image_then_i2v", "low", "server room racks")] * 5)
        enforce_route_budget(shots, "economy")
        assert any(s.visual_type == "stock_video" for s in shots)

    def test_demotes_to_kenburns_without_query(self):
        shots = make_shots([(10, "ai_image_then_i2v", "low", "")] * 5)
        enforce_route_budget(shots, "economy")
        assert any(s.visual_type == "still_kenburns" for s in shots)
        assert not any(s.visual_type == "stock_video" for s in shots)

    def test_rich_untouched(self):
        shots = make_shots([(10, "ai_image_then_i2v", "low", "x y")] * 5)
        enforce_route_budget(shots, "rich")
        assert all(s.visual_type == "ai_image_then_i2v" for s in shots)

    def test_high_priority_kept_longest(self):
        # 80s total, economy cap 20% -> 16s budget: the single 10s high shot
        # fits, so all demotions must land on the low-priority shots.
        shots = make_shots(
            [(10, "ai_image_then_i2v", "high", "a b")]
            + [(10, "ai_image_then_i2v", "low", "a b")] * 7
        )
        enforce_route_budget(shots, "economy")
        high = [s for s in shots if s.priority == "high"]
        assert all(s.visual_type == "ai_image_then_i2v" for s in high)
        assert any(s.visual_type != "ai_image_then_i2v" for s in shots if s.priority == "low")


class TestAiMixBudget:
    def test_economy_caps_ltx_runtime(self):
        shots = make_shots([(10, "ai_image_then_i2v", "medium", "")] * 10)
        enforce_ai_route_budget(shots, "economy")
        assert ai_share(shots) <= 0.30 * 1.1 + 0.01

    def test_no_long_kenburns_runs_even_under_tight_budget(self):
        # 12x4s all-AI at economy (30%) forces demoting 9 of 12 shots. The
        # run-aware demotion must SPREAD the survivors so no slideshow block
        # forms (naive in-order demotion used to leave a 9-still run here).
        shots = make_shots([(4, "ai_image_then_i2v", "low", "")] * 12)
        enforce_ai_route_budget(shots, "economy")
        run = max_run = 0
        for s in shots:
            run = run + 1 if s.visual_type == "still_kenburns" else 0
            max_run = max(max_run, run)
        assert max_run <= 4  # MAX_KENBURNS_RUN, +1 headroom for budget fallback
        assert ai_share(shots) <= 0.30 * 1.1 + 0.01

    def test_rich_untouched(self):
        shots = make_shots([(10, "ai_image_then_i2v", "low", "")] * 4)
        enforce_ai_route_budget(shots, "rich")
        assert all(s.visual_type == "ai_image_then_i2v" for s in shots)


class TestDiversify:
    def test_near_duplicates_get_reframed(self):
        shots = make_shots([(5, "stock_video", "medium", "airplane window clouds")] * 2)
        shots[0].description = "Вид из иллюминатора самолета на облака"
        shots[1].description = "Вид из иллюминатора самолета на густые облака"
        diversify_repeated_shots(shots)
        assert "Framed differently" in shots[1].image_prompt

    def test_distinct_shots_untouched(self):
        shots = make_shots([(5, "stock_video", "medium", "")] * 2)
        shots[0].description = "Крупный план обшивки крыла"
        shots[1].description = "Пассажир смотрит в окно салона"
        diversify_repeated_shots(shots)
        assert "Framed differently" not in shots[1].image_prompt

    def test_intentional_continuations_skipped(self):
        shots = make_shots([(5, "ai_image_then_i2v", "medium", "")] * 2)
        shots[0].description = shots[1].description = "Одинаковое описание кадра"
        shots[1].image_prompt = "Same scene. Continuation shot of the same scene and location: closer."
        diversify_repeated_shots(shots)
        assert shots[1].image_prompt.count("Framed differently") == 0
