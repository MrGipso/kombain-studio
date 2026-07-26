from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import local_model_eval as model_eval


def test_canonical_suite_is_unique_and_expands_schema_and_tool_refs():
    suite = model_eval.load_suite(model_eval.DEFAULT_CASES)

    assert suite["suite"] == "pipeline-studio-local-agent-vlm"
    assert suite["suite_version"] == "3.0.0"
    assert len(suite["cases"]) == 16
    assert len({case["id"] for case in suite["cases"]}) == 16
    route = next(case for case in suite["cases"] if case["track"] == "route")
    tool = next(case for case in suite["cases"] if case["id"] == "tool-001")
    assert route["response_schema"]["type"] == "object"
    assert tool["tools"][0]["function"]["name"] == "submit_generation_plan"
    expected = next(
        item
        for item in tool["tools"]
        if item["function"]["name"] == tool["expected_tool"]
    )
    assert expected["function"]["parameters"] == tool["response_schema"]


def test_every_scored_text_response_exposes_its_schema_to_the_model():
    suite = model_eval.load_suite(model_eval.DEFAULT_CASES)
    cases = [
        case
        for case in suite["cases"]
        if case.get("response_schema")
        and case.get("schema_target") != "tool_arguments"
    ]

    assert cases
    for case in cases:
        assert case["structured_output"] is True
        response_format = model_eval.structured_response_format(case)
        assert response_format is not None
        assert response_format["json_schema"]["schema"] == case[
            "response_schema"
        ]


