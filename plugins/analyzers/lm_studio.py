import json

import content_pipeline_gui as pipeline
from core.models import Scene, Shot, TranscriptSegment
from core.project import Project
from plugins.analyzers.base import AnalyzerPlugin, AnalysisResult


class LmStudioAnalyzer(AnalyzerPlugin):
    name = "lm_studio"

    def analyze_scenes(
        self,
        project: Project,
        segments: list[TranscriptSegment] | None = None,
        script_text: str | None = None,
        config: dict | None = None,
    ) -> AnalysisResult:
        cfg = config or {}
        base_url = cfg.get("lm_url", "http://127.0.0.1:1234/v1")
        model = cfg.get("lm_model", "local-model")

        try:
            if segments:
                pipeline_segments = [
                    pipeline.TranscriptSegment(s.start, s.end, s.text)
                    for s in segments
                ]
                raw_scenes = pipeline.call_lm_studio_for_segments(
                    base_url, model, pipeline_segments
                )
            elif script_text:
                raw_scenes = pipeline.call_lm_studio(base_url, model, script_text)
            else:
                return AnalysisResult(success=False, error="No segments or script_text provided")

            scenes = [Scene(**s.__dict__) for s in raw_scenes]
            project.write_json("scenes", [s.__dict__ for s in scenes])
            return AnalysisResult(success=True, scenes=scenes)

        except Exception as exc:
            return AnalysisResult(success=False, error=str(exc))

    def analyze_shots(
        self,
        project: Project,
        scenes: list[Scene],
        config: dict | None = None,
    ) -> AnalysisResult:
        cfg = config or {}
        base_url = cfg.get("lm_url", "http://127.0.0.1:1234/v1")
        model = cfg.get("lm_model", "local-model")
        image_pref = cfg.get("image_model_preference", "Auto")
        style_anchor = cfg.get("style_anchor") or project.metadata.get("style_anchor", "")
        character_description = cfg.get("character_description") or project.metadata.get("character_description", "")
        video_style = cfg.get("video_style") or project.metadata.get("video_style", "knitted")

        try:
            pipeline_scenes = [pipeline.Scene(**s.__dict__) for s in scenes]
            seg_data = project.read_json("transcript_segments") or []
            pipeline_segments = [pipeline.TranscriptSegment(**s) for s in seg_data]
            raw_shots = pipeline.call_lm_studio_for_shots(
                base_url, model, pipeline_scenes, image_pref,
                segments=pipeline_segments,
                style_anchor=style_anchor,
                character_description=character_description,
                video_style=video_style,
            )
            shots = [Shot(**s.__dict__) for s in raw_shots]

            project.write_json("shot_list", [s.__dict__ for s in shots])

            cards = pipeline.build_asset_board(project.dir)
            board_data = json.loads(json.dumps([c.__dict__ for c in cards], ensure_ascii=False))
            project.write_json("asset_board", board_data)

            return AnalysisResult(success=True, shots=shots)

        except Exception as exc:
            return AnalysisResult(success=False, error=str(exc))

    def load_model(self, base_url: str, model: str, context_length: int = 32768) -> dict:
        return pipeline.load_lm_studio_model(base_url, model, context_length)

    def unload_model(self, base_url: str, instance_id: str) -> dict:
        return pipeline.unload_lm_studio_model(base_url, instance_id)

    def validate(self) -> list[str]:
        errors = []
        from core.config import load_settings
        s = load_settings()
        if not s.lm_url:
            errors.append("LM Studio URL not configured")
        if not s.lm_model:
            errors.append("LM Studio model not configured")
        return errors


