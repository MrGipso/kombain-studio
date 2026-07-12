from core.models import AssetCard
from core.project import Project
from core.config import load_settings
from core import stock as stock_lib
from plugins.generators.base import GeneratorPlugin, GeneratorResult


class StockGenerator(GeneratorPlugin):
    """Resolve a card to a real stock clip (Pexels / Pixabay) instead of AI
    generation. Handles cards routed as ``stock_video``. Returns success=False
    when nothing suitable is found, so the caller can fall back to AI."""

    name = "stock"

    def generate(
        self,
        project: Project,
        card: AssetCard,
        config: dict | None = None,
    ) -> GeneratorResult:
        settings = project.settings or load_settings()
        try:
            result = stock_lib.fetch_stock_for_card(project.dir, card, settings)
        except Exception as exc:
            return GeneratorResult(success=False, error=str(exc))

        if not result:
            return GeneratorResult(success=False, error="No suitable stock clip found")

        card.video_output = result["video_output"]
        card.source_url = result.get("source_url", "")
        card.stock_score = float(result.get("stock_score", 0.0))
        return GeneratorResult(
            success=True,
            outputs={"video": result["video_output"]},
            metadata={"source_url": card.source_url, "stock_score": card.stock_score,
                      "source": result.get("source", "")},
        )

    def can_handle(self, card: AssetCard, config: dict | None = None) -> bool:
        return (card.visual_type or "").strip().lower() == "stock_video"

    def validate(self) -> list[str]:
        s = load_settings()
        if not stock_lib.stock_enabled(s):
            return ["Stock route disabled: set a Pexels or Pixabay API key"]
        return []
