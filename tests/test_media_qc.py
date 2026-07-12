from core.media_qc import evaluate_probe


def _probe(*, width=1920, height=1080, fps="25/1", audio=True):
    streams = [{
        "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
        "width": width, "height": height, "avg_frame_rate": fps,
    }]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000"})
    return {"streams": streams, "format": {"duration": "61.25"}}


def test_professional_probe_passes_expected_master():
    report = evaluate_probe(_probe())
    assert report["ok"] is True
    assert report["counts"] == {"error": 0, "warning": 0}
    assert report["duration"] == 61.25


def test_probe_blocks_wrong_resolution_fps_and_missing_audio():
    report = evaluate_probe(_probe(width=1280, height=720, fps="30/1", audio=False))
    assert report["ok"] is False
    assert {issue["code"] for issue in report["issues"]} == {
        "bad_resolution", "bad_fps", "missing_audio",
    }


def test_probe_reports_compatibility_warnings_without_blocking():
    probe = _probe()
    probe["streams"][0]["codec_name"] = "hevc"
    probe["streams"][0]["pix_fmt"] = "yuv444p"
    probe["streams"][1]["sample_rate"] = "44100"
    report = evaluate_probe(probe)
    assert report["ok"] is True
    assert report["counts"] == {"error": 0, "warning": 3}
