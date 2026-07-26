from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import script as script_router
from core.rewrite_agent import strip_source_cta, validate_rewrite
from core.script_workflow import default_script


def _ready_script(text: str, *, source: str | None = None, title: str = "Геология") -> dict:
    script = default_script()
    script.update({
        "mode": "rewrite",
        "source_text": source if source is not None else text,
        "final_text": text,
        "target_duration_sec": 60,
        "beats": [{"title": title, "text": text, "target_words": 100}],
        "quality": {
            "target_words": 100,
            "total_beats": 1,
            "completed_beats": 1,
            "accepted_actual": False,
        },
    })
    return script


def test_validator_accepts_clean_source_grounded_script():
    text = " ".join(["Геология"] * 100)

    report = validate_rewrite(_ready_script(text))

    assert report["status"] == "ready"
    assert report["blocking_issues"] == 0
    assert report["score"] == 100


def test_validator_blocks_numbers_absent_from_source():
    source = " ".join(["Геология"] * 100)
    final = " ".join(["Геология"] * 99 + ["13"])

    report = validate_rewrite(_ready_script(final, source=source))

    assert report["status"] == "needs_revision"
    assert report["blocking_issues"] == 1
    assert report["issues"][0]["type"] == "unsupported_numbers"


def test_validator_treats_thousands_separator_variants_as_same_numbers():
    source = (
        "Масса 25.000 тонн, вместимость 1.300 контейнеров, цена 30.000 долларов, "
        "путь 302.000 миль, водоизмещение 61.000 тонн и груз 8.500 тонн. "
        + " ".join(["Геология"] * 70)
    )
    final = (
        "Масса 25 000 тонн, вместимость 1300 контейнеров, цена 30 000 долларов, "
        "путь 302 000 миль, водоизмещение 61 000 тонн и груз 8500 тонн. "
        + " ".join(["Геология"] * 70)
    )
    words = script_router.word_count(final)
    script = _ready_script(final, source=source)
    script["quality"]["target_words"] = words

    report = validate_rewrite(script)

    assert not any(issue["type"] == "unsupported_numbers" for issue in report["issues"])


def test_validator_matches_written_range_with_digit_range():
    source = (
        "С каждой стороны ставят по два или три абсолютно пустых вагона. "
        + " ".join(["Железная дорога"] * 45)
    )
    final = (
        "С каждой стороны ставят по 2–3 абсолютно пустых вагона. "
        + " ".join(["Железная дорога"] * 45)
    )
    script = _ready_script(final, source=source)
    script["quality"]["target_words"] = script_router.word_count(final)

    report = validate_rewrite(script)

    assert not any(issue["type"] == "unsupported_numbers" for issue in report["issues"])


def test_validator_matches_inflected_compound_and_mixed_written_numbers():
    source = (
        "Дистанция достигла ста тридцати метров, а груз — 25 тысяч тонн. "
        + " ".join(["Измерение"] * 85)
    )
    final = (
        "Дистанция достигла 130 метров, а груз — 25 000 тонн. "
        + " ".join(["Измерение"] * 85)
    )
    script = _ready_script(final, source=source)
    script["quality"]["target_words"] = script_router.word_count(final)

    report = validate_rewrite(script)

    assert not any(issue["type"] == "unsupported_numbers" for issue in report["issues"])


def test_validator_understands_couple_hundred_as_two_hundred():
    source = "За ночь расход достигает 200 литров. " + " ".join(["Топливо"] * 90)
    final = "За ночь расход достигает пары сотен литров. " + " ".join(["Топливо"] * 90)
    script = _ready_script(final, source=source)
    script["quality"]["target_words"] = script_router.word_count(final)

    report = validate_rewrite(script)

    assert not any(issue["type"] == "unsupported_numbers" for issue in report["issues"])


def test_validator_uses_approved_fact_ledger_for_recovered_asr_number():
    source = "Объём контура составляет от 1.000 до 1лу лров. " + " ".join(["Охлаждение"] * 90)
    final = "Объём контура составляет от 1000 до 1100 литров. " + " ".join(["Охлаждение"] * 90)
    script = _ready_script(final, source=source)
    script["analysis"] = {
        "facts": ["Объём охлаждающего контура составляет от 1000 до 1100 литров."],
        "theses": [],
        "warnings": [],
    }
    script["quality"]["target_words"] = script_router.word_count(final)

    report = validate_rewrite(script)

    assert not any(issue["type"] == "unsupported_numbers" for issue in report["issues"])


