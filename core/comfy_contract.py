"""Engine <-> ComfyUI workflow contract based on node-title markers.

The engine mutates a handful of inputs in every ComfyUI API workflow: the
prompt, the input image, the clip duration, the output filename prefix and so
on.  Instead of hard-coding node ids, a workflow declares which node plays
which role by putting a marker such as ``ENGINE_PROMPT`` in the node title
(``_meta.title`` in the API JSON).  Any workflow -- including one built from
ComfyUI's cloud API nodes -- can then replace a bundled one without code
changes.

Rules (documented for users in ``docs/COMFY_WORKFLOWS.md``):

* A title may contain several markers, e.g. ``ENGINE_PROMPT ENGINE_DURATION``
  on a cloud node that takes both the prompt and the duration.  Markers are
  matched case-insensitively anywhere in the title.
* ``ENGINE_ROLE:input_name`` selects the input explicitly; otherwise the first
  literal (not connected) input from the role's candidate list is used.
* If a workflow contains at least one marker of its contract (other than
  ``ENGINE_SEED``) it is resolved strictly by markers.  A workflow without
  markers falls back to the node ids of the bundled workflow, so older copies
  of the bundled files and saved user paths keep working.
* ``ENGINE_SEED`` is honoured in both modes: when present, only marked nodes
  receive a fresh seed; otherwise every ``seed``/``noise_seed`` input does.

The module is dependency-free so both the transport adapter (validation) and
the generation layer (payload preparation) can share one implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable


PROMPT = "ENGINE_PROMPT"
NEGATIVE = "ENGINE_NEGATIVE"
INPUT_IMAGE = "ENGINE_INPUT_IMAGE"
REFERENCE_IMAGE = "ENGINE_REFERENCE_IMAGE"
OUTPUT = "ENGINE_OUTPUT"
SEED = "ENGINE_SEED"
DURATION = "ENGINE_DURATION"
WIDTH = "ENGINE_WIDTH"
HEIGHT = "ENGINE_HEIGHT"
LYRICS = "ENGINE_LYRICS"
BPM = "ENGINE_BPM"

SEED_KEYS = ("seed", "noise_seed", "sampling_mode.seed")

MODE_MARKERS = "markers"
MODE_LEGACY = "legacy_ids"

_TEXT = "text"
_NUMBER = "number"

_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(ENGINE_[A-Za-z0-9_]+)(?::([A-Za-z0-9_.]+))?",
    re.IGNORECASE,
)


class WorkflowContractError(ValueError):
    """A workflow does not satisfy the engine's marker contract."""


@dataclass(frozen=True)
class Role:
    """One input the engine writes, plus how to find it."""

    marker: str
    keys: tuple[str, ...]
    value_type: str
    description: str
    required: bool = False
    multiple: bool = False
    # Write every candidate key present on the node (seed inputs).
    all_keys: bool = False
    # (node_id, input_key) pairs of the bundled workflow, used when the
    # workflow carries no markers at all.
    legacy: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class WorkflowContract:
    name: str
    label: str
    roles: tuple[Role, ...]
    legacy_ids: bool = True

    def role(self, marker: str) -> Role | None:
        for role in self.roles:
            if role.marker == marker:
                return role
        return None

    @property
    def markers(self) -> tuple[str, ...]:
        return tuple(role.marker for role in self.roles)


@dataclass(frozen=True)
class Binding:
    node_id: str
    key: str

    def __str__(self) -> str:
        return f"{self.node_id}.{self.key}"


_PROMPT_KEYS = ("text", "value", "prompt", "prompt_text")
_NEGATIVE_KEYS = ("negative_prompt", "negative", "text", "value", "prompt")
_MUSIC_PROMPT_KEYS = ("tags", "text", "value", "prompt", "prompt_text")


def _seed_role() -> Role:
    return Role(
        SEED,
        SEED_KEYS,
        _NUMBER,
        "fresh seed per render (when absent, every seed input is randomised)",
        multiple=True,
        all_keys=True,
    )


def _prompt_role(node_id: str = "", key: str = "", *, keys=_PROMPT_KEYS) -> Role:
    return Role(
        PROMPT,
        keys,
        _TEXT,
        "positive prompt",
        required=True,
        legacy=((node_id, key),) if node_id else (),
    )


def _output_role(node_id: str = "") -> Role:
    return Role(
        OUTPUT,
        ("filename_prefix",),
        _TEXT,
        "save node; the engine sets its filename_prefix",
        required=True,
        legacy=((node_id, "filename_prefix"),) if node_id else (),
    )


