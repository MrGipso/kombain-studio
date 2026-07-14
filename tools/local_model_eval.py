r"""Read-only A/B harness for local OpenAI-compatible chat/VLM models.

The harness never calls Pipeline Studio generation endpoints and never writes
inside ``projects/``.  It reads immutable text/image fixtures, runs them one at
a time against LM Studio (or another OpenAI-compatible endpoint), and writes a
fully auditable report under ``artifacts/local-model-eval`` by default.

Examples::

    .\.venv\Scripts\python.exe tools\local_model_eval.py --model gemma-4-26b-a4b-it-qat --dry-run
    .\.venv\Scripts\python.exe tools\local_model_eval.py --model gemma-4-26b-a4b-it-qat \
        --base-url http://127.0.0.1:1234/v1

Supplying ``--model`` is the explicit opt-in to active inference.  Requests are
strictly sequential; ``--concurrency`` accepts only ``1`` so an eval cannot
compete with itself for the same GPU.
"""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.lm import extract_json, explain_error  # noqa: E402


DEFAULT_CASES = ROOT / "tools" / "local_model_eval_cases.v3.json"
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts" / "local-model-eval"
SUPPORTED_MODES = {"text", "image"}
THINKING_MODES = {"default", "on", "off"}
RESERVED_REQUEST_FIELDS = {
    "chat_template_kwargs",
    "enable_thinking",
    "max_tokens",
    "max_completion_tokens",
    "messages",
    "model",
    "n",
    "response_format",
    "reasoning",
    "reasoning_effort",
    "seed",
    "stream",
    "temperature",
    "tool_choice",
    "tools",
    "ttl",
}
MISSING = object()


