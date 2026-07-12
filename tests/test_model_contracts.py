import dataclasses

import content_pipeline_gui as legacy_engine

from backend.models import schemas
from core.models import AssetCard, Scene, Shot, TranscriptSegment


def _dataclass_fields(model) -> set[str]:
    return {field.name for field in dataclasses.fields(model)}


def test_legacy_engine_uses_canonical_core_models():
    assert legacy_engine.Scene is Scene
    assert legacy_engine.TranscriptSegment is TranscriptSegment
    assert legacy_engine.Shot is Shot
    assert legacy_engine.AssetCard is AssetCard


def test_api_models_cover_every_canonical_domain_field():
    assert set(schemas.Scene.model_fields) == _dataclass_fields(Scene)
    assert set(schemas.TranscriptSegment.model_fields) == _dataclass_fields(TranscriptSegment)
    assert set(schemas.Shot.model_fields) == _dataclass_fields(Shot)
    assert set(schemas.AssetCard.model_fields) == _dataclass_fields(AssetCard)
