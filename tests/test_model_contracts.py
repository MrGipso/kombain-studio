import dataclasses

from backend.models import schemas
from core.models import AssetCard, Scene, Shot, TranscriptSegment


def _dataclass_fields(model) -> set[str]:
    return {field.name for field in dataclasses.fields(model)}


def test_api_models_cover_every_canonical_domain_field():
    assert set(schemas.Scene.model_fields) == _dataclass_fields(Scene)
    assert set(schemas.TranscriptSegment.model_fields) == _dataclass_fields(TranscriptSegment)
    assert set(schemas.Shot.model_fields) == _dataclass_fields(Shot)
    assert set(schemas.AssetCard.model_fields) == _dataclass_fields(AssetCard)
