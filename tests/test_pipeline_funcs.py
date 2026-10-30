import json

from core.board import auto_overlay_for_screen_text, determine_asset_route
from core.models import Shot, timecode_to_seconds
from core.shot_planning import normalize_shot_routes
from core.shots import split_shots_for_stock, split_stock_shots_only
from core.transcribe import whisper_detect_language


def _shot(index, start, end):
    return Shot(
        index, 1, start, end, "ai_image_then_i2v", "desc", "query",
        "krea2_turbo", "ltx2.3_i2v", "img prompt", "vid prompt", "screen",
        "motion", "priority", "fallback",
    )


def _durs(shots):
    return [timecode_to_seconds(s.end) - timecode_to_seconds(s.start) for s in shots]


def test_split_shots_for_stock_even_cuts():
    r = split_shots_for_stock([_shot(1, "00:00:00", "00:00:15")], 5, variable=False)
    assert len(r) == 3
    assert _durs(r) == [5, 5, 5]
    assert all(s.visual_type == "stock_video" for s in r)
    assert all(s.image_model == "" and s.video_model == "" for s in r)
    assert [s.index for s in r] == [1, 2, 3]
    assert all(d <= 5 for d in _durs(r))


def test_split_shots_for_stock_variable_rhythm_stays_contiguous():
    r = split_shots_for_stock(
        [_shot(1, "00:00:00", "00:00:15")], 5, variable=True,
    )
    assert all(1 <= duration <= 5 for duration in _durs(r))
    assert sum(_durs(r)) == 15
    assert r[0].start == "00:00:00" and r[-1].end == "00:00:15"


def test_split_shots_for_stock_uneven_and_short():
    assert _durs(split_shots_for_stock([_shot(1, "00:00:00", "00:00:12")], 5, variable=False)) == [4, 4, 4]
    r = split_shots_for_stock([_shot(1, "00:00:00", "00:00:07")], 5, variable=False)
    assert len(r) == 2 and all(d <= 5 for d in _durs(r)) and sum(_durs(r)) == 7
    r1 = split_shots_for_stock([_shot(1, "00:00:10", "00:00:14")], 5, variable=False)
    assert len(r1) == 1 and r1[0].visual_type == "stock_video"


def test_split_shots_reindex_and_contiguous():
    r = split_shots_for_stock(
        [_shot(1, "00:00:00", "00:00:10"), _shot(2, "00:00:10", "00:00:16")], 5,
        variable=False,
    )
    assert [s.index for s in r] == [1, 2, 3, 4]
    assert r[0].start == "00:00:00" and r[-1].end == "00:00:16"


def _shot_vt(index, start, end, vt):
    s = _shot(index, start, end)
    s.visual_type = vt
    return s


def test_split_stock_shots_only():
    # Only the stock_video shot is split; AI/Ken Burns kept whole. All re-indexed.
    shots = [
        _shot_vt(1, "00:00:00", "00:00:15", "stock_video"),      # -> 3 cuts
        _shot_vt(2, "00:00:15", "00:00:25", "ai_image_then_i2v"),  # kept
        _shot_vt(3, "00:00:25", "00:00:30", "still_kenburns"),     # kept
    ]
    r = split_stock_shots_only(shots, 5, variable=False)
    assert len(r) == 5  # 3 + 1 + 1
    assert [s.index for s in r] == [1, 2, 3, 4, 5]
    kinds = [s.visual_type for s in r]
    assert kinds == ["stock_video", "stock_video", "stock_video", "ai_image_then_i2v", "still_kenburns"]
    # the AI shot keeps its full length
    assert r[3].start == "00:00:15" and r[3].end == "00:00:25"


def test_normalize_respects_kenburns_and_stock():
    ks = _shot_vt(1, "00:00:00", "00:00:05", "still_kenburns")
    st = _shot_vt(2, "00:00:05", "00:00:10", "stock_video")
    normalize_shot_routes([ks, st], "Auto")
    assert ks.visual_type == "still_kenburns" and ks.video_model == "" and ks.image_model
    assert st.visual_type == "stock_video"  # untouched


def test_determine_asset_route_special_routes():
    stock = _shot_vt(1, "00:00:00", "00:00:05", "stock_video")
    stock.video_model = ""
    kenburns = _shot_vt(2, "00:00:05", "00:00:10", "still_kenburns")
    kenburns.video_model = ""
    ai_video = _shot_vt(3, "00:00:10", "00:00:15", "ai_image_then_i2v")
    image_only = _shot_vt(4, "00:00:15", "00:00:20", "ai_image")
    image_only.video_model = ""

    assert determine_asset_route(stock) == "stock"
    assert determine_asset_route(kenburns) == "kenburns"
    assert determine_asset_route(ai_video) == "image+video"
    assert determine_asset_route(image_only) == "image"


def test_auto_overlay_for_screen_text():
    assert auto_overlay_for_screen_text("40 миллиардов к 2030") is True
    assert auto_overlay_for_screen_text("") is False
    assert auto_overlay_for_screen_text("   ") is False
    # Only numeric callouts (stats, dates) are burned in automatically.
    assert auto_overlay_for_screen_text("Эволюция") is False
    # Long narration fragments must not be burned in as pseudo-subtitles.
    assert auto_overlay_for_screen_text("в 2030 году мы увидим совсем другой рынок") is False


def test_whisper_detect_language(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"params": {"language": "auto"}, "result": {"language": "pt"}}), encoding="utf-8")
    assert whisper_detect_language(p) == "pt"

    p2 = tmp_path / "up.json"
    p2.write_text(json.dumps({"result": {"language": "RU"}}), encoding="utf-8")
    assert whisper_detect_language(p2) == "ru"

    p3 = tmp_path / "none.json"
    p3.write_text(json.dumps({"transcription": []}), encoding="utf-8")
    assert whisper_detect_language(p3) == ""

    p4 = tmp_path / "bad.json"
    p4.write_text("{ not json", encoding="utf-8")
    assert whisper_detect_language(p4) == ""