def test_suite_loader_rejects_a_hidden_scoring_schema(tmp_path: Path):
    path = tmp_path / "hidden-schema.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_version": "2.0.0",
                "schemas": {"answer": {"type": "object"}},
                "cases": [
                    {
                        "id": "hidden",
                        "instruction": "Answer.",
                        "response_schema_ref": "answer",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(model_eval.EvalError, match="does not expose"):
        model_eval.load_suite(path)


def test_legacy_v1_suite_remains_loadable_for_report_reproduction():
    legacy = model_eval.load_suite(
        model_eval.ROOT / "tools" / "local_model_eval_cases.v1.json"
    )

    assert legacy["suite_version"] == "1.0.0"
    assert len(legacy["cases"]) == 14


def test_suite_loader_checks_inline_scoring_schemas_too(tmp_path: Path):
    path = tmp_path / "inline-schema.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_version": "2.0.0",
                "cases": [
                    {
                        "id": "inline",
                        "instruction": "Answer.",
                        "response_schema": {"type": "object"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(model_eval.EvalError, match="does not expose"):
        model_eval.load_suite(path)


def test_parse_json_response_recovers_fenced_object_and_reports_plain_text():
    parsed, error = model_eval.parse_json_response(
        'prefix\n```json\n{"route":"stock"}\n```\nsuffix'
    )
    missing, missing_error = model_eval.parse_json_response("plain explanation")

    assert parsed == {"route": "stock"}
    assert error == ""
    assert missing is None
    assert missing_error


def test_negative_regex_does_not_award_points_for_a_missing_field():
    passed, observed = model_eval.evaluate_check(
        {"kind": "not_regex", "path": "video_prompt", "pattern": "audio"},
        {},
        [],
    )

    assert passed is False
    assert observed is None


def test_extra_request_fields_and_thinking_mode_are_explicit_and_safe():
    extra = model_eval.parse_extra_request_fields(
        '{"min_p":0.05,"top_p":0.8}'
    )
    case = {"id": "text-case", "mode": "text", "instruction": "Return JSON."}

    payload, _ = model_eval.build_request_payload(
        case,
        model="local",
        ttl=1,
        seed=7,
        thinking_mode="off",
        extra_request_fields=extra,
    )

    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["min_p"] == 0.05
    assert payload["top_p"] == 0.8
    with pytest.raises(model_eval.EvalError, match="cannot override"):
        model_eval.parse_extra_request_fields('{"messages":[]}')


@pytest.mark.parametrize("field", sorted(model_eval.RESERVED_REQUEST_FIELDS))
def test_extra_request_fields_cannot_override_any_controlled_field(field: str):
    with pytest.raises(model_eval.EvalError, match="controlled request fields"):
        model_eval.parse_extra_request_fields(json.dumps({field: "override"}))


def test_case_extra_request_fields_cannot_override_thinking_or_schema():
    for field in ("chat_template_kwargs", "response_format", "ttl", "tools"):
        case = {
            "id": "controlled-case",
            "mode": "text",
            "instruction": "Return JSON.",
            "extra_request_fields": {field: "override"},
        }
        with pytest.raises(model_eval.EvalError, match="controlled request fields"):
            model_eval.build_request_payload(
                case,
                model="local",
                ttl=1,
                seed=7,
                thinking_mode="off",
            )


def test_effective_request_settings_report_actual_extras_and_contract():
    case = {
        "id": "settings-case",
        "mode": "text",
        "instruction": "Return JSON.",
        "structured_output": True,
        "response_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
    payload, _ = model_eval.build_request_payload(
        case,
        model="local",
        ttl=2,
        seed=9,
        thinking_mode="off",
        extra_request_fields={"top_p": 0.8},
    )

    settings = model_eval.effective_request_settings(payload)

    assert settings["ttl"] == 2
    assert settings["chat_template_kwargs"] == {"enable_thinking": False}
    assert settings["response_format"]["type"] == "json_schema"
    assert settings["extra_request_fields"] == {"top_p": 0.8}


def test_assistant_parser_preserves_reasoning_content_separately():
    response = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "reasoning_content": "private chain summary",
                    "content": '{"route":"stock"}',
                },
            }
        ]
    }

    parsed = model_eval.parse_assistant_response(response)

    assert parsed["reasoning_content"] == "private chain summary"
    assert parsed["parsed"] == {"route": "stock"}


def test_validate_schema_checks_required_enum_range_and_extra_properties():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["route", "score"],
        "properties": {
            "route": {"type": "string", "enum": ["stock", "image"]},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }

    assert model_eval.validate_schema({"route": "stock", "score": 0.8}, schema) == []
    errors = model_eval.validate_schema(
        {"route": "video", "score": 2, "unexpected": True}, schema
    )

    assert any("enum" in error for error in errors)
    assert any("above maximum" in error for error in errors)
    assert any("additional property" in error for error in errors)


def test_score_response_enforces_hard_route_check():
    case = {
        "response_schema": {
            "type": "object",
            "required": ["route"],
            "properties": {"route": {"type": "string"}},
        },
        "checks": [
            {
                "name": "route",
                "kind": "equals",
                "path": "route",
                "value": "stock",
                "weight": 4,
                "hard": True,
            }
        ],
    }

    good = model_eval.score_response(case, {"route": "stock"}, [])
    bad = model_eval.score_response(case, {"route": "image"}, [])

    assert good["score"] == 1.0 and good["passed"] is True
    assert bad["score"] < 0.8 and bad["passed"] is False
    assert bad["hard_failures"] == ["route"]
    summary = model_eval.summarize_results(
        [{"id": "good", "track": "route", "ok": True, "score": good}]
    )
    assert summary["contract_cases"] == 1
    assert summary["contract_pass_rate"] == 1.0
    assert summary["automated_checks_score"] == 1.0


def test_manual_review_is_pending_and_excluded_from_all_automated_aggregates():
    case = {
        "manual_review": True,
        "response_schema": {
            "type": "object",
            "required": ["route"],
            "properties": {"route": {"type": "string"}},
        },
        "checks": [
            {
                "name": "route",
                "kind": "equals",
                "path": "route",
                "value": "stock",
                "hard": True,
            }
        ],
    }
    score = model_eval.score_response(case, {"route": "stock"}, [])

    assert score["score"] is None
    assert score["passed"] is None
    assert score["provisional_score"] == 1.0
    summary = model_eval.summarize_results(
        [{"id": "manual", "track": "manual", "ok": True, "score": score}]
    )
    assert summary["passed"] == 0
    assert summary["automated_scored_cases"] == 0
    assert summary["automated_score"] is None
    assert summary["tracks"]["manual"]["automated_cases"] == 0
    assert summary["tracks"]["manual"]["score"] is None


def test_schema_invalid_unhashable_array_is_a_clean_failure_not_exception():
    case = {
        "response_schema": {
            "type": "object",
            "required": ["issues"],
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {"type": "string"},
                }
            },
        },
        "checks": [
            {
                "name": "issue",
                "kind": "array_contains_any",
                "path": "issues",
                "values": ["garbled_text"],
                "hard": True,
            }
        ],
    }

    score = model_eval.score_response(case, {"issues": [{"bad": "shape"}]}, [])

    assert score["passed"] is False
    assert score["score"] == 0.0
    assert score["schema_errors"]
    assert "schema" in score["hard_failures"]