def test_same_unsupported_number_is_one_grouped_blocker():
    labels = ["Альфа", "Бета", "Гамма"]
    beats = [
        {
            "title": labels[index],
            "text": f"Уникальный раздел {labels[index]} сообщает значение 13. "
                    + " ".join([labels[index]] * 30),
            "target_words": 35,
        }
        for index in range(3)
    ]
    final = "\n\n".join(beat["text"] for beat in beats)
    script = _ready_script(final, source=" ".join(["Исходник"] * 105))
    script["beats"] = beats
    script["quality"] = {
        "target_words": script_router.word_count(final),
        "total_beats": 3,
        "completed_beats": 3,
        "accepted_actual": False,
    }

    report = validate_rewrite(script)
    number_issues = [
        issue for issue in report["issues"]
        if issue["type"] == "unsupported_numbers"
    ]

    assert report["blocking_issues"] == 1
    assert len(number_issues) == 1
    assert number_issues[0]["block_indices"] == [0, 1, 2]
    assert number_issues[0]["occurrence_count"] == 3


def test_validator_treats_model_identifier_punctuation_as_equivalent():
    source = "Аппарат MQ-25 используется для дозаправки. " + " ".join(["Авиация"] * 90)
    final = "Аппарат MQ25 используется для дозаправки. " + " ".join(["Авиация"] * 90)
    script = _ready_script(final, source=source)
    script["quality"]["target_words"] = script_router.word_count(final)

    report = validate_rewrite(script)

    assert not any(issue["type"] == "unsupported_identifier" for issue in report["issues"])
    assert not any(issue["type"] == "unsupported_numbers" for issue in report["issues"])


def test_validator_treats_f18_and_fa18_as_same_aircraft_family():
    source = "Истребитель F-18 участвовал в дозаправке. " + " ".join(["Авиация"] * 90)
    final = "Истребитель F/A-18 участвовал в дозаправке. " + " ".join(["Авиация"] * 90)
    script = _ready_script(final, source=source)
    script["quality"]["target_words"] = script_router.word_count(final)

    report = validate_rewrite(script)

    assert not any(issue["type"] == "unsupported_identifier" for issue in report["issues"])


def test_validator_blocks_identifier_absent_from_source():
    source = "Самолёт используется для дозаправки. " + " ".join(["Авиация"] * 90)
    final = "Самолёт MQ-25 используется для дозаправки. " + " ".join(["Авиация"] * 90)
    script = _ready_script(final, source=source)
    script["quality"]["target_words"] = script_router.word_count(final)

    report = validate_rewrite(script)

    assert any(issue["type"] == "unsupported_identifier" for issue in report["issues"])
    assert report["blocking_issues"] == 1


def test_small_duration_deviation_is_only_a_warning():
    text = " ".join(["Геология"] * 90)

    report = validate_rewrite(_ready_script(text))

    assert report["status"] == "ready"
    assert report["blocking_issues"] == 0
    assert any(issue["type"] == "duration_deviation" for issue in report["issues"])


def test_short_time_phrase_is_only_a_style_notification():
    text = "Шесть утра. " + " ".join(["Локомотив"] * 98)

    report = validate_rewrite(_ready_script(text))
    short_issue = next(
        issue for issue in report["issues"]
        if issue["type"] == "sentence_fragment"
    )

    assert short_issue["severity"] == "medium"
    assert short_issue["suggested_action"] == "review_style"
    assert report["blocking_issues"] == 0
    assert report["status"] == "ready"


def test_cta_cleanup_removes_only_promotional_sentence():
    text = "Полезный документальный вывод. Подпишитесь на канал и поставьте лайк. История продолжается."

    cleaned, removed = strip_source_cta(text)

    assert cleaned == "Полезный документальный вывод. История продолжается."
    assert removed == ["Подпишитесь на канал и поставьте лайк."]


