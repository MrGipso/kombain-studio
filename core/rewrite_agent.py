"""Bounded rewrite agent: deterministic editorial QA plus the repair loop.

The language model may propose or perform repairs, but it is never trusted to
declare its own output production-ready.  The QA half of this module
(:func:`validate_rewrite` and the report helpers) is pure: no network, no
project storage, so every agent pass can be scored and tested reproducibly.

The loop half (:class:`RewriteAgentRun`) drafts through LM Studio, applies
safe local fixes, repairs flagged blocks one at a time and persists a
candidate only when the deterministic report improves.  Locking, run logs and
transport (HTTP/SSE) stay with the caller.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core import lm
from core.project import Project
from core.script_generation import (
    analysis_facts,
    clean_broken_paragraphs,
    expand_script,
    facts_for_excerpt,
    join_beat_texts,
    lm_text,
    process_script,
    source_sections,
    unload_lm_after_script,
)
from core.script_store import read_script, store_processed_script, write_script
from core.script_workflow import (
    DEFAULT_WPM,
    MAX_REWRITE_WORD_RATIO,
    MIN_REWRITE_WORD_RATIO,
    apply_requested_duration,
    assess_script_quality,
    clean_subtitle_text,
    estimate_duration_sec,
    word_count,
)


BLOCKING_SEVERITIES = frozenset({"critical", "high"})
_WORD_RE = re.compile(r"[\wЁёА-Яа-я-]+", flags=re.UNICODE)
_NUMBER_RE = re.compile(
    r"(?<![\w])(?:"
    r"\d{1,3}(?:[.\u00A0\u202F ]\d{3})+(?:,\d+)?"
    r"|\d+(?:[.,]\d+)?"
    r")(?![\w])"
)
_IDENTIFIER_RE = re.compile(
    r"(?<![\w])"
    r"[A-Za-zА-Яа-яЁё]{1,10}(?:/[A-Za-zА-Яа-яЁё]{1,10})*[-–—]?\d+[A-Za-zА-Яа-яЁё]?"
    r"(?![\w])"
)
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?|[А-Яа-яЁё]+", flags=re.UNICODE)
_RU_NUMBER_VALUES = {
    "ноль": 0, "нуля": 0, "нулю": 0, "нулем": 0, "нулём": 0,
    "один": 1, "одна": 1, "одно": 1, "одного": 1, "одной": 1,
    "одному": 1, "одним": 1, "одну": 1,
    "два": 2, "две": 2, "двух": 2, "двум": 2, "двумя": 2,
    "пара": 2, "пары": 2, "пару": 2, "парой": 2,
    "три": 3, "трех": 3, "трёх": 3, "трем": 3, "трём": 3, "тремя": 3,
    "четыре": 4, "четырех": 4, "четырёх": 4, "четырем": 4,
    "четырём": 4, "четырьмя": 4,
    "пять": 5, "пяти": 5, "пятью": 5,
    "шесть": 6, "шести": 6, "шестью": 6,
    "семь": 7, "семи": 7, "семью": 7,
    "восемь": 8, "восьми": 8, "восемью": 8,
    "девять": 9, "девяти": 9, "девятью": 9,
    "десять": 10, "десяти": 10, "десятью": 10,
    "одиннадцать": 11, "одиннадцати": 11,
    "двенадцать": 12, "двенадцати": 12,
    "тринадцать": 13, "тринадцати": 13,
    "четырнадцать": 14, "четырнадцати": 14,
    "пятнадцать": 15, "пятнадцати": 15,
    "шестнадцать": 16, "шестнадцати": 16,
    "семнадцать": 17, "семнадцати": 17,
    "восемнадцать": 18, "восемнадцати": 18,
    "девятнадцать": 19, "девятнадцати": 19,
    "двадцать": 20, "двадцати": 20,
    "тридцать": 30, "тридцати": 30,
    "сорок": 40, "сорока": 40,
    "пятьдесят": 50, "пятидесяти": 50,
    "шестьдесят": 60, "шестидесяти": 60,
    "семьдесят": 70, "семидесяти": 70,
    "восемьдесят": 80, "восьмидесяти": 80,
    "девяносто": 90, "девяноста": 90,
    "сто": 100, "ста": 100,
    "двести": 200, "двухсот": 200,
    "триста": 300, "трехсот": 300, "трёхсот": 300,
    "четыреста": 400, "четырехсот": 400, "четырёхсот": 400,
    "пятьсот": 500, "пятисот": 500,
    "шестьсот": 600, "шестисот": 600,
    "семьсот": 700, "семисот": 700,
    "восемьсот": 800, "восьмисот": 800,
    "девятьсот": 900, "девятисот": 900,
}
_RU_NUMBER_SCALES = {
    "сотня": 100, "сотни": 100, "сотен": 100, "сотню": 100, "сотней": 100,
    "тысяча": 1_000, "тысячи": 1_000, "тысяч": 1_000,
    "тысячу": 1_000, "тысячей": 1_000,
    "миллион": 1_000_000, "миллиона": 1_000_000, "миллионов": 1_000_000,
    "миллиард": 1_000_000_000, "миллиарда": 1_000_000_000,
    "миллиардов": 1_000_000_000,
}
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
_CTA_RE = re.compile(
    r"\b(?:подпиш(?:итесь|ись)|поставьте\s+лайк|жмите\s+колокольчик|"
    r"ссылка\s+в\s+описании|напишите\s+в\s+комментариях)\b",
    flags=re.IGNORECASE,
)
_STOPWORDS = {
    "это", "как", "для", "что", "чтобы", "или", "при", "над", "под", "без",
    "его", "её", "они", "она", "оно", "был", "была", "были", "есть", "уже",
    "ещё", "так", "весь", "всего", "этот", "эта", "эти", "того", "только",
    "более", "около", "после", "когда", "если", "где", "который", "которая",
}


def _normal_words(text: str) -> list[str]:
    return [match.group(0).casefold().replace("ё", "е") for match in _WORD_RE.finditer(text)]


def _issue(
    kind: str,
    severity: str,
    message: str,
    *,
    block_index: int | None = None,
    evidence: str = "",
    action: str = "repair_block",
    **details: Any,
) -> dict[str, Any]:
    seed = f"{kind}|{block_index}|{evidence}|{message}".encode("utf-8", errors="replace")
    issue = {
        "id": hashlib.sha1(seed).hexdigest()[:12],
        "type": kind,
        "severity": severity,
        "block_index": block_index,
        "message": message,
        "evidence": evidence[:320],
        "suggested_action": action,
    }
    issue.update(details)
    return issue


def _normalise_number(value: str) -> str:
    """Canonicalise common Russian/European thousands and decimal formats.

    The source subtitles often use ``25.000`` while a rewrite uses ``25 000``
    or ``25000``.  Treating the trailing ``000`` as a separate unsupported
    number caused valid scripts to be blocked.
    """
    compact = re.sub(r"[\u00A0\u202F ]", "", str(value))
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", compact):
        integer, comma, fraction = compact.partition(",")
        canonical = integer.replace(".", "")
        return canonical + (f".{fraction}" if comma and fraction else "")
    if "," in compact:
        integer, fraction = compact.split(",", 1)
        return f"{integer}.{fraction}"
    return compact


def _parse_number_sequence(tokens: list[str]) -> int | None:
    total = 0
    current = 0
    for raw in tokens:
        token = raw.casefold().replace("ё", "е")
        if re.fullmatch(r"\d+", token):
            current += int(token)
            continue
        value = _RU_NUMBER_VALUES.get(token)
        if value is not None:
            current += value
            continue
        scale = _RU_NUMBER_SCALES.get(token)
        if scale is None:
            return None
        total += max(1, current) * scale
        current = 0
    return total + current


def _word_number_mentions(text: str) -> tuple[dict[str, list[str]], list[tuple[int, int]]]:
    """Extract Russian written/mixed numbers and return spans owning digit tokens."""
    tokens = list(_NUMBER_TOKEN_RE.finditer(text))
    mentions: dict[str, list[str]] = {}
    covered_digit_spans: list[tuple[int, int]] = []
    index = 0
    while index < len(tokens):
        match = tokens[index]
        normalized = match.group(0).casefold().replace("ё", "е")
        is_word_number = normalized in _RU_NUMBER_VALUES or normalized in _RU_NUMBER_SCALES
        is_digit = bool(re.fullmatch(r"\d+", normalized))
        if not (is_word_number or is_digit):
            index += 1
            continue
        sequence = [match]
        cursor = index + 1
        while cursor < len(tokens):
            candidate = tokens[cursor]
            gap = text[sequence[-1].end():candidate.start()]
            candidate_value = candidate.group(0).casefold().replace("ё", "е")
            candidate_is_number = (
                candidate_value in _RU_NUMBER_VALUES
                or candidate_value in _RU_NUMBER_SCALES
                or bool(re.fullmatch(r"\d+", candidate_value))
            )
            if not candidate_is_number or not gap.isspace():
                break
            sequence.append(candidate)
            cursor += 1
        # Pure digit sequences remain the responsibility of _NUMBER_RE, which
        # already handles decimals and thousands separators correctly.
        if any(not re.fullmatch(r"\d+", item.group(0)) for item in sequence):
            parsed = _parse_number_sequence([item.group(0) for item in sequence])
            if parsed is not None:
                raw = text[sequence[0].start():sequence[-1].end()]
                mentions.setdefault(str(parsed), []).append(raw)
                covered_digit_spans.extend(
                    item.span()
                    for item in sequence
                    if re.fullmatch(r"\d+", item.group(0))
                )
        index = max(index + 1, cursor)
    return mentions, covered_digit_spans


def _number_mentions(text: str) -> dict[str, list[str]]:
    mentions, word_owned_digit_spans = _word_number_mentions(text)
    identifier_spans = [match.span() for match in _IDENTIFIER_RE.finditer(text)]
    for match in _NUMBER_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in identifier_spans):
            continue
        if any(start <= match.start() and match.end() <= end for start, end in word_owned_digit_spans):
            continue
        raw = match.group(0)
        mentions.setdefault(_normalise_number(raw), []).append(raw)
    return mentions


def _numbers(text: str) -> set[str]:
    return set(_number_mentions(text))


def _normalise_identifier(value: str) -> str:
    normalized = str(value).casefold().replace("ё", "е")
    # The Hornet family is commonly shortened to both F-18 and F/A-18 in
    # Russian subtitles; the slash and the intermediate "A" do not introduce
    # a new numeric claim.
    normalized = re.sub(r"^f[/\-–—]?a(?=[\-–—]?\d)", "f", normalized)
    return re.sub(r"[/\-–—]", "", normalized)


def _identifier_mentions(text: str) -> dict[str, list[str]]:
    mentions: dict[str, list[str]] = {}
    for match in _IDENTIFIER_RE.finditer(text):
        raw = match.group(0)
        mentions.setdefault(_normalise_identifier(raw), []).append(raw)
    return mentions


def _paragraph_seam_issues(beats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for index, beat in enumerate(beats):
        text = str(beat.get("text") or "")
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        for paragraph_index, paragraph in enumerate(paragraphs):
            first = paragraph[:1]
            if first and first.isalpha() and first.islower():
                issues.append(_issue(
                    "broken_paragraph_seam",
                    "high",
                    "Абзац начинается со строчной буквы и, вероятно, оторван от предыдущей фразы.",
                    block_index=index,
                    evidence=paragraph[:180],
                    action="repair_transition",
                ))
            sentences = [part.strip() for part in _SENTENCE_RE.split(paragraph) if part.strip()]
            for sentence_index, sentence in enumerate(sentences):
                count = word_count(sentence)
                if count <= 2 and (
                    sentence_index > 0
                    or paragraph_index > 0
                    or sentence.rstrip().endswith(".")
                ):
                    issues.append(_issue(
                        "sentence_fragment",
                        "medium",
                        "Короткая фраза: проверьте, является ли она намеренным стилистическим акцентом.",
                        block_index=index,
                        evidence=sentence,
                        action="review_style",
                    ))
    return issues


def _duplicate_issues(beats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, tuple[int, str]] = {}
    issues: list[dict[str, Any]] = []
    for block_index, beat in enumerate(beats):
        for sentence in _SENTENCE_RE.split(str(beat.get("text") or "")):
            normalized = " ".join(_normal_words(sentence))
            if len(normalized) < 45:
                continue
            previous = seen.get(normalized)
            if previous is not None:
                issues.append(_issue(
                    "duplicate_sentence",
                    "medium",
                    "Предложение повторяет уже использованный текст.",
                    block_index=block_index,
                    evidence=sentence.strip(),
                    action="remove_repetition",
                ))
            else:
                seen[normalized] = (block_index, sentence.strip())
    return issues


def _repeated_phrase_issues(beats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    occurrences: dict[tuple[str, ...], list[int]] = {}
    for block_index, beat in enumerate(beats):
        words = _normal_words(str(beat.get("text") or ""))
        for position in range(max(0, len(words) - 5)):
            phrase = tuple(words[position:position + 6])
            if len(set(phrase) - _STOPWORDS) < 3:
                continue
            occurrences.setdefault(phrase, []).append(block_index)
    repeated = [
        (phrase, indices)
        for phrase, indices in occurrences.items()
        if len(set(indices)) >= 3
    ]
    if not repeated:
        return []
    phrase, indices = max(repeated, key=lambda item: len(set(item[1])))
    block_indices = sorted(set(indices))
    block_index = max(block_indices)
    return [_issue(
        "semantic_repetition",
        "medium",
        "Одна и та же формулировка встречается как минимум в трёх смысловых блоках.",
        block_index=block_index,
        evidence=" ".join(phrase),
        action="remove_repetition",
        occurrence_count=len(block_indices),
        block_indices=block_indices,
    )]


def _title_alignment_issues(beats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for index, beat in enumerate(beats):
        title_words = {
            word for word in _normal_words(str(beat.get("title") or ""))
            if len(word) >= 5 and word not in _STOPWORDS
        }
        content_words = set(_normal_words(str(beat.get("text") or "")))
        if title_words and not any(
            any(content.startswith(title[:5]) or title.startswith(content[:5]) for content in content_words)
            for title in title_words
        ):
            issues.append(_issue(
                "title_content_mismatch",
                "medium",
                "Название смыслового блока слабо связано с его фактическим содержанием.",
                block_index=index,
                evidence=str(beat.get("title") or ""),
                action="rename_block",
            ))
    return issues


def strip_source_cta(text: str) -> tuple[str, list[str]]:
    """Remove complete promotional sentences without rewriting narration."""
    removed: list[str] = []
    paragraphs: list[str] = []
    for paragraph in re.split(r"\n\s*\n", str(text or "")):
        value = paragraph.strip()
        if not value:
            continue
        sentences = [part.strip() for part in _SENTENCE_RE.split(value) if part.strip()]
        kept: list[str] = []
        for sentence in sentences:
            if _CTA_RE.search(sentence):
                removed.append(sentence)
            else:
                kept.append(sentence)
        if kept:
            paragraphs.append(" ".join(kept))
    return "\n\n".join(paragraphs).strip(), removed


def validate_rewrite(script: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic QA report and a score suitable for agent loops."""
    quality = assess_script_quality(script)
    beats = [item for item in (script.get("beats") or []) if isinstance(item, dict)]
    final_text = str(script.get("final_text") or "")
    source_text = str(script.get("source_text") or "")
    issues: list[dict[str, Any]] = []

    coverage_ratio = float(quality.get("coverage_ratio") or 0)
    word_ratio = float(quality.get("word_ratio") or 0)
    if not final_text.strip() or coverage_ratio < 0.999:
        issues.append(_issue(
            "duration_or_coverage",
            "critical",
            "Сценарий оборван или не покрывает все запланированные смысловые блоки.",
            evidence=f"{quality.get('actual_words', 0)}/{quality.get('target_words', 0)} слов",
            action="adjust_length",
        ))
    elif word_ratio < 0.85 or word_ratio > 1.15:
        issues.append(_issue(
            "duration_or_coverage",
            "high",
            "Объём сценария существенно отличается от естественного объёма источника.",
            evidence=f"{quality.get('actual_words', 0)}/{quality.get('target_words', 0)} слов",
            action="adjust_length",
        ))
    elif quality.get("status") != "ready":
        issues.append(_issue(
            "duration_deviation",
            "medium",
            "Длительность немного отличается от автоматической цели; это не блокирует озвучку.",
            evidence=f"{quality.get('actual_words', 0)}/{quality.get('target_words', 0)} слов",
            action="adjust_length",
        ))

    analysis = script.get("analysis") if isinstance(script.get("analysis"), dict) else {}
    approved_facts = [
        str(item).strip()
        for item in (analysis.get("facts") or [])
        if str(item).strip()
    ]
    # The preliminary analysis is the approved fact ledger used by generation.
    # Include it in grounding so a clearly recovered ASR value (for example
    # ``1лу лров`` -> ``1100 литров``) is not rejected after the same ledger
    # supplied that value to the rewrite model.
    grounding_text = "\n".join([source_text, *approved_facts])
    source_numbers = _numbers(grounding_text)
    final_numbers = _number_mentions(final_text)
    unsupported_numbers = sorted(set(final_numbers) - source_numbers)
    if unsupported_numbers:
        for number in unsupported_numbers:
            affected_blocks = [
                block_index
                for block_index, beat in enumerate(beats)
                if number in _number_mentions(str(beat.get("text") or ""))
            ]
            raw_variants = list(dict.fromkeys(final_numbers[number]))
            issues.append(_issue(
                "unsupported_numbers",
                "critical",
                "В сценарии появились числа, которых нет в исходном материале.",
                block_index=affected_blocks[0] if affected_blocks else None,
                evidence=", ".join(raw_variants[:8]),
                action="remove_unsupported_claim",
                normalized_value=number,
                occurrence_count=len(affected_blocks) or len(raw_variants),
                block_indices=affected_blocks,
            ))

    source_identifiers = set(_identifier_mentions(grounding_text))
    final_identifiers = _identifier_mentions(final_text)
    unsupported_identifiers = sorted(set(final_identifiers) - source_identifiers)
    for identifier in unsupported_identifiers:
        affected = [
            index
            for index, beat in enumerate(beats)
            if identifier in _identifier_mentions(str(beat.get("text") or ""))
        ]
        issues.append(_issue(
            "unsupported_identifier",
            "high",
            "В сценарии появился индекс модели или техники, которого нет в источнике.",
            block_index=affected[0] if affected else None,
            evidence=final_identifiers[identifier][0],
            action="remove_unsupported_claim",
            normalized_value=identifier,
            occurrence_count=len(affected) or len(final_identifiers[identifier]),
            block_indices=affected,
        ))

    if _CTA_RE.search(final_text):
        issues.append(_issue(
            "source_cta",
            "high",
            "В итоговом сценарии остался призыв из исходного YouTube-материала.",
            evidence=_CTA_RE.search(final_text).group(0) if _CTA_RE.search(final_text) else "",
            action="repair_block",
        ))

    issues.extend(_paragraph_seam_issues(beats))
    issues.extend(_duplicate_issues(beats))
    issues.extend(_repeated_phrase_issues(beats))
    issues.extend(_title_alignment_issues(beats))

    # De-duplicate issues that point at the same actionable defect.
    unique: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for issue in issues:
        if issue["id"] in seen_ids:
            continue
        seen_ids.add(issue["id"])
        unique.append(issue)

    weights = {"critical": 24, "high": 12, "medium": 4, "low": 1}
    penalty = sum(weights.get(str(issue.get("severity")), 1) for issue in unique)
    blocking = [issue for issue in unique if issue.get("severity") in BLOCKING_SEVERITIES]
    status = "ready" if not blocking else "needs_revision"
    return {
        "status": status,
        "score": max(0, 100 - penalty),
        "quality": quality,
        "issues": unique,
        "blocking_issues": len(blocking),
        "warnings": len(unique) - len(blocking),
    }