def test_qa_consistency_rejects_contradictory_accept_payload():
    check = {"kind": "qa_consistency"}
    contradictory = {
        "verdict": "accept",
        "overall_score": 0.0,
        "issues": ["broken_geometry", "none"],
        "rationale": "Major visible defects remain.",
    }
    clean = {
        "verdict": "accept",
        "overall_score": 0.9,
        "issues": ["none"],
        "rationale": "The frame matches the requested composition.",
    }

    assert model_eval.evaluate_check(check, contradictory, [])[0] is False
    assert model_eval.evaluate_check(check, clean, [])[0] is True


def test_qa_consistency_requires_an_issue_for_needs_review():
    check = {"kind": "qa_consistency"}
    contradictory = {
        "verdict": "needs_review",
        "overall_score": 1.0,
        "issues": ["none"],
    }
    calibrated = {
        "verdict": "needs_review",
        "overall_score": 0.7,
        "issues": ["composition_problem"],
    }

    assert model_eval.evaluate_check(check, contradictory, [])[0] is False
    assert model_eval.evaluate_check(check, calibrated, [])[0] is True


def test_irrelevant_but_well_formed_prompt_rewrite_fails_semantic_checks():
    suite = model_eval.load_suite(model_eval.DEFAULT_CASES)
    case = next(item for item in suite["cases"] if item["id"] == "prompt-001")
    irrelevant = {
        "case_id": "prompt-001",
        "asset_query": "red sports car",
        "image_prompt": (
            "A red sports car waits on a mountain road at sunrise. "
            "Polished bodywork reflects the orange sky and distant peaks. "
            "A wide lens reveals empty asphalt curling through the valley."
        ),
        "video_prompt": (
            "The camera slowly tracks beside the parked car. "
            "A light breeze moves dry grass near the road."
        ),
    }

    score = model_eval.score_response(case, irrelevant, [])

    assert score["passed"] is False
    assert {"query-semantic", "image-bed", "image-food-storage"} & set(
        score["hard_failures"]
    )


def test_normalize_and_score_native_tool_arguments():
    message = {
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "submit_generation_plan",
                    "arguments": json.dumps(
                        {
                            "case_id": "c1",
                            "route": "stock",
                            "priority": "low",
                            "asset_query": "airplane window clouds",
                            "rationale": "Common real-world footage is readily available as stock.",
                        }
                    ),
                },
            }
        ]
    }
    calls = model_eval.normalize_tool_calls(message)
    case = {
        "expected_tool": "submit_generation_plan",
        "schema_target": "tool_arguments",
        "response_schema": {
            "type": "object",
            "required": ["case_id", "route"],
            "properties": {
                "case_id": {"type": "string"},
                "route": {"type": "string"},
                "priority": {"type": "string"},
                "asset_query": {"type": "string"},
                "rationale": {"type": "string"},
            },
        },
        "checks": [
            {
                "name": "tool",
                "kind": "tool_name",
                "value": "submit_generation_plan",
                "hard": True,
            },
            {
                "name": "route",
                "kind": "tool_arg_equals",
                "tool": "submit_generation_plan",
                "path": "route",
                "value": "stock",
                "hard": True,
            },
        ],
    }

    score = model_eval.score_response(case, None, calls)

    assert calls[0]["arguments"]["route"] == "stock"
    assert calls[0]["parse_error"] == ""
    assert score["passed"] is True


