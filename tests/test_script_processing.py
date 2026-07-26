from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.routers import script as script_router
from core.script_workflow import default_script


def _settings(*, auto: bool = True, delay: str = "0") -> SimpleNamespace:
    return SimpleNamespace(
        lm_url="http://lm.test/v1",
        lm_model="test-model",
        lm_auto_unload=auto,
        lm_unload_delay=delay,
    )


def _rewrite_script() -> dict:
    script = default_script()
    script.update({
        "mode": "rewrite",
        "source_text": "Проверенный исходный материал для нового сценария.",
        "target_duration_sec": 600,
        "estimated_wpm": 135,
    })
    return script


def test_new_final_text_marks_existing_narration_as_outdated(tmp_path):
    script = _rewrite_script()
    script.update({
        "final_text": "Предыдущая редакция.",
        "actual_audio_duration_sec": 42.5,
        "text_revision": 3,
        "voice_source_revision": 3,
        "voice_outdated": False,
    })

    result = script_router._store_processed_script(
        tmp_path,
        script,
        source=script["source_text"],
        final_text="Новая редакция итогового сценария.",
        analysis={"facts": [], "theses": [], "warnings": []},
        target_seconds=600,
    )

    assert result["actual_audio_duration_sec"] == 42.5
    assert result["text_revision"] == 4
    assert result["voice_source_revision"] == 3
    assert result["voice_outdated"] is True


def test_unchanged_final_text_does_not_advance_text_revision(tmp_path):
    script = _rewrite_script()
    script.update({"final_text": "Текущая редакция.", "text_revision": 5})

    result = script_router._store_processed_script(
        tmp_path,
        script,
        source=script["source_text"],
        final_text=script["final_text"],
        analysis={"facts": [], "theses": [], "warnings": []},
        target_seconds=600,
    )

    assert result["text_revision"] == 5
    assert result["voice_outdated"] is False


def test_process_request_updates_duration_selected_after_import():
    script = _rewrite_script()

    result = script_router._apply_requested_duration(script, 23)

    assert result["target_duration_sec"] == 23 * 60


def test_process_request_uses_source_duration_with_small_automatic_buffer():
    script = _rewrite_script()
    script["source_estimated_duration_sec"] = 25 * 60
    script["duration_mode"] = "manual"

    result = script_router._apply_requested_duration(script, None)

    assert result["duration_mode"] == "auto"
    assert result["target_duration_min_sec"] == round(25 * 60 * 0.95)
    assert result["target_duration_sec"] == 25 * 60 + 90
    assert result["target_duration_max_sec"] == 27 * 60


@pytest.mark.parametrize("minutes", [0, 181])
def test_process_request_rejects_invalid_duration(minutes):
    with pytest.raises(ValueError, match="от 1 до 180"):
        script_router._apply_requested_duration(_rewrite_script(), minutes)


@pytest.mark.parametrize(
    ("auto", "delay", "expected_unloads", "expected_log"),
    [
        (True, "0", 1, "модель выгружена"),
        (True, "75", 0, "через 75 сек"),
        (False, "0", 0, "отключена"),
    ],
)
def test_script_unload_policy(monkeypatch, auto, delay, expected_unloads, expected_log):
    calls = []
    logs = []
    monkeypatch.setattr(script_router.lm, "unload_model", lambda url, model: calls.append((url, model)) or {"ok": True})

    script_router._unload_lm_after_script(_settings(auto=auto, delay=delay), logs.append)

    assert len(calls) == expected_unloads
    assert expected_log in logs[-1]


