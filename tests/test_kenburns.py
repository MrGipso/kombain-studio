import types
from pathlib import Path

from core import kenburns


def _capture(monkeypatch):
    box = {}

    def fake_run(cmd, capture_output=True, text=True):
        box["cmd"] = cmd
        Path(cmd[-1]).write_text("x")
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(kenburns.subprocess, "run", fake_run)
    return box


def test_kenburns_zoom_in(monkeypatch, tmp_path):
    box = _capture(monkeypatch)
    kenburns.build_kenburns_clip(tmp_path / "i.png", tmp_path / "o.mp4", "ffmpeg", 5)
    cmd = box["cmd"]
    joined = " ".join(cmd)
    assert "-loop" in cmd and "-t" in cmd
    assert "5.00" in cmd                       # slot duration
    assert "zoompan" in joined
    assert "1920x1080" in joined               # output size
    assert "1.0+(1.1500-1.0)*" in joined       # eased zoom-in (grows from 1.0)
    assert "3-2*(on/" in joined                # smoothstep easing
    assert "-an" in cmd and "libx264" in cmd


def test_kenburns_zoom_out(monkeypatch, tmp_path):
    box = _capture(monkeypatch)
    kenburns.build_kenburns_clip(tmp_path / "i.png", tmp_path / "o.mp4", "ffmpeg", 3, direction="out")
    joined = " ".join(box["cmd"])
    assert "1.1500-(1.1500-1.0)*" in joined    # eased zoom-out (shrinks toward 1.0)
    assert "3.00" in box["cmd"]


def test_kenburns_raises_on_ffmpeg_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(kenburns.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stderr="boom"))
    try:
        kenburns.build_kenburns_clip(tmp_path / "i.png", tmp_path / "o.mp4", "ffmpeg", 5)
        assert False, "should have raised"
    except RuntimeError as e:
        assert "Ken Burns" in str(e)