class EvalError(RuntimeError):
    """A fixture, transport, or safety failure with a concise explanation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-.")
    return cleaned or "model"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_output_dir(path: Path) -> Path:
    """Resolve an output directory and reject writes inside real projects."""
    resolved = path.expanduser().resolve()
    projects = (ROOT / "projects").resolve()
    if resolved == projects or _is_relative_to(resolved, projects):
        raise EvalError("Eval output cannot be inside projects/")
    return resolved


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_suite(path: Path) -> dict[str, Any]:
    try:
        source = path.read_bytes()
        suite = json.loads(source.decode("utf-8"))
    except FileNotFoundError as exc:
        raise EvalError(f"Cases file not found: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvalError(f"Cases file is unreadable: {path}") from exc
    if not isinstance(suite, dict) or suite.get("schema_version") != 1:
        raise EvalError("Cases file must be a schema_version=1 JSON object")
    # The digest belongs to the exact bytes parsed above.  Computing it after a
    # long inference run could accidentally identify a newer suite revision.
    suite["_source_sha256"] = hashlib.sha256(source).hexdigest()
    try:
        contract_major = int(str(suite.get("suite_version", "1")).split(".", 1)[0])
    except ValueError:
        contract_major = 0
    strict_contract = contract_major >= 2
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalError("Cases file must contain a non-empty cases array")
    schema_library = suite.get("schemas") or {}
    tool_library = suite.get("tool_sets") or {}
    ids: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise EvalError(f"Case #{index + 1} is not an object")
        case_id = str(case.get("id", "")).strip()
        if not case_id or case_id in ids:
            raise EvalError(f"Case id is empty or duplicated: {case_id!r}")
        ids.add(case_id)
        if case.get("mode", "text") not in SUPPORTED_MODES:
            raise EvalError(f"Unsupported mode in {case_id}: {case.get('mode')}")
        if not str(case.get("instruction", "")).strip():
            raise EvalError(f"Case {case_id} has no instruction")
        schema_ref = case.get("response_schema_ref")
        if schema_ref:
            if schema_ref not in schema_library:
                raise EvalError(f"Unknown response_schema_ref in {case_id}: {schema_ref}")
            case["response_schema"] = deepcopy(schema_library[schema_ref])
        tools_ref = case.get("tools_ref")
        if tools_ref:
            refs = [tools_ref] if isinstance(tools_ref, str) else list(tools_ref)
            expanded_tools: list[dict[str, Any]] = []
            for ref in refs:
                if ref not in tool_library:
                    raise EvalError(f"Unknown tools_ref in {case_id}: {ref}")
                expanded_tools.extend(deepcopy(tool_library[ref]))
            for tool in expanded_tools:
                function = tool.get("function") if isinstance(tool, dict) else None
                if not isinstance(function, dict):
                    raise EvalError(f"Malformed tool in {case_id}")
                parameters_ref = function.pop("parameters_ref", None)
                if parameters_ref:
                    if parameters_ref not in schema_library:
                        raise EvalError(
                            f"Unknown tool parameters_ref in {case_id}: {parameters_ref}"
                        )
                    if "parameters" in function:
                        raise EvalError(
                            f"Tool in {case_id} cannot define parameters and parameters_ref"
                        )
                    function["parameters"] = deepcopy(schema_library[parameters_ref])
            case["tools"] = expanded_tools
        scoring_schema = case.get("response_schema")
        if scoring_schema is not None and not isinstance(scoring_schema, dict):
            raise EvalError(f"Case {case_id} response_schema must be an object")
        if (
            strict_contract
            and isinstance(scoring_schema, dict)
            and case.get("schema_target") != "tool_arguments"
        ):
            if case.get("structured_output") is not True:
                raise EvalError(
                    f"Case {case_id} scores a response schema but does not expose it "
                    "through structured_output=true"
                )
        if strict_contract and case.get("schema_target") == "tool_arguments":
            if not isinstance(scoring_schema, dict):
                raise EvalError(
                    f"Case {case_id} scores tool arguments without a response_schema"
                )
            expected_tool = str(case.get("expected_tool", ""))
            matching = [
                tool
                for tool in case.get("tools", [])
                if str((tool.get("function") or {}).get("name", ""))
                == expected_tool
            ]
            if len(matching) != 1:
                raise EvalError(
                    f"Case {case_id} must expose exactly one expected tool "
                    f"named {expected_tool!r}"
                )
            parameters = (matching[0].get("function") or {}).get("parameters")
            if parameters != scoring_schema:
                raise EvalError(
                    f"Case {case_id} tool parameters differ from its scoring schema"
                )
    return suite


def _validate_extra_request_fields(
    fields: dict[str, Any], *, source: str
) -> dict[str, Any]:
    reserved = sorted(RESERVED_REQUEST_FIELDS & set(fields))
    if reserved:
        raise EvalError(
            f"{source} cannot override controlled request fields: "
            + ", ".join(reserved)
        )
    return fields


def parse_extra_request_fields(value: str | None) -> dict[str, Any]:
    """Parse a JSON object or ``@path.json`` without changing case identity."""
    text = str(value or "").strip()
    if not text:
        return {}
    if text.startswith("@"):
        source = Path(text[1:]).expanduser().resolve()
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise EvalError(f"Cannot read extra request fields: {source}") from exc
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvalError("--extra-request-fields must be a JSON object or @file") from exc
    if not isinstance(result, dict):
        raise EvalError("--extra-request-fields must decode to an object")
    return _validate_extra_request_fields(result, source="Extra request fields")


def resolve_media_path(value: str) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if not path.is_file():
        raise EvalError(f"Image fixture not found: {path}")
    return path


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        raise EvalError(f"Only image fixtures are accepted, got {path.name}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _user_text(case: dict[str, Any]) -> str:
    instruction = str(case["instruction"]).strip()
    input_data = case.get("input")
    if input_data is None:
        return instruction
    return (
        instruction
        + "\n\nInput JSON:\n"
        + json.dumps(input_data, ensure_ascii=False, indent=2, sort_keys=True)
    )


def build_request_payload(
    case: dict[str, Any],
    *,
    model: str,
    ttl: int | None,
    seed: int | None,
    thinking_mode: str = "default",
    extra_request_fields: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if thinking_mode not in THINKING_MODES:
        raise EvalError(f"Unsupported thinking mode: {thinking_mode}")
    system = str(
        case.get("system")
        or "Follow the task exactly. Return no markdown unless explicitly requested."
    )
    text = _user_text(case)
    media_records: list[dict[str, Any]] = []
    if case.get("mode", "text") == "image":
        values = case.get("images") or ([case["image"]] if case.get("image") else [])
        if not values:
            raise EvalError(f"Image case {case['id']} has no image fixture")
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        expected_hashes = case.get("image_sha256") or []
        if isinstance(expected_hashes, str):
            expected_hashes = [expected_hashes]
        for index, value in enumerate(values):
            path = resolve_media_path(str(value))
            digest = sha256_file(path)
            if index < len(expected_hashes):
                expected = str(expected_hashes[index]).lower()
                if expected and digest != expected:
                    raise EvalError(
                        f"Fixture drift in {case['id']}: {path.name} SHA-256 "
                        f"is {digest}, expected {expected}"
                    )
            media_records.append(
                {"path": str(path), "sha256": digest, "size": path.stat().st_size}
            )
            content.append(
                {"type": "image_url", "image_url": {"url": _data_url(path)}}
            )
        user_content: Any = content
    else:
        user_content = text

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": float(case.get("temperature", 0.1)),
        "max_tokens": int(case.get("max_tokens", 1200)),
        "stream": False,
    }
    if ttl is not None and ttl > 0:
        payload["ttl"] = int(ttl)
    if seed is not None:
        payload["seed"] = int(seed)
    tools = case.get("tools")
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = case.get("tool_choice", "auto")
    if case.get("structured_output") and case.get("response_schema"):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": _slug(case["id"]).replace("-", "_")[:64],
                "strict": True,
                "schema": case["response_schema"],
            },
        }
    if thinking_mode != "default":
        payload["chat_template_kwargs"] = {
            "enable_thinking": thinking_mode == "on"
        }
    case_extra = case.get("extra_request_fields") or {}
    if not isinstance(case_extra, dict):
        raise EvalError(f"Case {case['id']} extra_request_fields is not an object")
    _validate_extra_request_fields(
        case_extra, source=f"Case {case['id']} extra_request_fields"
    )
    _validate_extra_request_fields(
        extra_request_fields or {}, source="Extra request fields"
    )
    payload.update(deepcopy(case_extra))
    payload.update(deepcopy(extra_request_fields or {}))
    return payload, media_records


def effective_request_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Record the actual controlled knobs without messages or full tool schemas."""
    keys = (
        "model",
        "temperature",
        "max_tokens",
        "seed",
        "ttl",
        "chat_template_kwargs",
        "tool_choice",
    )
    result = {key: deepcopy(payload[key]) for key in keys if key in payload}
    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        json_schema = response_format.get("json_schema") or {}
        result["response_format"] = {
            "type": response_format.get("type"),
            "name": json_schema.get("name") if isinstance(json_schema, dict) else None,
            "strict": json_schema.get("strict") if isinstance(json_schema, dict) else None,
        }
    result["tools"] = [
        str((tool.get("function") or {}).get("name", ""))
        for tool in payload.get("tools", [])
        if isinstance(tool, dict)
    ]
    known = set(keys) | {"messages", "response_format", "stream", "tools"}
    result["extra_request_fields"] = {
        key: deepcopy(value) for key, value in payload.items() if key not in known
    }
    return result