def test_suite_hides_route_labels_and_has_image_swap_control():
    suite = model_eval.load_suite(model_eval.DEFAULT_CASES)
    routes = [case for case in suite["cases"] if case["track"] == "route"]
    assert routes
    for case in routes:
        assert "current_route" not in case["input"]
        expected = next(
            (
                check.get("value")
                for check in case["checks"]
                if check.get("name") == "preferred-route"
            ),
            None,
        )
        assert not expected or expected not in case["id"]

    matched = next(case for case in suite["cases"] if case["id"] == "vision-004")
    swapped = next(case for case in suite["cases"] if case["id"] == "vision-005")
    matched_intent = {key: value for key, value in matched["input"].items() if key != "case_id"}
    swapped_intent = {key: value for key, value in swapped["input"].items() if key != "case_id"}
    assert matched_intent == swapped_intent
    assert matched["image"] != swapped["image"]
    assert any(
        check.get("name") == "identify-semantic-mismatch"
        for check in swapped["checks"]
    )


def test_suite_digest_is_bound_to_the_bytes_that_were_parsed(tmp_path: Path):
    path = tmp_path / "suite.json"
    original = json.dumps(
        {
            "schema_version": 1,
            "cases": [{"id": "one", "instruction": "Do one thing."}],
        },
        separators=(",", ":"),
    ).encode()
    path.write_bytes(original)

    suite = model_eval.load_suite(path)
    path.write_text("{}", encoding="utf-8")

    import hashlib

    assert suite["_source_sha256"] == hashlib.sha256(original).hexdigest()


def test_run_case_preserves_received_response_when_the_harness_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    response = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"route":"stock"}'},
            }
        ],
        "usage": {"completion_tokens": 3},
    }

    monkeypatch.setattr(
        model_eval, "post_chat", lambda *_args, **_kwargs: (response, 0.1)
    )

    def broken_scorer(*_args, **_kwargs):
        raise RuntimeError("scorer bug")

    monkeypatch.setattr(model_eval, "score_response", broken_scorer)
    result = model_eval.run_case(
        {"id": "raw-case", "track": "test", "instruction": "Return JSON."},
        model="local",
        base_url="http://127.0.0.1:1234/v1",
        timeout=1,
        ttl=1,
        seed=7,
        thinking_mode="default",
        extra_request_fields={},
        raw_dir=tmp_path,
    )
    raw = json.loads((tmp_path / "raw-case.json").read_text(encoding="utf-8"))

    assert result["ok"] is False
    assert result["error_stage"] == "harness"
    assert raw["response"] == response
    assert raw["error_stage"] == "harness"


def test_request_redaction_keeps_image_hash_without_base64(tmp_path: Path):
    image = tmp_path / "fixture.png"
    image.write_bytes(b"not-a-real-png-but-stable-bytes")
    case = {
        "id": "image-case",
        "mode": "image",
        "instruction": "Inspect this image.",
        "image": str(image),
    }

    payload, media = model_eval.build_request_payload(
        case, model="local", ttl=1, seed=7
    )
    redacted = model_eval._redact_data_urls(payload)
    url = redacted["messages"][1]["content"][1]["image_url"]["url"]

    assert media[0]["sha256"] in url
    assert "<omitted bytes=" in url
    assert "bm90LWE=" not in url


def test_output_guard_rejects_project_tree():
    with pytest.raises(model_eval.EvalError, match="inside projects"):
        model_eval.safe_output_dir(model_eval.ROOT / "projects" / "eval-output")