def test_validator_detects_broken_paragraph_and_youtube_cta():
    text = " ".join(["Геология"] * 47) + ".\n\n" + "оборвано. Подпишитесь на канал. " + " ".join(["порода"] * 48)

    report = validate_rewrite(_ready_script(text))
    issue_types = {issue["type"] for issue in report["issues"]}

    assert {"broken_paragraph_seam", "source_cta"} <= issue_types
    assert report["status"] == "needs_revision"


def test_block_repair_is_persisted_only_when_score_improves(tmp_path, monkeypatch):
    source = " ".join(["Геология"] * 100)
    original = " ".join(["Геология"] * 99 + ["13"])
    repaired = " ".join(["Геология"] * 100)
    script = _ready_script(original, source=source)
    report = validate_rewrite(script)
    monkeypatch.setattr(
        script_router,
        "_lm_text",
        lambda *_args, **_kwargs: json.dumps(
            {"title": "Геология", "text": repaired},
            ensure_ascii=False,
        ),
    )

    result, next_report, actions, improved = script_router._repair_agent_blocks(
        tmp_path,
        script,
        report,
        SimpleNamespace(),
    )

    assert improved is True
    assert actions == 1
    assert next_report["status"] == "ready"
    assert result["final_text"] == repaired
    assert (tmp_path / "00_script.txt").read_text(encoding="utf-8") == repaired


def test_deterministic_cta_removal_is_kept_even_if_length_needs_refill(tmp_path):
    narration = " ".join(["Геология"] * 92) + "."
    cta = "Подпишитесь на канал и напишите в комментариях о следующей теме."
    text = f"{narration} {cta}"
    script = _ready_script(text, source=text)
    script["quality"]["target_words"] = script_router.word_count(text)
    report = validate_rewrite(script)

    result, next_report, actions, improved = script_router._apply_deterministic_agent_repairs(
        tmp_path,
        script,
        report,
    )

    assert improved is True
    assert actions == 1
    assert "Подпишитесь" not in result["final_text"]
    assert not any(issue["type"] == "source_cta" for issue in next_report["issues"])
    assert any(issue["type"] == "duration_deviation" for issue in next_report["issues"])


def test_deterministic_repair_removes_broken_lowercase_paragraph(tmp_path):
    first = " ".join(["Тепловоз"] * 48) + "."
    broken = "же, как сто часов работы. При морозе масло становится густым. " + " ".join(["Двигатель"] * 42)
    text = f"{first}\n\n{broken}"
    script = _ready_script(text, source=text)
    report = validate_rewrite(script)

    result, next_report, actions, improved = script_router._apply_deterministic_agent_repairs(
        tmp_path,
        script,
        report,
    )

    assert improved is True
    assert actions == 1
    assert "же, как сто часов работы." not in result["final_text"]
    assert "При морозе" in result["final_text"]
    assert not any(issue["type"] == "broken_paragraph_seam" for issue in next_report["issues"])


def test_repetition_repair_targets_only_later_occurrences():
    script = _ready_script(" ".join(["Геология"] * 100))
    script["beats"] = [
        {"title": f"Блок {index}", "text": "Текст", "target_words": 25}
        for index in range(4)
    ]
    report = {
        "issues": [{
            "type": "semantic_repetition",
            "severity": "medium",
            "block_index": 3,
            "block_indices": [0, 1, 2, 3],
            "occurrence_count": 4,
        }]
    }

    assert script_router._issue_block_indexes(script, report) == [2, 3]


def test_grouped_fact_repair_targets_every_affected_block():
    script = _ready_script(" ".join(["Геология"] * 100))
    script["beats"] = [
        {"title": f"Блок {index}", "text": "Текст", "target_words": 25}
        for index in range(4)
    ]
    report = {
        "issues": [{
            "type": "unsupported_numbers",
            "severity": "critical",
            "block_index": 0,
            "block_indices": [0, 2, 3],
            "occurrence_count": 3,
        }]
    }

    assert script_router._issue_block_indexes(script, report) == [0, 2, 3]