def _redact_data_urls(value: Any) -> Any:
    """Keep raw request structure without duplicating multi-megabyte base64."""
    if isinstance(value, dict):
        return {key: _redact_data_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_data_urls(item) for item in value]
    if isinstance(value, str) and value.startswith("data:image/") and ";base64," in value:
        header, encoded = value.split(",", 1)
        try:
            raw = base64.b64decode(encoded, validate=True)
            digest = hashlib.sha256(raw).hexdigest()
            return f"{header},<omitted bytes={len(raw)} sha256={digest}>"
        except Exception:
            return value.split(",", 1)[0] + ",<invalid base64 omitted>"
    return value


def post_chat(base_url: str, payload: dict[str, Any], timeout: float) -> tuple[dict, float]:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise EvalError(f"LM HTTP {exc.code}: {body[-2000:]}") from exc
    except Exception as exc:
        raise EvalError(explain_error(exc)) from exc
    elapsed = time.perf_counter() - started
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EvalError(f"LM returned non-JSON HTTP body: {raw[-1000:]}") from exc
    if not isinstance(data, dict):
        raise EvalError("LM response body is not a JSON object")
    return data, elapsed


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def normalize_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        return result
    for call in raw_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = str(function.get("name", ""))
        arguments_raw = function.get("arguments", {})
        if isinstance(arguments_raw, str):
            try:
                arguments = json.loads(arguments_raw)
                parse_error = ""
            except json.JSONDecodeError as exc:
                arguments = None
                parse_error = str(exc)
        elif isinstance(arguments_raw, dict):
            arguments = arguments_raw
            parse_error = ""
        else:
            arguments = None
            parse_error = "arguments are neither an object nor JSON text"
        result.append(
            {
                "id": str(call.get("id", "")),
                "name": name,
                "arguments": arguments,
                "arguments_raw": arguments_raw,
                "parse_error": parse_error,
            }
        )
    return result