def _ltx_contract(name: str, label: str) -> WorkflowContract:
    return WorkflowContract(
        name,
        label,
        (
            Role(
                INPUT_IMAGE,
                ("image",),
                _TEXT,
                "start frame (LoadImage); the engine uploads the shot image",
                required=True,
                legacy=(("269", "image"),),
            ),
            _prompt_role("267:266", "value"),
            Role(
                NEGATIVE,
                _NEGATIVE_KEYS,
                _TEXT,
                "negative prompt; the video style negative is appended",
                multiple=True,
                legacy=(("267:247", "text"),),
            ),
            Role(
                DURATION,
                ("duration", "duration_seconds", "seconds", "value"),
                _NUMBER,
                "clip length in seconds (numeric input, not a combo)",
                multiple=True,
                legacy=(("267:290", "value"),),
            ),
            Role(
                WIDTH,
                ("width", "value"),
                _NUMBER,
                "render width from the LTX resolution setting",
                multiple=True,
                legacy=(("267:257", "value"),),
            ),
            Role(
                HEIGHT,
                ("height", "value"),
                _NUMBER,
                "render height from the LTX resolution setting",
                multiple=True,
                legacy=(("267:258", "value"),),
            ),
            _output_role("273"),
            _seed_role(),
        ),
    )


CONTRACTS: dict[str, WorkflowContract] = {
    "krea": WorkflowContract(
        "krea",
        "Krea image",
        (_prompt_role("30:19", "value"), _output_role("29"), _seed_role()),
    ),
    "ideogram": WorkflowContract(
        "ideogram",
        "Ideogram image",
        (_prompt_role("98:24", "text"), _output_role("158"), _seed_role()),
    ),
    "ltx": _ltx_contract("ltx", "LTX video"),
    "ltx_draft": _ltx_contract("ltx_draft", "LTX draft video"),
    "music": WorkflowContract(
        "music",
        "Music",
        (
            Role(
                PROMPT,
                _MUSIC_PROMPT_KEYS,
                _TEXT,
                "style tags / music description",
                required=True,
                legacy=(("94", "tags"),),
            ),
            Role(
                LYRICS,
                ("lyrics", "text", "value"),
                _TEXT,
                'lyrics ("[instrumental]" for background beds)',
                legacy=(("94", "lyrics"),),
            ),
            Role(
                BPM,
                ("bpm", "value"),
                _NUMBER,
                "tempo",
                legacy=(("94", "bpm"),),
            ),
            Role(
                DURATION,
                ("duration", "seconds", "duration_seconds", "value"),
                _NUMBER,
                "track length in seconds",
                multiple=True,
                legacy=(("94", "duration"), ("98", "seconds")),
            ),
            _output_role("107"),
            _seed_role(),
        ),
    ),
    "reference": WorkflowContract(
        "reference",
        "Reference",
        (
            Role(
                REFERENCE_IMAGE,
                ("image",),
                _TEXT,
                "canonical reference image (LoadImage)",
                required=True,
            ),
            _prompt_role(),
            _output_role(),
            _seed_role(),
        ),
        legacy_ids=False,
    ),
}

KNOWN_MARKERS = frozenset(
    role.marker for contract in CONTRACTS.values() for role in contract.roles
)


def contract_for(name: str) -> WorkflowContract:
    try:
        return CONTRACTS[name]
    except KeyError:
        raise KeyError(f"Unknown workflow contract: {name}") from None


def node_markers(node: Any) -> list[tuple[str, str]]:
    """Return ``(MARKER, explicit_key)`` pairs found in one node title."""

    if not isinstance(node, dict):
        return []
    meta = node.get("_meta")
    title = meta.get("title", "") if isinstance(meta, dict) else ""
    if not isinstance(title, str):
        return []
    return [
        (match.group(1).upper(), match.group(2) or "")
        for match in _MARKER_RE.finditer(title)
    ]


def _is_link(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], int)
    )


def _type_ok(value: Any, value_type: str) -> bool:
    if value_type == _TEXT:
        return isinstance(value, str)
    if value_type == _NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _describe(value: Any) -> str:
    if isinstance(value, str):
        return f"text {value[:40]!r}"
    return type(value).__name__


