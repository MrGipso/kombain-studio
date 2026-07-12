from pathlib import Path

from core import config


def test_preset_workflow_defaults_are_portable_and_exist():
    presets = config.list_presets()
    assert presets

    for preset in presets:
        for workflow_name, workflow in preset.comfy_workflows.items():
            default = str(workflow.get("default", "")).strip()
            assert default, f"{preset.path.name}:{workflow_name} has no default"

            path = Path(default)
            has_windows_drive = len(default) >= 2 and default[1] == ":"
            assert not path.is_absolute() and not has_windows_drive, (
                f"{preset.path.name}:{workflow_name} is machine-specific: {default}"
            )
            assert (config.ROOT / path).is_file(), (
                f"{preset.path.name}:{workflow_name} does not exist: {default}"
            )