def parse_json_response(content: str) -> tuple[Any | None, str]:
    if not str(content or "").strip():
        return None, "assistant content is empty"
    try:
        return extract_json(content), ""
    except (ValueError, json.JSONDecodeError) as exc:
        return None, str(exc)


def parse_assistant_response(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise EvalError("LM response has no choices[0]")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise EvalError("LM response has no assistant message")
    content = _message_text(message.get("content"))
    reasoning_content = _message_text(
        message.get("reasoning_content", message.get("reasoning"))
    )
    parsed, parse_error = parse_json_response(content)
    return {
        "content": content,
        "reasoning_content": reasoning_content,
        "parsed": parsed,
        "parse_error": parse_error,
        "tool_calls": normalize_tool_calls(message),
        "finish_reason": choices[0].get("finish_reason"),
    }


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate the JSON-Schema subset used by the immutable eval cases."""
    errors: list[str] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if not any(_type_matches(value, item) for item in expected_type):
            return [f"{path}: expected one of {expected_type}, got {type(value).__name__}"]
    elif isinstance(expected_type, str) and not _type_matches(value, expected_type):
        return [f"{path}: expected {expected_type}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value {value!r} is not in enum")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key}: required property is missing")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key}: additional property is forbidden")
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                errors.extend(validate_schema(item, child_schema, f"{path}.{key}"))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: expected at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: expected at most {schema['maxItems']} items")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            errors.append(f"{path}: array items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, item_schema, f"{path}[{index}]"))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{path}: string is shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path}: string is longer than {schema['maxLength']}")
        if schema.get("pattern") and not re.search(str(schema["pattern"]), value):
            errors.append(f"{path}: string does not match required pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: value is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: value is above maximum {schema['maximum']}")
    return errors


def _get_path(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return MISSING
    return current


def _first_tool(tool_calls: list[dict[str, Any]], name: str = "") -> dict[str, Any] | None:
    for call in tool_calls:
        if not name or call.get("name") == name:
            return call
    return None


def _english_ratio(value: Any) -> float:
    text = str(value if value is not MISSING else "")
    latin = len(re.findall(r"[A-Za-z]", text))
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
    return latin / max(1, latin + cyrillic)


def _array_intersection(actual: Any, expected: Any) -> list[Any]:
    """Return equality matches without assuming model values are hashable."""
    actual_values = actual if isinstance(actual, list) else []
    expected_values = expected if isinstance(expected, list) else []
    return [
        candidate
        for candidate in expected_values
        if any(item == candidate for item in actual_values)
    ]


def _qa_consistency(value: Any) -> tuple[bool, dict[str, Any]]:
    """Reject internally contradictory QA verdict/score/issue combinations."""
    if not isinstance(value, dict):
        return False, {"reason": "QA payload is not an object"}
    verdict = value.get("verdict")
    issues = value.get("issues")
    score = value.get("overall_score")
    if not isinstance(issues, list) or not _type_matches(score, "number"):
        return False, {"verdict": verdict, "issues": issues, "overall_score": score}
    has_none = any(item == "none" for item in issues)
    substantive = [item for item in issues if item != "none"]
    consistent = verdict in {"accept", "needs_review", "retry_image", "retry_video"}
    if has_none and substantive:
        consistent = False
    if verdict == "accept" and (substantive or score < 0.65):
        consistent = False
    if verdict == "needs_review" and (not substantive or score > 0.8):
        consistent = False
    if verdict in {"retry_image", "retry_video"} and (not substantive or score > 0.8):
        consistent = False
    return consistent, {
        "verdict": verdict,
        "issues": issues,
        "overall_score": score,
    }


def evaluate_check(
    check: dict[str, Any], parsed: Any, tool_calls: list[dict[str, Any]]
) -> tuple[bool, Any]:
    kind = str(check.get("kind", ""))
    path = str(check.get("path", ""))
    value = _get_path(parsed, path) if parsed is not None else MISSING
    if kind == "equals":
        return value == check.get("value"), None if value is MISSING else value
    if kind == "in":
        return value in (check.get("values") or []), None if value is MISSING else value
    if kind == "not_in":
        return value is not MISSING and value not in (check.get("values") or []), None if value is MISSING else value
    if kind == "nonempty":
        passed = value is not MISSING and bool(str(value).strip())
        return passed, None if value is MISSING else value
    if kind in {"regex", "not_regex"}:
        if value is MISSING:
            return False, None
        matched = bool(
            re.search(str(check.get("pattern", "")), str(value), flags=re.IGNORECASE)
        )
        return (matched if kind == "regex" else not matched), None if value is MISSING else value
    if kind == "word_count":
        count = len(re.findall(r"\b[\w'-]+\b", str(value if value is not MISSING else "")))
        return int(check.get("min", 0)) <= count <= int(check.get("max", 10**9)), count
    if kind == "sentence_count":
        count = len([part for part in re.split(r"[.!?]+", str(value if value is not MISSING else "")) if part.strip()])
        return int(check.get("min", 0)) <= count <= int(check.get("max", 10**9)), count
    if kind == "english_ratio":
        ratio = _english_ratio(value)
        return ratio >= float(check.get("min", 0.9)), round(ratio, 3)
    if kind == "array_contains_any":
        matches = _array_intersection(value, check.get("values") or [])
        return bool(matches), matches
    if kind == "array_empty":
        return isinstance(value, list) and not value, None if value is MISSING else value
    if kind == "qa_consistency":
        return _qa_consistency(parsed)
    if kind == "tool_count":
        count = len(tool_calls)
        return count == int(check.get("value", 0)), count
    if kind == "tool_name":
        name = str(check.get("value", ""))
        return _first_tool(tool_calls, name) is not None, [call.get("name") for call in tool_calls]
    if kind in {
        "tool_arg_equals",
        "tool_arg_in",
        "tool_arg_array_contains_any",
        "tool_arg_qa_consistency",
    }:
        call = _first_tool(tool_calls, str(check.get("tool", "")))
        arguments = call.get("arguments") if call else None
        actual = _get_path(arguments, path) if isinstance(arguments, dict) else MISSING
        if kind == "tool_arg_equals":
            passed = actual == check.get("value")
        elif kind == "tool_arg_in":
            passed = actual in (check.get("values") or [])
        elif kind == "tool_arg_array_contains_any":
            passed = bool(_array_intersection(actual, check.get("values") or []))
        else:
            passed, observed = _qa_consistency(arguments)
            return passed, observed
        return passed, None if actual is MISSING else actual
    raise EvalError(f"Unknown scoring check: {kind}")


def score_response(
    case: dict[str, Any], parsed: Any, tool_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    schema = case.get("response_schema")
    target = parsed
    if case.get("schema_target") == "tool_arguments":
        expected_tool = str(case.get("expected_tool", ""))
        call = _first_tool(tool_calls, expected_tool)
        target = call.get("arguments") if call else None
    schema_errors = validate_schema(target, schema) if isinstance(schema, dict) else []
    earned = 0.0
    possible = 0.0
    checks_earned = 0.0
    checks_possible = 0.0
    hard_failures: list[str] = []
    if isinstance(schema, dict):
        weight = float(case.get("schema_weight", 2.0))
        possible += weight
        if not schema_errors:
            earned += weight
        elif case.get("schema_hard", True):
            hard_failures.append("schema")
    outcomes = []
    for index, check in enumerate(case.get("checks") or []):
        weight = float(check.get("weight", 1.0))
        passed, observed = evaluate_check(check, parsed, tool_calls)
        possible += weight
        checks_possible += weight
        if passed:
            earned += weight
            checks_earned += weight
        elif check.get("hard"):
            hard_failures.append(str(check.get("name") or f"check-{index + 1}"))
        outcomes.append(
            {
                "name": str(check.get("name") or f"check-{index + 1}"),
                "kind": check.get("kind"),
                "passed": passed,
                "weight": weight,
                "observed": observed,
            }
        )
    score = earned / possible if possible else 0.0
    manual_review = bool(case.get("manual_review", False))
    provisional_passed = (
        score >= float(case.get("pass_threshold", 0.8)) and not hard_failures
    )
    return {
        "score": None if manual_review else round(score, 4),
        "passed": None if manual_review else provisional_passed,
        "provisional_score": round(score, 4) if manual_review else None,
        "provisional_passed": provisional_passed if manual_review else None,
        "earned": earned,
        "possible": possible,
        "checks_score": (
            round(checks_earned / checks_possible, 4) if checks_possible else None
        ),
        "schema_checked": isinstance(schema, dict),
        "schema_valid": not schema_errors if isinstance(schema, dict) else None,
        "schema_errors": schema_errors,
        "hard_failures": hard_failures,
        "checks": outcomes,
        "manual_review": manual_review,
    }


def _reasoning_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    direct = usage.get("reasoning_tokens")
    details = usage.get("completion_tokens_details") or {}
    nested = details.get("reasoning_tokens") if isinstance(details, dict) else None
    value = nested if isinstance(nested, int) else direct
    return int(value) if isinstance(value, int) and value >= 0 else 0


def run_case(
    case: dict[str, Any],
    *,
    model: str,
    base_url: str,
    timeout: float,
    ttl: int | None,
    seed: int | None,
    thinking_mode: str,
    extra_request_fields: dict[str, Any],
    raw_dir: Path,
) -> dict[str, Any]:
    payload, media = build_request_payload(
        case,
        model=model,
        ttl=ttl,
        seed=seed,
        thinking_mode=thinking_mode,
        extra_request_fields=extra_request_fields,
    )
    result: dict[str, Any] = {
        "id": case["id"],
        "track": case.get("track", "uncategorized"),
        "mode": case.get("mode", "text"),
        "media": media,
        "effective_request_settings": effective_request_settings(payload),
    }
    raw_payload: dict[str, Any] = {
        "case": {"id": case["id"], "track": case.get("track")},
        "request": _redact_data_urls(deepcopy(payload)),
        "media": media,
    }
    error_stage = "transport"
    try:
        response, elapsed = post_chat(base_url, payload, timeout)
        raw_payload["response"] = response
        raw_payload["elapsed_seconds"] = elapsed
        error_stage = "model_output"
        assistant = parse_assistant_response(response)
        error_stage = "harness"
        score = score_response(case, assistant["parsed"], assistant["tool_calls"])
        usage = response.get("usage") or {}
        reasoning_tokens = _reasoning_tokens(usage)
        result.update(
            {
                "ok": True,
                "elapsed_seconds": round(elapsed, 4),
                "usage": usage,
                "reasoning_tokens": reasoning_tokens,
                "assistant": assistant,
                "score": score,
            }
        )
        if thinking_mode == "off" and (
            assistant["reasoning_content"].strip() or reasoning_tokens > 0
        ):
            result["warnings"] = [
                "The OpenAI-compatible endpoint returned reasoning even though "
                "thinking_mode=off was requested. LM Studio/model chat templates "
                "may ignore chat_template_kwargs; use the native LM Studio SDK "
                "for a controlled no-thinking comparison."
            ]
        completion_tokens = usage.get("completion_tokens")
        if isinstance(completion_tokens, int) and elapsed > 0:
            result["completion_tokens_per_second"] = round(
                completion_tokens / elapsed, 3
            )
    except Exception as exc:
        manual_review = bool(case.get("manual_review", False))
        result.update(
            {
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "error_stage": error_stage,
                "score": {
                    "score": None if manual_review else 0.0,
                    "passed": None if manual_review else False,
                    "provisional_score": 0.0 if manual_review else None,
                    "provisional_passed": False if manual_review else None,
                    "hard_failures": [error_stage],
                    "manual_review": manual_review,
                },
            }
        )
        raw_payload["error"] = str(exc)
        raw_payload["error_type"] = type(exc).__name__
        raw_payload["error_stage"] = error_stage
    atomic_write_json(raw_dir / f"{_slug(case['id'])}.json", raw_payload)
    return result


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_track: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "cases": 0,
            "automated_cases": 0,
            "manual_review_cases": 0,
            "passed": 0,
            "scores": [],
        }
    )
    automated_scores: list[float] = []
    automated_check_scores: list[float] = []
    contract_results: list[bool] = []
    for item in results:
        score_data = item.get("score") or {}
        manual_review = bool(score_data.get("manual_review"))
        stats = by_track[str(item.get("track", "uncategorized"))]
        stats["cases"] += 1
        if manual_review:
            stats["manual_review_cases"] += 1
            continue
        score = float(score_data.get("score") or 0.0)
        automated_scores.append(score)
        if score_data.get("checks_score") is not None:
            automated_check_scores.append(float(score_data["checks_score"]))
        if score_data.get("schema_checked"):
            contract_results.append(bool(score_data.get("schema_valid")))
        stats["automated_cases"] += 1
        stats["scores"].append(score)
        stats["passed"] += bool(score_data.get("passed"))
    exceptions_by_stage = {
        stage: sum(
            not bool(item.get("ok")) and item.get("error_stage") == stage
            for item in results
        )
        for stage in ("transport", "model_output", "harness")
    }
    return {
        "cases": len(results),
        "runtime_errors": sum(not bool(item.get("ok")) for item in results),
        "exceptions_by_stage": exceptions_by_stage,
        "passed": sum(
            bool((item.get("score") or {}).get("passed"))
            for item in results
            if not bool((item.get("score") or {}).get("manual_review"))
        ),
        "manual_review_cases": sum(
            bool((item.get("score") or {}).get("manual_review")) for item in results
        ),
        "thinking_off_not_honored": sum(
            any("thinking_mode=off" in warning for warning in item.get("warnings", []))
            for item in results
        ),
        "automated_scored_cases": len(automated_scores),
        "automated_score": (
            round(sum(automated_scores) / len(automated_scores), 4)
            if automated_scores
            else None
        ),
        "automated_checks_score": (
            round(sum(automated_check_scores) / len(automated_check_scores), 4)
            if automated_check_scores
            else None
        ),
        "contract_cases": len(contract_results),
        "contract_passed": sum(contract_results),
        "contract_pass_rate": (
            round(sum(contract_results) / len(contract_results), 4)
            if contract_results
            else None
        ),
        "tracks": {
            track: {
                "cases": stats["cases"],
                "automated_cases": stats["automated_cases"],
                "manual_review_cases": stats["manual_review_cases"],
                "passed": stats["passed"],
                "score": (
                    round(sum(stats["scores"]) / len(stats["scores"]), 4)
                    if stats["scores"]
                    else None
                ),
            }
            for track, stats in sorted(by_track.items())
        },
    }


def _selected_cases(suite: dict[str, Any], requested: list[str]) -> list[dict[str, Any]]:
    cases = list(suite["cases"])
    if not requested:
        return cases
    wanted = set(requested)
    selected = [case for case in cases if case["id"] in wanted]
    missing = sorted(wanted - {case["id"] for case in selected})
    if missing:
        raise EvalError(f"Unknown case ids: {', '.join(missing)}")
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact model id exposed by /v1/models.")
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:1234/v1", help="OpenAI-compatible /v1 base URL."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--output",
        type=Path,
        help="New/result directory; defaults to artifacts/local-model-eval/<timestamp>-<model>.",
    )
    parser.add_argument("--case", action="append", default=[], help="Run one case id; repeatable.")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--ttl", type=int, default=1, help="LM Studio idle auto-unload TTL; 0 omits it.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--thinking-mode",
        choices=sorted(THINKING_MODES),
        default="default",
        help=(
            "Request chat-template thinking on/off, or leave the model default. "
            "Some LM Studio OpenAI-compatible templates ignore this field."
        ),
    )
    parser.add_argument(
        "--extra-request-fields",
        default="",
        help=(
            "Extra top-level request fields as a JSON object or @path.json; "
            "cannot override harness-controlled identity, schema, tools, or "
            "inference settings."
        ),
    )
    parser.add_argument("--concurrency", type=int, choices=[1], default=1)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate cases and fixture hashes without submitting inference.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    if args.timeout <= 0 or args.ttl < 0:
        print("timeout must be positive and ttl cannot be negative", file=sys.stderr)
        return 2
    try:
        cases_path = args.cases.expanduser().resolve()
        suite = load_suite(cases_path)
        cases = _selected_cases(suite, args.case)
        extra_request_fields = parse_extra_request_fields(args.extra_request_fields)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = safe_output_dir(
            args.output
            or (DEFAULT_OUTPUT_ROOT / f"{stamp}-{_slug(args.model)}")
        )
        if args.dry_run:
            fixtures = []
            for case in cases:
                _, media = build_request_payload(
                    case,
                    model=args.model,
                    ttl=args.ttl or None,
                    seed=args.seed,
                    thinking_mode=args.thinking_mode,
                    extra_request_fields=extra_request_fields,
                )
                fixtures.extend({"case_id": case["id"], **item} for item in media)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "suite": suite.get("suite"),
                        "cases_sha256": suite.get("_source_sha256"),
                        "cases": len(cases),
                        "model": args.model,
                        "base_url": args.base_url,
                        "thinking_mode": args.thinking_mode,
                        "extra_request_fields": extra_request_fields,
                        "would_write": str(output),
                        "fixtures": fixtures,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if output.exists() and any(output.iterdir()):
            raise EvalError(f"Eval output directory is not empty: {output}")
        output.mkdir(parents=True, exist_ok=True)
        raw_dir = output / "raw"
        results = []
        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] {case['id']}", file=sys.stderr, flush=True)
            results.append(
                run_case(
                    case,
                    model=args.model,
                    base_url=args.base_url,
                    timeout=args.timeout,
                    ttl=args.ttl or None,
                    seed=args.seed,
                    thinking_mode=args.thinking_mode,
                    extra_request_fields=extra_request_fields,
                    raw_dir=raw_dir,
                )
            )
        report = {
            "schema_version": 2,
            "suite": suite.get("suite"),
            "suite_version": suite.get("suite_version"),
            "cases_path": str(cases_path),
            "cases_sha256": suite.get("_source_sha256"),
            "generated_at": utc_now(),
            "model": args.model,
            "base_url": args.base_url.rstrip("/"),
            "settings": {
                "concurrency": 1,
                "timeout": args.timeout,
                "ttl": args.ttl,
                "seed": args.seed,
                "thinking_mode": args.thinking_mode,
                "extra_request_fields": extra_request_fields,
                "effective_request_settings": "recorded per result",
            },
            "limitations": [
                "LM Studio 0.4.x may ignore chat_template_kwargs.enable_thinking "
                "on /v1/chat/completions for some Qwen reasoning templates. The "
                "harness records reasoning_content/reasoning_tokens and warns when "
                "thinking_mode=off is not honored; native SDK runs should be kept "
                "as a separate comparison track.",
                "A json_schema response format does not guarantee that every "
                "semantic constraint is enforced by the endpoint grammar. The "
                "harness independently validates required fields, enums, numeric "
                "ranges, lengths, uniqueness, and cross-field QA consistency.",
                "Each case is a single deterministic regression probe, not a "
                "multi-seed statistical estimate.",
            ],
            "summary": summarize_results(results),
            "results": results,
        }
        atomic_write_json(output / "report.json", report)
        print(
            json.dumps(
                {"output": str(output), "summary": report["summary"]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if report["summary"]["runtime_errors"] else 0
    except Exception as exc:
        print(f"local model eval failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