class BoundWorkflow:
    """A workflow whose engine roles were resolved to concrete inputs."""

    def __init__(
        self,
        workflow: dict,
        contract: WorkflowContract,
        mode: str,
        bindings: dict[str, tuple[Binding, ...]],
        warnings: list[str],
    ) -> None:
        self.workflow = workflow
        self.contract = contract
        self.mode = mode
        self.bindings = bindings
        self.warnings = warnings

    def targets(self, marker: str) -> tuple[Binding, ...]:
        return self.bindings.get(marker, ())

    def set(self, marker: str, value: Any) -> bool:
        """Write ``value`` to every input bound to ``marker``."""

        return self.transform(marker, lambda _old: value)

    def transform(self, marker: str, update: Callable[[Any], Any]) -> bool:
        targets = self.targets(marker)
        for target in targets:
            inputs = self.workflow[target.node_id]["inputs"]
            inputs[target.key] = update(inputs.get(target.key))
        return bool(targets)

    def report(self) -> dict[str, Any]:
        return {
            "ok": True,
            "contract": self.contract.name,
            "mode": self.mode,
            "roles": {
                marker: [str(target) for target in targets]
                for marker, targets in self.bindings.items()
                if targets
            },
            "warnings": list(self.warnings),
        }


def _node_inputs(workflow: dict, node_id: str) -> dict | None:
    node = workflow.get(node_id)
    if not isinstance(node, dict):
        return None
    inputs = node.get("inputs")
    return inputs if isinstance(inputs, dict) else None


def _pick_keys(
    role: Role,
    node_id: str,
    inputs: dict | None,
    explicit_key: str,
    errors: list[str],
) -> list[str]:
    where = f"{role.marker} on node {node_id!r}"
    if inputs is None:
        errors.append(f"{where} has no inputs object")
        return []
    if explicit_key:
        if explicit_key not in inputs:
            errors.append(
                f"{where} names input {explicit_key!r}, but the node has only: "
                + (", ".join(sorted(inputs)) or "no inputs")
            )
            return []
        candidates = [explicit_key]
    else:
        candidates = [key for key in role.keys if key in inputs]
    literal = [key for key in candidates if not _is_link(inputs[key])]
    if not literal:
        linked = [key for key in candidates if _is_link(inputs[key])]
        if linked:
            source = inputs[linked[0]][0]
            errors.append(
                f"{where}: input {linked[0]!r} is connected to node {source!r}; "
                "put the marker on that source node instead"
            )
        else:
            errors.append(
                f"{where} needs one of the inputs {'/'.join(role.keys)} "
                f"(or {role.marker}:<input_name> in the title)"
            )
        return []
    chosen = literal if role.all_keys else literal[:1]
    for key in chosen:
        if not _type_ok(inputs[key], role.value_type):
            expected = "a number" if role.value_type == _NUMBER else "text"
            errors.append(
                f"{where}: input {key!r} must be {expected}, got "
                f"{_describe(inputs[key])}"
                + (
                    " (combo/choice inputs cannot be driven by the engine; "
                    "remove the marker)"
                    if role.value_type == _NUMBER
                    else ""
                )
            )
            return []
    return chosen


def _check_conflicts(
    bindings: dict[str, tuple[Binding, ...]],
    errors: list[str],
) -> None:
    owners: dict[Binding, str] = {}
    for marker, targets in bindings.items():
        for target in targets:
            previous = owners.setdefault(target, marker)
            if previous != marker:
                errors.append(
                    f"{previous} and {marker} both target input {target}; "
                    f"use {marker}:<input_name> to pick another input"
                )


def _marker_scan(
    workflow: dict,
    contract: WorkflowContract,
) -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
    found: dict[str, list[tuple[str, str]]] = {marker: [] for marker in contract.markers}
    warnings: list[str] = []
    for node_id, node in workflow.items():
        for marker, key in node_markers(node):
            if marker in found:
                if (str(node_id), key) not in found[marker]:
                    found[marker].append((str(node_id), key))
            elif marker in KNOWN_MARKERS:
                warnings.append(
                    f"{marker} on node {node_id!r} is not used by the "
                    f"{contract.label} workflow"
                )
            else:
                warnings.append(
                    f"unknown marker {marker} on node {node_id!r} "
                    f"(known: {', '.join(contract.markers)})"
                )
    return found, warnings