def test_grouped_fact_repair_accepts_fewer_remaining_occurrences():
    before = {
        "score": 76,
        "blocking_issues": 1,
        "issues": [{
            "type": "unsupported_numbers",
            "severity": "critical",
            "normalized_value": "13",
            "evidence": "13",
            "occurrence_count": 3,
        }],
    }
    after = {
        "score": 76,
        "blocking_issues": 1,
        "issues": [{
            "type": "unsupported_numbers",
            "severity": "critical",
            "normalized_value": "13",
            "evidence": "13",
            "occurrence_count": 2,
        }],
    }

    assert script_router._report_improved(before, after) is True


def test_agent_recognises_only_blocking_length_issues_for_expansion():
    critical = {
        "issues": [{
            "type": "duration_or_coverage",
            "severity": "high",
        }]
    }
    warning = {
        "issues": [{
            "type": "duration_deviation",
            "severity": "medium",
        }]
    }

    assert script_router._has_blocking_length_issue(critical) is True
    assert script_router._has_blocking_length_issue(warning) is False


def test_agent_releases_script_with_important_findings_as_notifications():
    report = {
        "issues": [{
            "type": "unsupported_numbers",
            "severity": "critical",
            "message": "Неподтверждённое число",
        }],
        "blocking_issues": 1,
    }

    assert script_router._agent_release_status(report) == (
        "ready_with_warnings",
        "Готово с замечаниями",
    )


def test_block_repairs_are_accepted_independently(tmp_path, monkeypatch):
    first = " ".join(["Геология"] * 50)
    second = " ".join(["Порода"] * 50)
    script = default_script()
    script.update({
        "mode": "rewrite",
        "source_text": f"{first} {second}",
        "final_text": f"{first}\n\n{second}",
        "target_duration_sec": 60,
        "beats": [
            {"title": "Океан", "text": first, "target_words": 50},
            {"title": "Самолёт", "text": second, "target_words": 50},
        ],
        "quality": {
            "target_words": 100,
            "total_beats": 2,
            "completed_beats": 2,
            "accepted_actual": False,
        },
    })
    report = validate_rewrite(script)

    def fake_text(prompt, *_args, **_kwargs):
        if f"<CURRENT_BLOCK>\n{first}" in prompt:
            return json.dumps({"title": "Геология", "text": first}, ensure_ascii=False)
        bad = " ".join(["Порода"] * 49 + ["99"])
        return json.dumps({"title": "Порода", "text": bad}, ensure_ascii=False)

    monkeypatch.setattr(script_router, "_lm_text", fake_text)
    result, next_report, actions, improved = script_router._repair_agent_blocks(
        tmp_path,
        script,
        report,
        SimpleNamespace(),
    )

    assert improved is True
    assert actions == 2
    assert result["beats"][0]["title"] == "Геология"
    assert result["beats"][1]["title"] == "Самолёт"
    assert next_report["score"] > report["score"]
    assert not any(issue["type"] == "unsupported_numbers" for issue in next_report["issues"])


def test_agent_stream_emits_current_stage_logs_and_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        script_router,
        "_resolve_project",
        lambda _project_id: SimpleNamespace(dir=tmp_path / "project"),
    )

    def fake_run(project_id, request, live_log=None, live_progress=None):
        assert project_id == "demo"
        assert request.max_iterations == 3
        live_progress("Редакторский проход 1/3", 62)
        live_log("Агент: правки приняты")
        return {
            "final_text": "Готово",
            "agent": {
                "status": "ready",
                "stage": "Готово",
                "progress": 100,
                "score": 100,
                "issues": [],
            },
        }

    monkeypatch.setattr(script_router, "_run_rewrite_agent_sync", fake_run)
    app = FastAPI()
    app.include_router(script_router.router, prefix="/api/projects")

    with TestClient(app) as client:
        response = client.post(
            "/api/projects/demo/script/agent-stream",
            json={"rewrite_style": "deep", "target_minutes": 10, "max_iterations": 3},
        )

    assert response.status_code == 200
    assert '"event": "progress"' in response.text
    assert "Редакторский проход 1/3" in response.text
    assert '"event": "log"' in response.text
    assert "правки приняты" in response.text
    assert '"event": "done"' in response.text
    assert '"status": "ready"' in response.text
