from pathlib import Path

import content_pipeline_gui as pipeline
from core.models import AssetCard
from core.project import Project
from plugins.generators.base import GeneratorPlugin, GeneratorResult


class ComfyGenerator(GeneratorPlugin):
    name = "comfy"

    def generate(
        self,
        project: Project,
        card: AssetCard,
        config: dict | None = None,
    ) -> GeneratorResult:
        cfg = config or {}
        comfy_url = cfg.get("comfy_url", "http://127.0.0.1:8188")

        try:
            outputs = {}
            if card.image_model:
                image_path = pipeline.generate_comfy_image(comfy_url, project.dir, card)
                outputs["image"] = str(image_path)

            if card.video_model:
                video_path = pipeline.generate_comfy_ltx_video(comfy_url, project.dir, card)
                outputs["video"] = str(video_path)

            if not outputs:
                return GeneratorResult(
                    success=False,
                    error="No image or video model configured for this card",
                )

            return GeneratorResult(success=True, outputs=outputs)

        except Exception as exc:
            return GeneratorResult(success=False, error=str(exc))

    def can_handle(self, card: AssetCard, config: dict | None = None) -> bool:
        return bool(card.image_model or card.video_model)

    def validate(self) -> list[str]:
        errors = []
        from core.config import load_settings
        s = load_settings()
        if not s.comfy_url:
            errors.append("ComfyUI URL not configured")
        return errors