def _resolve_markers(
    workflow: dict,
    roles: tuple[Role, ...],
    found: dict[str, list[tuple[str, str]]],
    errors: list[str],
) -> dict[str, tuple[Binding, ...]]:
    bindings: dict[str, tuple[Binding, ...]] = {}
    for role in roles:
        nodes = found.get(role.marker, [])
        if not nodes:
            if role.required:
                errors.append(f"no node is marked {role.marker} ({role.description})")
            continue
        if len(nodes) > 1 and not role.multiple:
            errors.append(
                f"{role.marker} must mark exactly one node, found on: "
                + ", ".join(repr(node_id) for node_id, _key in nodes)
            )
            continue
        targets: list[Binding] = []
        for node_id, explicit_key in nodes:
            keys = _pick_keys(
                role,
                node_id,
                _node_inputs(workflow, node_id),
                explicit_key,
                errors,
            )
            targets.extend(Binding(node_id, key) for key in keys)
        bindings[role.marker] = tuple(dict.fromkeys(targets))
    return bindings


def _resolve_legacy(
    workflow: dict,
    contract: WorkflowContract,
    errors: list[str],
) -> dict[str, tuple[Binding, ...]]:
    bindings: dict[str, tuple[Binding, ...]] = {}
    for role in contract.roles:
        if role.marker == SEED:
            continue
        targets: list[Binding] = []
        for node_id, key in role.legacy:
            inputs = _node_inputs(workflow, node_id)
            problem = ""
            if inputs is None:
                problem = f"node {node_id!r} is missing"
            elif key not in inputs:
                problem = f"node {node_id!r} has no input {key!r}"
            elif _is_link(inputs[key]):
                problem = f"node {node_id!r} input {key!r} is connected to another node"
            elif not _type_ok(inputs[key], role.value_type):
                problem = (
                    f"node {node_id!r} input {key!r} has unexpected "
                    f"{_describe(inputs[key])}"
                )
            if problem:
                if role.required:
                    errors.append(f"{role.marker}: {problem}")
                continue
            targets.append(Binding(node_id, key))
        bindings[role.marker] = tuple(targets)
    return bindings


def bind_workflow(workflow: dict, name: str) -> BoundWorkflow:
    """Resolve every engine role of ``workflow`` or raise a clear error."""

    contract = contract_for(name)
    if not isinstance(workflow, dict):
        raise WorkflowContractError(f"{contract.label} workflow root must be an object")
    found, warnings = _marker_scan(workflow, contract)
    errors: list[str] = []
    uses_markers = any(
        nodes for marker, nodes in found.items() if marker != SEED
    )
    if uses_markers or not contract.legacy_ids:
        mode = MODE_MARKERS
        bindings = _resolve_markers(workflow, contract.roles, found, errors)
    else:
        mode = MODE_LEGACY
        bindings = _resolve_legacy(workflow, contract, errors)
        # ENGINE_SEED does not switch the mode, so it can pin seeds even in an
        # otherwise unmarked copy of a bundled workflow.
        seed_roles = tuple(role for role in contract.roles if role.marker == SEED)
        bindings.update(_resolve_markers(workflow, seed_roles, found, errors))
    _check_conflicts(bindings, errors)
    if errors:
        raise WorkflowContractError(_error_message(contract, mode, errors, warnings))
    return BoundWorkflow(workflow, contract, mode, bindings, warnings)


def _error_message(
    contract: WorkflowContract,
    mode: str,
    errors: list[str],
    warnings: list[str],
) -> str:
    required = ", ".join(role.marker for role in contract.roles if role.required)
    if mode == MODE_LEGACY:
        head = (
            f"{contract.label} workflow has no ENGINE_* markers and does not "
            f"match the bundled node layout ({'; '.join(errors)}). "
            f"Mark its nodes via _meta.title: {required} are required"
        )
    else:
        head = f"{contract.label} workflow breaks the marker contract: " + "; ".join(errors)
    if warnings:
        head += ". Also: " + "; ".join(warnings)
    return head


def contract_report(workflow: dict, name: str) -> dict[str, Any]:
    """Non-raising variant of :func:`bind_workflow` for diagnostics."""

    try:
        return bind_workflow(workflow, name).report()
    except WorkflowContractError as exc:
        return {"ok": False, "contract": name, "error": str(exc)}


__all__ = [
    "BPM",
    "Binding",
    "BoundWorkflow",
    "CONTRACTS",
    "DURATION",
    "HEIGHT",
    "INPUT_IMAGE",
    "KNOWN_MARKERS",
    "LYRICS",
    "MODE_LEGACY",
    "MODE_MARKERS",
    "NEGATIVE",
    "OUTPUT",
    "PROMPT",
    "REFERENCE_IMAGE",
    "Role",
    "SEED",
    "SEED_KEYS",
    "WIDTH",
    "WorkflowContract",
    "WorkflowContractError",
    "bind_workflow",
    "contract_for",
    "contract_report",
    "node_markers",
]
