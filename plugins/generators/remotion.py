import json
import shutil
import subprocess
from pathlib import Path

import content_pipeline_gui as pipeline
from core.models import AssetCard, Shot
from core.project import Project
from plugins.generators.base import GeneratorPlugin, GeneratorResult

REMOTION_DIR = Path(__file__).resolve().parent.parent.parent / "remotion_graphics"


class RemotionGenerator(GeneratorPlugin):
    name = "remotion"

    def generate(
        self,
        project: Project,
        card: AssetCard,
        config: dict | None = None,
    ) -> GeneratorResult:
        try:
            renders_dir = project.dir / "renders" / "remotion"
            renders_dir.mkdir(parents=True, exist_ok=True)

            if card.visual_type == "motion_text":
                job = pipeline.remotion_motion_text_job_for_card(card, renders_dir)
            else:
                shots_path = project.dir / "04_shot_list.json"
                if not shots_path.exists():
                    return GeneratorResult(success=False, error="shot_list.json not found")
                shots = [Shot(**item) for item in json.loads(shots_path.read_text("utf-8"))]
                shot = next((s for s in shots if s.index == card.shot_index), None)
                if not shot:
                    return GeneratorResult(success=False, error=f"Shot {card.shot_index} not found")
                job = pipeline.remotion_job_for_shot(shot, renders_dir)

            if not job:
                return GeneratorResult(success=False, error="No Remotion job generated for this card")

            output_path = self._render_job(project, card, job)
            return GeneratorResult(success=True, outputs={"remotion": str(output_path)})

        except Exception as exc:
            return GeneratorResult(success=False, error=str(exc))

    def _render_job(self, project: Project, card: AssetCard, job: dict) -> Path:
        jobs_dir = project.dir / "remotion_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        template = job["props"]["template"]
        job_path = jobs_dir / f"shot_{card.shot_index:03d}_{template}.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), "utf-8")

        node_modules = REMOTION_DIR / "node_modules"
        if not node_modules.exists():
            raise RuntimeError(f"Remotion deps missing. Run npm install in {REMOTION_DIR}")

        node = shutil.which("node.exe") or shutil.which("node")
        if not node:
            raise RuntimeError("node.exe not found in PATH")

        script = REMOTION_DIR / "scripts" / "render-job.mjs"
        command = [node, str(script), str(job_path)]
        process = subprocess.Popen(
            command, cwd=REMOTION_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        assert process.stdout is not None
        output_lines = []
        for line in process.stdout:
            output_lines.append(line.rstrip())
        code = process.wait()
        if code != 0:
            details = "\n".join(output_lines[-40:])
            raise RuntimeError(f"Remotion failed with code {code}\n{details}")

        output = Path(job.get("output", ""))
        if not output.exists():
            raise RuntimeError(f"Expected output not found: {output}")
        return output

    def can_handle(self, card: AssetCard, config: dict | None = None) -> bool:
        return card.route in ("remotion", "graphic")

    def validate(self) -> list[str]:
        errors = []
        if not REMOTION_DIR.exists():
            errors.append(f"Remotion directory not found: {REMOTION_DIR}")
        node_modules = REMOTION_DIR / "node_modules"
        if not node_modules.exists():
            errors.append(f"Remotion dependencies not installed. Run npm install in {REMOTION_DIR}")
        return errors