def issue_summary(report: dict[str, Any], *, limit: int = 8) -> str:
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    if not issues:
        return "Проблем не обнаружено."
    lines: list[str] = []
    for item in issues[:limit]:
        block_indices = [
            int(index) + 1
            for index in (item.get("block_indices") or [])
            if isinstance(index, int)
        ]
        if len(block_indices) > 1:
            location = "блоки " + ", ".join(str(index) for index in block_indices)
        else:
            location = (
                str(int(item.get("block_index")) + 1)
                if isinstance(item.get("block_index"), int)
                else "весь сценарий"
            )
        repeats = (
            f"; повторов: {item.get('occurrence_count')}"
            if item.get("occurrence_count")
            else ""
        )
        lines.append(
            f"- [{item.get('severity')}] {item.get('message')} "
            f"({location if str(location).startswith('блоки ') else f'блок {location}'}; "
            f"фрагмент: {item.get('evidence') or '—'}{repeats})"
        )
    return "\n".join(lines)


# --- Agent state and report comparison -----------------------------------


Log = Callable[[str], None]
Progress = Callable[[str, int], None]
CancelCheck = Callable[[], None]

# Editorial passes per run. RewriteAgentRequest.max_iterations is accepted by
# the API but does not raise this bound.
AGENT_MAX_ITERATIONS = 1
_GROUPED_CLAIM_TYPES = frozenset({"unsupported_numbers", "unsupported_identifier"})


