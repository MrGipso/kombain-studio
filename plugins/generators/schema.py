from pathlib import Path

import schema_generator as sg
from core.models import AssetCard
from core.project import Project
from plugins.generators.base import GeneratorPlugin, GeneratorResult


class SchemaGeneratorPlugin(GeneratorPlugin):
    name = "schema"

    def generate(
        self,
        project: Project,
        card: AssetCard,
        config: dict | None = None,
    ) -> GeneratorResult:
        try:
            spec_path = None
            if config and "spec_path" in config:
                spec_path = Path(config["spec_path"])

            if spec_path and spec_path.exists():
                import json
                spec = json.loads(spec_path.read_text("utf-8"))
                svg = sg.render_schema(spec)
                output_dir = project.dir / "assets" / "schemas"
                output_dir.mkdir(parents=True, exist_ok=True)
                out_path = output_dir / f"schema_{card.shot_index:03d}.svg"
                out_path.write_text(svg, "utf-8")
                return GeneratorResult(
                    success=True,
                    outputs={"svg": str(out_path)},
                )

            if config and "sample_specs" in config:
                samples = sg.sample_specs()
                if config["sample_specs"] in samples:
                    spec = samples[config["sample_specs"]]
                    svg = sg.render_schema(spec)
                    output_dir = project.dir / "assets" / "schemas"
                    output_dir.mkdir(parents=True, exist_ok=True)
                    out_path = output_dir / f"{config['sample_specs']}.svg"
                    out_path.write_text(svg, "utf-8")
                    return GeneratorResult(
                        success=True,
                        outputs={"svg": str(out_path)},
                    )

            return GeneratorResult(
                success=False,
                error="No schema spec provided",
            )

        except Exception as exc:
            return GeneratorResult(success=False, error=str(exc))

    def can_handle(self, card: AssetCard, config: dict | None = None) -> bool:
        return card.visual_type in ("programmatic_schema", "schema", "graphic")

    def list_specs(self) -> dict:
        return sg.sample_specs()

    def render_spec(self, spec: dict) -> str:
        return sg.render_schema(spec)