def test_script_json_call_retries_once_after_malformed_response(monkeypatch):
    calls = []
    logs = []

    def fake_chat_json(_settings, prompt, **_kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            raise ValueError("invalid JSON")
        return {"ok": True}

    monkeypatch.setattr(script_router.lm, "chat_json", fake_chat_json)

    result = script_router._lm_json(
        "Верни JSON",
        "Только JSON",
        _settings(),
        log=logs.append,
    )

    assert result == {"ok": True}
    assert len(calls) == 2
    assert "предыдущая попытка" in calls[1]
    assert "исправляющую попытку" in logs[0]


def test_rewrite_keeps_generated_blocks_when_local_correction_fails(tmp_path, monkeypatch):
    unloads = []
    text_calls = []
    draft = " ".join(f"слово{i}" for i in range(80))
    monkeypatch.setattr(script_router, "load_settings", lambda: _settings())
    monkeypatch.setattr(
        script_router,
        "_lm_json",
        lambda *args, **kwargs: {
            "facts": ["Факт"],
            "theses": ["Тезис"],
            "warnings": [],
            "beats": [
                {"title": "Вступление", "goal": "Объяснить", "visual_hint": "Архив"},
                {"title": "Финал", "goal": "Завершить", "visual_hint": "Итог"},
            ],
        },
    )

    def fake_text(*args, **kwargs):
        text_calls.append(1)
        if str(args[0]).startswith("Напиши только блок"):
            return draft
        raise RuntimeError("correction failed")

    monkeypatch.setattr(script_router, "_lm_text", fake_text)
    monkeypatch.setattr(
        script_router.lm,
        "unload_model",
        lambda url, model: unloads.append((url, model)) or {"ok": True},
    )

    result = script_router._process_script_sync(tmp_path, _rewrite_script(), "deep")

    expected = f"{draft}\n\n{draft}"
    assert result["final_text"] == expected
    assert (tmp_path / "00_script.txt").read_text(encoding="utf-8") == expected
    assert result["quality"]["status"] == "incomplete"
    assert result["quality"]["total_beats"] == 2
    assert [beat["title"] for beat in result["beats"]] == ["Вступление", "Финал"]
    assert unloads == [("http://lm.test/v1", "test-model")]


def test_block_generation_uses_distinct_source_sections_and_word_budgets(monkeypatch):
    calls = []
    source = "\n\n".join(
        " ".join(f"часть{section}_слово{index}" for index in range(120))
        for section in range(3)
    )

    def fake_text(prompt, *_args, **_kwargs):
        calls.append(prompt)
        match = __import__("re").search(r"Целевой объём блока: (\d+) слов", prompt)
        budget = int(match.group(1))
        return " ".join(f"готово{len(calls)}_{index}" for index in range(budget))

    monkeypatch.setattr(script_router, "_lm_text", fake_text)
    result = script_router._generate_rewrite_sections(
        source=source,
        raw_beats=[
            {"title": "Один", "goal": "Первый"},
            {"title": "Два", "goal": "Второй"},
            {"title": "Три", "goal": "Третий"},
        ],
        analysis={"facts": []},
        target_words=900,
        rewrite_style="deep",
        settings=_settings(),
    )

    assert len(calls) == 3
    assert [item["target_words"] for item in result] == [300, 300, 300]
    assert all(item["complete"] for item in result)
    assert "часть0_слово0" in calls[0]
    assert "часть1_слово0" in calls[1]
    assert "часть2_слово0" in calls[2]
    assert "полностью переформулируй материал" in calls[0]


def test_expand_preserves_existing_text_and_adds_only_missing_material(tmp_path, monkeypatch):
    script = _rewrite_script()
    first = " ".join(["первый"] * 40)
    second = " ".join(["второй"] * 40)
    script.update({
        "final_text": f"{first}\n\n{second}",
        "beats": [
            {"title": "Один", "text": first, "target_words": 100},
            {"title": "Два", "text": second, "target_words": 100},
        ],
        "quality": {"target_words": 200, "total_beats": 2, "completed_beats": 2},
    })
    monkeypatch.setattr(script_router, "load_settings", lambda: _settings(auto=False))
    monkeypatch.setattr(
        script_router,
        "_lm_text",
        lambda *args, **kwargs: " ".join(["добавление"] * 60),
    )

    result = script_router._expand_script_sync(tmp_path, script)

    assert result["beats"][0]["text"].startswith(first)
    assert result["beats"][1]["text"].startswith(second)
    assert result["quality"]["status"] == "ready"
    assert 190 <= result["quality"]["actual_words"] <= 210


def test_generated_addition_trims_at_safe_limit():
    text = "Короткое первое предложение. " + " ".join(f"слово{i}" for i in range(100))
    result = script_router._trim_generated_addition(text, 20)

    assert script_router.word_count(result) <= 20
    assert result.endswith((".", "!", "?", "…"))


def test_generated_addition_drops_leading_lowercase_fragment():
    text = "пламя. В такой ситуации вагон принимает первый удар. Затем состав останавливается."

    result = script_router._clean_generated_addition(text)

    assert result.startswith("В такой ситуации")
    assert "пламя." not in result


def test_broken_paragraph_cleanup_preserves_valid_paragraphs():
    text = (
        "Первый корректный абзац.\n\n"
        "же, как сто часов работы. При морозе масло становится густым."
    )

    result, fixed = script_router._clean_broken_paragraphs(text)

    assert fixed == 1
    assert result == "Первый корректный абзац.\n\nПри морозе масло становится густым."


def test_block_facts_are_limited_to_matching_source_excerpt():
    facts = [
        "Трансформатор окружён пустыми вагонами.",
        "Радиационный груз отделяют защитной зоной.",
        "Мост распределяет нагрузку по пролётам.",
    ]

    result = script_router._facts_for_excerpt(
        facts,
        "Опасный радиационный груз проходит через станцию внутри защитной зоны.",
    )

    assert result == ["Радиационный груз отделяют защитной зоной."]


def test_script_unloads_even_when_primary_generation_fails(tmp_path, monkeypatch):
    unloads = []
    monkeypatch.setattr(script_router, "load_settings", lambda: _settings())
    monkeypatch.setattr(script_router, "_lm_json", lambda *args, **kwargs: {"facts": [], "theses": [], "warnings": [], "beats": []})
    monkeypatch.setattr(script_router, "_lm_text", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("generation failed")))
    monkeypatch.setattr(
        script_router.lm,
        "unload_model",
        lambda url, model: unloads.append((url, model)) or {"ok": True},
    )

    with pytest.raises(RuntimeError, match="generation failed"):
        script_router._process_script_sync(tmp_path, _rewrite_script(), "deep")

    assert unloads == [("http://lm.test/v1", "test-model")]