def agent_payload(
    script: dict[str, Any],
    *,
    status: str | None = None,
    stage: str | None = None,
    progress: int | None = None,
    iteration: int | None = None,
    max_iterations: int | None = None,
    report: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Update ``script["agent"]`` in place with the given fields and return it."""
    current = script.get("agent") if isinstance(script.get("agent"), dict) else {}
    agent = dict(current)
    if status is not None:
        agent["status"] = status
    if stage is not None:
        agent["stage"] = stage
    if progress is not None:
        agent["progress"] = max(0, min(100, int(progress)))
    if iteration is not None:
        agent["iteration"] = max(0, int(iteration))
    if max_iterations is not None:
        agent["max_iterations"] = max(1, int(max_iterations))
    if report is not None:
        agent["score"] = int(report.get("score") or 0)
        agent["issues"] = list(report.get("issues") or [])
    if history is not None:
        agent["history"] = history[-20:]
    script["agent"] = agent
    return agent


def agent_release_status(report: dict[str, Any]) -> tuple[str, str]:
    """Editorial findings are notices; a non-empty final script remains voice-ready."""
    if report.get("issues"):
        return "ready_with_warnings", "Готово с замечаниями"
    return "ready", "Готово"


def _report_issues(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (report.get("issues") or []) if isinstance(item, dict)]


def _issue_types(report: dict[str, Any]) -> set[str]:
    return {str(item.get("type") or "") for item in _report_issues(report)}


def _valid_block_indices(issue: dict[str, Any], beats: list[dict[str, Any]]) -> list[int]:
    return [
        int(item)
        for item in (issue.get("block_indices") or [])
        if isinstance(item, int) and 0 <= item < len(beats)
    ]


def _blocks_outside_budget(beats: list[dict[str, Any]]) -> list[int]:
    indexes: list[int] = []
    for block_index, beat in enumerate(beats):
        words = word_count(str(beat.get("text") or ""))
        budget = max(1, int(beat.get("target_words") or 0))
        if (
            words < round(budget * MIN_REWRITE_WORD_RATIO)
            or words > round(budget * MAX_REWRITE_WORD_RATIO)
        ):
            indexes.append(block_index)
    return indexes


def _blocks_mentioning_numbers(issue: dict[str, Any], beats: list[dict[str, Any]]) -> list[int]:
    numbers = [
        item.strip()
        for item in str(issue.get("evidence") or "").split(",")
        if item.strip()
    ]
    return [
        block_index
        for block_index, beat in enumerate(beats)
        if any(
            re.search(rf"(?<!\w){re.escape(number)}(?!\w)", str(beat.get("text") or ""))
            for number in numbers
        )
    ]


def _issue_blocks(issue: Any, beats: list[dict[str, Any]]) -> list[int]:
    """Blocks one QA finding asks to rewrite, most specific evidence first."""
    if not isinstance(issue, dict):
        return []
    kind = issue.get("type")
    if kind == "semantic_repetition":
        # Keep the first two factual mentions and rewrite later repetitions.
        return _valid_block_indices(issue, beats)[2:]
    if kind in _GROUPED_CLAIM_TYPES:
        affected_blocks = _valid_block_indices(issue, beats)
        if affected_blocks:
            return affected_blocks
    index = issue.get("block_index")
    if isinstance(index, int) and 0 <= index < len(beats):
        return [index]
    if kind == "duration_or_coverage":
        return _blocks_outside_budget(beats) or list(range(len(beats)))
    evidence = str(issue.get("evidence") or "").strip()
    if kind == "source_cta" and evidence:
        return [
            block_index
            for block_index, beat in enumerate(beats)
            if evidence.casefold() in str(beat.get("text") or "").casefold()
        ]
    if kind == "unsupported_numbers":
        return _blocks_mentioning_numbers(issue, beats)
    return []


def issue_block_indexes(script: dict[str, Any], report: dict[str, Any]) -> list[int]:
    """Blocks the next repair pass should rewrite, in report priority order."""
    beats = [item for item in (script.get("beats") or []) if isinstance(item, dict)]
    indexes: list[int] = []
    for issue in report.get("issues") or []:
        indexes.extend(_issue_blocks(issue, beats))
    # Preserve report priority and keep a pass bounded even on a very long plan.
    return list(dict.fromkeys(indexes))[:6]


def has_blocking_length_issue(report: dict[str, Any]) -> bool:
    return any(
        isinstance(issue, dict)
        and issue.get("type") == "duration_or_coverage"
        and issue.get("severity") in BLOCKING_SEVERITIES
        for issue in (report.get("issues") or [])
    )


def _issue_key(issue: dict[str, Any]) -> tuple[str, str, int | None, str, int]:
    return (
        str(issue.get("type") or ""),
        str(issue.get("severity") or ""),
        issue.get("block_index") if isinstance(issue.get("block_index"), int) else None,
        str(issue.get("evidence") or "").strip().casefold(),
        int(issue.get("occurrence_count") or 0),
    )


def _issue_label(issue: dict[str, Any]) -> str:
    block = issue.get("block_index")
    location = f"блок {block + 1}" if isinstance(block, int) else "весь сценарий"
    return f"{issue.get('type') or 'issue'} ({location})"


def report_delta(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Human-readable list of findings resolved and introduced by a change."""
    before_map = {_issue_key(item): item for item in _report_issues(before)}
    after_map = {_issue_key(item): item for item in _report_issues(after)}
    resolved = [_issue_label(before_map[key]) for key in before_map.keys() - after_map.keys()]
    appeared = [_issue_label(after_map[key]) for key in after_map.keys() - before_map.keys()]
    parts = []
    if resolved:
        parts.append("устранено: " + ", ".join(resolved[:5]))
    if appeared:
        parts.append("появилось: " + ", ".join(appeared[:5]))
    if not parts:
        parts.append("набор замечаний не изменился")
    return "; ".join(parts)


def report_signature(report: dict[str, Any]) -> str:
    """Stable fingerprint of a report, used to skip repeated identical passes."""
    quality = report.get("quality") if isinstance(report.get("quality"), dict) else {}
    payload = {
        "issues": sorted(
            (_issue_key(item) for item in _report_issues(report)),
            key=repr,
        ),
        "actual_words": int(quality.get("actual_words") or 0),
        "target_words": int(quality.get("target_words") or 0),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _repetitions_reduced(before: dict[str, Any], after: dict[str, Any]) -> bool:
    def repetitions(report: dict[str, Any]) -> dict[str, int]:
        return {
            str(item.get("evidence") or ""): int(item.get("occurrence_count") or 0)
            for item in _report_issues(report)
            if item.get("type") == "semantic_repetition"
        }

    before_repetitions = repetitions(before)
    after_repetitions = repetitions(after)
    return any(
        after_repetitions.get(evidence, 0) < count
        for evidence, count in before_repetitions.items()
    )


def _grouped_claims_reduced(before: dict[str, Any], after: dict[str, Any]) -> bool:
    def grouped(report: dict[str, Any]) -> dict[tuple[str, str], int]:
        return {
            (
                str(item.get("type") or ""),
                str(item.get("normalized_value") or item.get("evidence") or ""),
            ): int(item.get("occurrence_count") or 0)
            for item in _report_issues(report)
            if item.get("type") in _GROUPED_CLAIM_TYPES
        }

    before_grouped = grouped(before)
    after_grouped = grouped(after)
    return any(
        after_grouped.get(key, 0) < count
        for key, count in before_grouped.items()
    )


def _closer_to_target_without_new_issue_types(before: dict[str, Any], after: dict[str, Any]) -> bool:
    # A single shorter/longer block may move the total towards the target
    # without immediately clearing the global duration issue. Allow that
    # incremental improvement only when it introduces no new issue types.
    before_quality = before.get("quality") if isinstance(before.get("quality"), dict) else {}
    after_quality = after.get("quality") if isinstance(after.get("quality"), dict) else {}
    before_distance = abs(float(before_quality.get("word_ratio") or 0) - 1.0)
    after_distance = abs(float(after_quality.get("word_ratio") or 0) - 1.0)
    return after_distance + 0.001 < before_distance and _issue_types(after) <= _issue_types(before)


def report_improved(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Whether ``after`` is strictly better than ``before`` for the agent loop."""
    before_blocking = int(before.get("blocking_issues") or 0)
    after_blocking = int(after.get("blocking_issues") or 0)
    if after_blocking > before_blocking:
        return False
    if after_blocking < before_blocking:
        return True
    before_score = int(before.get("score") or 0)
    after_score = int(after.get("score") or 0)
    if after_score != before_score:
        return after_score > before_score
    if _repetitions_reduced(before, after):
        return True
    if _grouped_claims_reduced(before, after):
        return True
    return _closer_to_target_without_new_issue_types(before, after)


# --- Candidate repairs --------------------------------------------------------


def _refresh_candidate(candidate: dict[str, Any]) -> str:
    """Rebuild final text, duration and quality from the candidate's beats."""
    beats = [dict(item) for item in (candidate.get("beats") or []) if isinstance(item, dict)]
    final_text = join_beat_texts(beats)
    candidate["final_text"] = final_text
    candidate["estimated_duration_sec"] = estimate_duration_sec(
        final_text, int(candidate.get("estimated_wpm") or DEFAULT_WPM)
    )
    candidate["quality"] = assess_script_quality(candidate)
    return final_text


def _store_agent_candidate(
    pdir: Path,
    candidate: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    beats = [dict(item) for item in (candidate.get("beats") or []) if isinstance(item, dict)]
    final_text = _refresh_candidate(candidate)
    return store_processed_script(
        pdir,
        candidate,
        source=source,
        final_text=final_text,
        analysis=dict(candidate.get("analysis") or {}),
        target_seconds=int(candidate.get("target_duration_sec") or 0),
        raw_beats=beats,
    )


def _strip_cta_and_broken_seams(beats: list[dict[str, Any]]) -> tuple[list[str], int]:
    """Edit ``beats`` in place; return removed CTA sentences and repaired seams."""
    removed: list[str] = []
    repaired_seams = 0
    for beat in beats:
        cleaned, local_removed = strip_source_cta(str(beat.get("text") or ""))
        cleaned, local_seams = clean_broken_paragraphs(cleaned)
        repaired_seams += local_seams
        if local_removed:
            removed.extend(local_removed)
        if local_removed or local_seams:
            beat["text"] = cleaned
            beat["actual_words"] = word_count(cleaned)
    return removed, repaired_seams


def _local_repair_acceptable(before: dict[str, Any], after: dict[str, Any]) -> bool:
    # Removing a source CTA shortens the script; a length finding appearing
    # as the only side effect must not block that removal.
    before_types = _issue_types(before)
    after_types = _issue_types(after)
    cta_removed = "source_cta" in before_types and "source_cta" not in after_types
    only_expected_new_issue = (after_types - before_types) <= {"duration_or_coverage"}
    return report_improved(before, after) or (cta_removed and only_expected_new_issue)


def apply_deterministic_repairs(
    pdir: Path,
    script: dict[str, Any],
    report: dict[str, Any],
    *,
    log: Log | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int, bool]:
    """Apply safe local fixes that do not require a generative model.

    Returns ``(script, report, actions, accepted)``; the candidate is persisted
    only when accepted.
    """
    if not any(
        isinstance(item, dict)
        and item.get("type") in {"source_cta", "broken_paragraph_seam"}
        for item in (report.get("issues") or [])
    ):
        return script, report, 0, False

    candidate = copy.deepcopy(script)
    beats = [dict(item) for item in (candidate.get("beats") or []) if isinstance(item, dict)]
    removed, repaired_seams = _strip_cta_and_broken_seams(beats)
    actions = len(removed) + repaired_seams
    if not actions:
        return script, report, 0, False
    candidate["beats"] = beats
    source = clean_subtitle_text(str(candidate.get("source_text") or ""))
    _refresh_candidate(candidate)
    next_report = validate_rewrite(candidate)
    if not _local_repair_acceptable(report, next_report):
        if log:
            log(
                "Агент: локальные исправления отклонены; "
                + report_delta(report, next_report)
            )
        return script, report, actions, False

    result = _store_agent_candidate(pdir, candidate, source=source)
    accepted_report = validate_rewrite(result)
    if log:
        log(
            f"Агент: локальные исправления — CTA {len(removed)}, стыки {repaired_seams}; "
            f"балл {report.get('score', 0)} → {accepted_report.get('score', 0)}; "
            + report_delta(report, accepted_report)
        )
    return result, accepted_report, actions, True


def _issues_for_block(report: dict[str, Any], index: int) -> list[dict[str, Any]]:
    return [
        item for item in _report_issues(report)
        if (
            item.get("block_index") == index
            or item.get("block_index") is None
            or index in (item.get("block_indices") or [])
        )
    ]


def _block_repair_prompt(
    beats: list[dict[str, Any]],
    index: int,
    *,
    budget: int,
    issues: list[dict[str, Any]],
    excerpt: str,
    facts: list[str],
) -> str:
    current_text = str(beats[index].get("text") or "").strip()
    problems = "\n".join(
        f"- {item.get('message')} Фрагмент: {item.get('evidence') or '—'}"
        for item in issues[:8]
    )
    previous_tail = str(beats[index - 1].get("text") or "")[-500:] if index else ""
    next_start = (
        str(beats[index + 1].get("text") or "")[:500]
        if index + 1 < len(beats) else ""
    )
    return (
        f"Отредактируй один блок документального сценария. Цель — около {budget} слов, допуск ±7%. "
        "Исправь перечисленные дефекты, сохрани подтверждённые факты и естественный переход между соседними "
        "блоками. Не добавляй внешние сведения, новые числа, призывы подписаться и технические комментарии. "
        "Верни только JSON-объект вида "
        '{"title":"точное название блока","text":"полный исправленный текст блока"}. '
        "Название должно точно отражать содержание.\n"
        f"<PROBLEMS>\n{problems}\n</PROBLEMS>\n"
        f"<FACTS>{json.dumps(facts, ensure_ascii=False)}</FACTS>\n"
        f"<SOURCE_EXCERPT>\n{excerpt}\n</SOURCE_EXCERPT>\n"
        f"<PREVIOUS_TAIL>\n{previous_tail}\n</PREVIOUS_TAIL>\n"
        f"<CURRENT_BLOCK>\n{current_text}\n</CURRENT_BLOCK>\n"
        f"<NEXT_START>\n{next_start}\n</NEXT_START>"
    )


def repair_blocks(
    pdir: Path,
    script: dict[str, Any],
    report: dict[str, Any],
    settings: Any,
    *,
    log: Log | None = None,
    cancel_check: CancelCheck | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int, bool]:
    """Repair blocks one by one; never discard a good block with a bad batch.

    Returns ``(script, report, attempted, any_accepted)``.  Each accepted
    block is persisted immediately.
    """
    beats = [dict(item) for item in (script.get("beats") or []) if isinstance(item, dict)]
    if not beats:
        return script, report, 0, False
    indexes = issue_block_indexes(script, report)
    if not indexes:
        return script, report, 0, False

    source = clean_subtitle_text(str(script.get("source_text") or ""))
    excerpts = source_sections(source, len(beats))
    facts = analysis_facts(script)
    working_script = copy.deepcopy(script)
    working_report = report
    attempted = 0
    accepted = 0

    for order, index in enumerate(indexes, start=1):
        if cancel_check:
            cancel_check()
        current_beats = [
            dict(item)
            for item in (working_script.get("beats") or [])
            if isinstance(item, dict)
        ]
        if index >= len(current_beats):
            continue
        beat = current_beats[index]
        current_text = str(beat.get("text") or "").strip()
        if not current_text:
            continue
        budget = int(beat.get("target_words") or word_count(current_text))
        block_issues = _issues_for_block(working_report, index)
        if not block_issues:
            continue
        if log:
            log(f"Агент: исправление блока {index + 1} ({order}/{len(indexes)})")
        prompt = _block_repair_prompt(
            current_beats,
            index,
            budget=budget,
            issues=block_issues,
            excerpt=excerpts[index],
            facts=facts_for_excerpt(facts, excerpts[index]),
        )
        try:
            attempted += 1
            raw = lm_text(
                prompt,
                "Ты строгий редактор. Отвечай только валидным JSON и не выдумывай факты.",
                settings,
                timeout=600,
                max_tokens=max(768, min(4096, round(max(100, budget) * 3.2))),
                log=log,
            )
            data = lm.extract_json(raw)
            text = str(data.get("text") or "").strip() if isinstance(data, dict) else ""
            title = str(data.get("title") or "").strip() if isinstance(data, dict) else ""
        except Exception as exc:
            if log:
                log(f"Агент: блок {index + 1} пропущен — {exc}")
            continue
        actual = word_count(text)
        if not text or actual < round(max(1, budget) * 0.75) or actual > round(max(1, budget) * 1.25):
            if log:
                log(
                    f"Агент: блок {index + 1} отклонён — объём {actual} слов "
                    f"при цели {budget}"
                )
            continue
        beat["text"] = text
        beat["title"] = title or str(beat.get("title") or f"Смысловой блок {index + 1}")
        beat["actual_words"] = actual
        beat["complete"] = True
        candidate = copy.deepcopy(working_script)
        candidate["beats"] = current_beats
        _refresh_candidate(candidate)
        candidate_report = validate_rewrite(candidate)
        if not report_improved(working_report, candidate_report):
            if log:
                log(
                    f"Агент: блок {index + 1} отклонён, балл "
                    f"{working_report.get('score', 0)} → {candidate_report.get('score', 0)}; "
                    + report_delta(working_report, candidate_report)
                )
            continue

        before_report = working_report
        working_script = _store_agent_candidate(pdir, candidate, source=source)
        working_report = validate_rewrite(working_script)
        accepted += 1
        if log:
            log(
                f"Агент: блок {index + 1} принят, балл "
                f"{before_report.get('score', 0)} → {working_report.get('score', 0)}; "
                + report_delta(before_report, working_report)
            )

    if log and attempted:
        log(
            f"Агент: проход по блокам завершён — принято {accepted} из {attempted}"
        )
    return working_script, working_report, attempted, accepted > 0


# --- Agent run ------------------------------------------------------------------


@dataclass(frozen=True)
class AgentOutcome:
    """Final script of a completed run and the summary worth recording."""

    script: dict[str, Any]
    status: str
    message: str
    score: Any
    blocking_issues: int
    warnings: int


@dataclass(frozen=True)
class _PreviousCheck:
    """What the last completed run recorded, to skip an identical re-check."""

    status: Any
    signature: str
    checked_revision: int
    input_revision: int

    def matches(self, report: dict[str, Any]) -> bool:
        return bool(
            self.status in {"needs_review", "ready_with_warnings", "ready"}
            and self.signature
            and self.signature == report_signature(report)
            and self.checked_revision == self.input_revision
        )


class RewriteAgentRun:
    """One bounded agent run over a project's rewrite script.

    The caller owns the project lock and run log.  This object keeps the
    in-flight script so that, after :meth:`run` raises, the caller can still
    record the interruption (:meth:`record_cancelled` / :meth:`record_failed`)
    and release LM Studio (:meth:`release_lm`) in its own cleanup order.
    """

    def __init__(
        self,
        settings: Any,
        *,
        progress: Progress | None = None,
        max_iterations: int = AGENT_MAX_ITERATIONS,
    ):
        self.settings = settings
        self.max_iterations = max_iterations
        self.script: dict[str, Any] | None = None
        self.used_lm = False
        self._progress = progress
        self._pdir: Path | None = None
        self._log: Log | None = None
        self._cancel_check: CancelCheck | None = None
        self._history: list[dict[str, Any]] = []

    def run(
        self,
        project: Project,
        *,
        rewrite_style: str,
        target_minutes: int | None,
        regenerate: bool,
        log: Log,
        cancel_check: CancelCheck | None = None,
    ) -> AgentOutcome:
        self._pdir = project.dir
        self._log = log
        self._cancel_check = cancel_check

        previous = self._prepare(target_minutes)
        if regenerate or not str(self.script.get("final_text") or "").strip():
            self._create_draft(rewrite_style)
        report = self._initial_check()
        report, changed = self._apply_local_repairs(report, kind="deterministic")
        if changed:
            self._save(report)

        if previous.matches(report):
            log(
                "Агент: сценарий и замечания не изменились после прошлого запуска — "
                "повторные запросы к LM Studio пропущены"
            )
        else:
            if has_blocking_length_issue(report):
                report = self._repair_length()
            report = self._editorial_passes(report)
        return self._finish(project)

    def record_cancelled(self) -> None:
        self._record_interruption("cancelled", "Отменено")

    def record_failed(self) -> None:
        self._record_interruption("failed", "Ошибка")

    def release_lm(self) -> None:
        if self.used_lm:
            unload_lm_after_script(self.settings, self._log)

    # Steps ------------------------------------------------------------------

    def _prepare(self, target_minutes: int | None) -> _PreviousCheck:
        script = apply_requested_duration(read_script(self._pdir), target_minutes)
        self.script = script
        if script.get("mode") != "rewrite":
            raise ValueError("Агент-редактор доступен только в режиме «Рерайт»")
        if not str(script.get("source_text") or "").strip():
            raise ValueError("Сначала добавьте исходный текст или субтитры")
        previous_agent = (
            dict(script.get("agent"))
            if isinstance(script.get("agent"), dict)
            else {}
        )
        input_revision = int(script.get("revision", 0) or 0)
        previous_signature = str(previous_agent.get("last_report_signature") or "")
        previous_checked_revision = int(previous_agent.get("last_checked_revision", -1) or -1)
        previous = _PreviousCheck(
            status=previous_agent.get("status"),
            signature=previous_signature,
            checked_revision=previous_checked_revision,
            input_revision=input_revision,
        )
        self._history = [
            dict(item)
            for item in (previous_agent.get("history") or [])
            if isinstance(item, dict)
        ][-12:]
        agent_payload(
            script,
            status="running",
            stage="Подготовка",
            progress=2,
            iteration=0,
            max_iterations=self.max_iterations,
            report={"score": 0, "issues": []},
            history=self._history,
        )
        write_script(self._pdir, script)
        self._emit("Анализ исходного материала", 5, "Агент: запуск контролируемого цикла")
        self._check_cancel()
        return previous

    def _create_draft(self, rewrite_style: str) -> None:
        self.used_lm = True
        self._emit("Создание черновика", 12, "Агент: создаю первичный сценарий по смысловым блокам")
        self.script = process_script(
            self._pdir,
            self.script,
            rewrite_style,
            settings=self.settings,
            log=self._log,
            cancel_check=self._cancel_check,
            manage_lifecycle=False,
        )
        self._emit("Первичная проверка", 42, "Агент: черновик сохранён, запускаю независимый контроль")

    def _initial_check(self) -> dict[str, Any]:
        report = validate_rewrite(self.script)
        agent_payload(self.script, report=report)
        write_script(self._pdir, self.script)
        self._log(
            f"Агент: контроль — {report['score']}/100, "
            f"критичных проблем {report['blocking_issues']}, предупреждений {report['warnings']}"
        )
        return report

    def _apply_local_repairs(self, report: dict[str, Any], *, kind: str) -> tuple[dict[str, Any], bool]:
        self.script, report, actions, changed = apply_deterministic_repairs(
            self._pdir,
            self.script,
            report,
            log=self._log,
        )
        if changed:
            self._history.append({
                "iteration": 0,
                "kind": kind,
                "actions": actions,
                "after_score": int(report.get("score") or 0),
                "accepted": True,
            })
        return report, changed

    def _repair_length(self) -> dict[str, Any]:
        self._check_cancel()
        self.used_lm = True
        self._emit("Доведение объёма", 50, "Агент: объём вне допуска, дополняю незавершённые блоки")
        self.script = expand_script(
            self._pdir,
            self.script,
            settings=self.settings,
            log=self._log,
            cancel_check=self._cancel_check,
            manage_lifecycle=False,
        )
        report = validate_rewrite(self.script)
        report, _changed = self._apply_local_repairs(report, kind="deterministic_after_length")
        self._save(report)
        return report

    def _editorial_passes(self, report: dict[str, Any]) -> dict[str, Any]:
        seen_reports: set[str] = set()
        for iteration in range(1, self.max_iterations + 1):
            self._check_cancel()
            blocking_issues = [
                issue
                for issue in (report.get("issues") or [])
                if isinstance(issue, dict)
                and issue.get("severity") in BLOCKING_SEVERITIES
            ]
            if not blocking_issues:
                break
            signature = report_signature(report)
            if signature in seen_reports:
                self._log(
                    "Агент: набор замечаний не изменился — повторный одинаковый проход отменён"
                )
                break
            seen_reports.add(signature)
            report, improved = self._editorial_pass(iteration, report, blocking_issues)
            if not improved and report_signature(report) == signature:
                self._log(
                    "Агент: проход не улучшил ни одно замечание — "
                    "повторять те же запросы нецелесообразно"
                )
                break
        return report

    def _editorial_pass(
        self,
        iteration: int,
        report: dict[str, Any],
        blocking_issues: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        progress = 55 + round((iteration - 1) / self.max_iterations * 35)
        agent_payload(self.script, iteration=iteration, report=report, history=self._history)
        self._emit(
            f"Редакторский проход {iteration}/{self.max_iterations}",
            progress,
            f"Агент: проход {iteration}/{self.max_iterations}\n{issue_summary(report)}",
        )
        self.used_lm = True
        before_score = int(report.get("score") or 0)
        before_blocking = int(report.get("blocking_issues") or 0)
        repair_report = {**report, "issues": blocking_issues}
        self.script, next_report, actions, improved = repair_blocks(
            self._pdir,
            self.script,
            repair_report,
            self.settings,
            log=self._log,
            cancel_check=self._cancel_check,
        )
        self._history.append({
            "iteration": iteration,
            "before_score": before_score,
            "after_score": int(next_report.get("score") or 0),
            "before_blocking": before_blocking,
            "after_blocking": int(next_report.get("blocking_issues") or 0),
            "actions": actions,
            "accepted": improved,
        })
        self._save(next_report, iteration=iteration)
        return next_report, improved

    def _finish(self, project: Project) -> AgentOutcome:
        self._check_cancel()
        report = validate_rewrite(self.script)
        blocking = int(report.get("blocking_issues") or 0)
        warnings = int(report.get("warnings") or 0)
        notice_count = len(report.get("issues") or [])
        status, stage = agent_release_status(report)
        agent_payload(
            self.script,
            status=status,
            stage=stage,
            progress=100,
            report=report,
            history=self._history,
        )
        self.script["agent"]["last_report_signature"] = report_signature(report)
        self.script["agent"]["last_checked_revision"] = int(self.script.get("revision", 0) or 0)
        write_script(self._pdir, self.script)
        if self._progress:
            self._progress(stage, 100)
        project.mark_stage_completed(
            "script",
            self.script,
            metadata={
                "agent_status": status,
                "agent_score": report.get("score"),
                "notices": notice_count,
                "important_notices": blocking,
            },
        )
        final_message = (
            f"Агент: сценарий готов, балл {report['score']}/100"
            + (f", замечаний {notice_count}" if notice_count else "")
        )
        self._log(final_message)
        return AgentOutcome(
            script=self.script,
            status=status,
            message=final_message,
            score=report.get("score"),
            blocking_issues=blocking,
            warnings=warnings,
        )

    # Plumbing ---------------------------------------------------------------

    def _emit(self, stage: str, progress: int, message: str | None = None) -> None:
        if message:
            self._log(message)
        if self.script is not None:
            agent_payload(
                self.script,
                status="running",
                stage=stage,
                progress=progress,
                max_iterations=self.max_iterations,
            )
            write_script(self._pdir, self.script)
        if self._progress:
            self._progress(stage, progress)

    def _save(self, report: dict[str, Any], **fields: Any) -> None:
        agent_payload(self.script, report=report, history=self._history, **fields)
        write_script(self._pdir, self.script)

    def _check_cancel(self) -> None:
        if self._cancel_check:
            self._cancel_check()

    def _record_interruption(self, status: str, stage: str) -> None:
        if self.script is not None:
            agent_payload(self.script, status=status, stage=stage, progress=100)
            write_script(self._pdir, self.script)


__all__ = [
    "AGENT_MAX_ITERATIONS",
    "BLOCKING_SEVERITIES",
    "AgentOutcome",
    "RewriteAgentRun",
    "agent_payload",
    "agent_release_status",
    "apply_deterministic_repairs",
    "has_blocking_length_issue",
    "issue_block_indexes",
    "issue_summary",
    "repair_blocks",
    "report_delta",
    "report_improved",
    "report_signature",
    "strip_source_cta",
    "validate_rewrite",
]
