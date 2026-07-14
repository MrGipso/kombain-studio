import json

import pytest
from pydantic import ValidationError

from core import asset_qa
from core import asset_qa_store


def _technical(ok=True, **extra):
    return {"ok": ok, "issues": [], **extra}


def _vision(**overrides):
    data = {
        "semantic_alignment": "match",
        "composition": "good",
        "expected_text_legible": "not_applicable",
        "findings": [],
        "confidence": 0.95,
        "summary": "The requested subject is clearly visible.",
    }
    data.update(overrides)
    return asset_qa.VisionObservations.model_validate(data)


def test_contract_rejects_model_verdict_score_and_out_of_range_confidence():
    with pytest.raises(ValidationError):
        _vision(verdict="accept")
    with pytest.raises(ValidationError):
        _vision(score=0.9)
    with pytest.raises(ValidationError):
        _vision(confidence=4.0)


def test_clean_image_is_accepted_by_engine():
    decision = asset_qa.decide_image_action(_technical(), _vision())
    assert decision.action == "accept"
    assert decision.score == 1.0
    assert decision.reason_codes == []


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        (_vision(semantic_alignment="partial"), "needs_review"),
        (_vision(semantic_alignment="mismatch", confidence=0.9), "retry_image"),
        (
            _vision(findings=[{
                "code": "bad_anatomy",
                "severity": "major",
                "confidence": 0.91,
                "evidence": "The left hand has seven fused fingers.",
            }]),
            "retry_image",
        ),
        (
            _vision(findings=[{
                "code": "poor_composition",
                "severity": "minor",
                "confidence": 0.8,
                "evidence": "The subject is clipped at the edge.",
            }]),
            "needs_review",
        ),
    ],
)
def test_engine_maps_observations_to_actions(observations, expected):
    assert asset_qa.decide_image_action(_technical(), observations).action == expected


def test_duplicate_model_findings_do_not_multiply_penalty():
    finding = {
        "code": "poor_composition",
        "severity": "minor",
        "confidence": 0.8,
        "evidence": "The subject is close to the frame edge.",
    }
    decision = asset_qa.decide_image_action(
        _technical(), _vision(findings=[finding, finding]),
    )
    assert decision.score == 0.92
    assert decision.reason_codes.count("poor_composition") == 1


def test_text_legibility_only_applies_when_text_is_required():
    observation = _vision(expected_text_legible="no")
    without_text = asset_qa.decide_image_action(_technical(), observation)
    with_text = asset_qa.decide_image_action(
        _technical(), observation, expected_text_required=True,
    )
    assert without_text.action == "accept"
    assert without_text.score == 1.0
    assert with_text.action == "needs_review"
    assert with_text.score == 0.7


def test_unreadable_image_and_unavailable_vision_fail_safe():
    broken = asset_qa.decide_image_action(
        {"ok": False, "issues": [{"code": "decode_failed"}]},
        None,
    )
    unavailable = asset_qa.decide_image_action(_technical(), None)
    assert broken.action == "retry_image"
    assert broken.reason_codes == ["decode_failed"]
    assert unavailable.action == "needs_review"
    assert unavailable.reason_codes == ["qa_unavailable"]


@pytest.mark.parametrize(
    ("technical", "dynamic", "expected"),
    [
        (_technical(black_fraction=0.81, frozen_fraction=0.0), True, "retry_video"),
        (_technical(black_fraction=0.2, frozen_fraction=0.0), True, "needs_review"),
        (_technical(black_fraction=0.0, frozen_fraction=0.70), True, "retry_video"),
        (_technical(black_fraction=0.0, frozen_fraction=0.70), False, "accept"),
        (_technical(black_fraction=0.0, frozen_fraction=0.0), True, "accept"),
        ({"ok": False, "issues": [{"code": "decode_failed"}]}, True, "retry_video"),
    ],
)
def test_video_policy_is_route_aware(technical, dynamic, expected):
    assert asset_qa.decide_video_action(technical, dynamic=dynamic).action == expected


def test_store_is_append_only_and_latest_is_per_kind(tmp_path):
    image = asset_qa_store.append_report(tmp_path, {
        "card_id": "card-a",
        "kind": "image",
        "media": {"sha256": "a" * 64},
        "recommendation": "accept",
    })
    video = asset_qa_store.append_report(tmp_path, {
        "card_id": "card-a",
        "kind": "video",
        "media": {"sha256": "b" * 64},
        "recommendation": "needs_review",
    })
    second_image = asset_qa_store.append_report(tmp_path, {
        "card_id": "card-a",
        "kind": "image",
        "media": {"sha256": "c" * 64},
        "recommendation": "retry_image",
    })

    store = asset_qa_store.load_store(tmp_path)
    assert store["revision"] == 3
    assert [item["report_id"] for item in store["reports"]] == [
        image["report_id"], video["report_id"], second_image["report_id"],
    ]
    latest = asset_qa_store.latest_reports(tmp_path)
    assert latest["card-a"]["image"]["media"]["sha256"] == "c" * 64
    assert latest["card-a"]["video"]["media"]["sha256"] == "b" * 64


def test_store_refuses_to_overwrite_corrupt_history(tmp_path):
    path = tmp_path / asset_qa_store.ASSET_QA_FILE
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(asset_qa_store.QAStoreIntegrityError):
        asset_qa_store.append_report(tmp_path, {
            "card_id": "card-a",
            "kind": "image",
            "media": {"sha256": "a" * 64},
        })
    assert path.read_text(encoding="utf-8") == "{broken"


def test_shadow_never_mutates_card_and_review_only_flags():
    class Card:
        status = "generated"
        notes = ""
        approved = True

    shadow = Card()
    asset_qa.apply_review_mode(shadow, [{
        "mode": "shadow", "recommendation": "retry_image", "report_id": "s", "summary": "bad",
    }])
    assert (shadow.status, shadow.notes, shadow.approved) == ("generated", "", True)

    review = Card()
    report = {
        "mode": "review", "recommendation": "retry_image", "report_id": "r1", "summary": "hands",
    }
    asset_qa.apply_review_mode(review, [report])
    asset_qa.apply_review_mode(review, [report])
    assert review.status == "needs_review"
    assert review.approved is True
    assert review.notes.count("[QA:r1]") == 1


def test_store_never_contains_binary_payload(tmp_path):
    asset_qa_store.append_report(tmp_path, {
        "card_id": "card-a",
        "kind": "image",
        "media": {"sha256": "a" * 64},
        "technical": {"width": 1280, "height": 720},
    })
    text = (tmp_path / asset_qa_store.ASSET_QA_FILE).read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert parsed["reports"][0]["technical"]["width"] == 1280
    assert "data:image" not in text

    with pytest.raises(TypeError):
        asset_qa_store.append_report(tmp_path, {
            "card_id": "card-b",
            "kind": "image",
            "media": {"sha256": "b" * 64},
            "raw_image": b"binary payload",
        })


def test_vision_ttl_releases_default_immediate_unload_model():
    class Settings:
        lm_auto_unload = True
        lm_unload_delay = "0"

    assert asset_qa._vision_ttl(Settings()) == 20
    Settings.lm_unload_delay = "45"
    assert asset_qa._vision_ttl(Settings()) == 45
    Settings.lm_auto_unload = False
    assert asset_qa._vision_ttl(Settings()) is None
