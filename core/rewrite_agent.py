"""Deterministic editorial QA for the bounded rewrite agent.

The language model may propose or perform repairs, but it is never trusted to
declare its own output production-ready.  This module is deliberately free of
network and project-storage dependencies so every agent pass can be scored and
tested reproducibly.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

from core.script_workflow import assess_script_quality, word_count


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


__all__ = ["BLOCKING_SEVERITIES", "issue_summary", "strip_source_cta", "validate_rewrite"]
