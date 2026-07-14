import types
from pathlib import Path

from core import stock


def _card(**kw):
    base = dict(asset_query="", description="", screen_text="",
                shot_index=1, start="00:00:00", end="00:00:05")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _settings(**kw):
    base = dict(pexels_api_key="k", pixabay_api_key="k2", lm_vision_model="vl",
                lm_model="chat", stock_score_threshold=0.6, stock_floor=0.4,
                stock_retry_queries=0, stock_max_seconds=0, stock_candidates=20,
                ffmpeg="ffmpeg", lm_url="http://x/v1",
                lm_auto_unload=True, lm_unload_delay="0")
    base.update(kw)
    return types.SimpleNamespace(**base)


# ---- pure helpers ----
def test_mostly_ascii():
    assert stock._mostly_ascii("server room rows of racks") is True
    assert stock._mostly_ascii("серверная стойки") is False
    assert stock._mostly_ascii("") is False
    assert stock._mostly_ascii("40 billion by 2030") is True


def test_queries_prefer_english_asset_query():
    assert stock._queries_for_card(_card(asset_query="server room", description="серверная")) == ["server room"]


def test_queries_reject_cyrillic():
    assert stock._queries_for_card(_card(asset_query="серверная", description="русское")) == ["documentary b-roll"]


def test_queries_english_description_fallback():
    assert stock._queries_for_card(_card(asset_query="", description="busy city street")) == ["busy city street"]


def test_tc_to_seconds():
    assert stock._tc_to_seconds("00:00:05") == 5


def test_registry_uses_atomic_runtime_path(monkeypatch, tmp_path):
    runtime_path = tmp_path / "runtime" / "stock_registry.json"
    monkeypatch.setattr(stock, "REGISTRY_PATH", runtime_path)
    monkeypatch.setattr(stock, "LEGACY_REGISTRY_PATH", tmp_path / "legacy.json")
    payload = {"clips": [{"source": "pexels", "id": "1"}]}

    stock.save_registry(payload)

    assert stock.load_registry() == payload
    assert runtime_path.is_file()
    assert not list(runtime_path.parent.glob("*.tmp"))


def test_registry_reads_legacy_history_without_moving_it(monkeypatch, tmp_path):
    runtime_path = tmp_path / "runtime" / "stock_registry.json"
    legacy_path = tmp_path / "stock_registry.json"
    legacy_path.write_text('{"clips": [{"id": "legacy"}]}', encoding="utf-8")
    monkeypatch.setattr(stock, "REGISTRY_PATH", runtime_path)
    monkeypatch.setattr(stock, "LEGACY_REGISTRY_PATH", legacy_path)

    assert stock.load_registry() == {"clips": [{"id": "legacy"}]}
    assert not runtime_path.exists()
    assert legacy_path.exists()
    assert stock._tc_to_seconds("00:01:30") == 90
    assert stock._tc_to_seconds("1:00:00") == 3600
    assert stock._tc_to_seconds("garbage") == 0


def test_hamming():
    assert stock.hamming(0b1010, 0b1000) == 1


def test_is_duplicate():
    reg = {"clips": [{"source": "pexels", "id": "7", "phash": None}]}
    assert stock.is_duplicate({"source": "pexels", "id": "7"}, None, reg) is True
    assert stock.is_duplicate({"source": "pexels", "id": "8"}, None, reg) is False
    reg2 = {"clips": [{"source": "x", "id": "1", "phash": 0}]}
    assert stock.is_duplicate({"source": "y", "id": "2"}, 0b111, reg2) is True   # 3 bits
    assert stock.is_duplicate({"source": "y", "id": "2"}, 0xFF, reg2) is False   # 8 bits


def test_stock_enabled():
    assert stock.stock_enabled(types.SimpleNamespace(pexels_api_key="", pixabay_api_key="")) is False
    assert stock.stock_enabled(types.SimpleNamespace(pexels_api_key="k", pixabay_api_key="")) is True


def test_average_hash_graceful():
    h = stock.average_hash(b"not an image")
    assert h is None or isinstance(h, int)


def test_vision_ttl():
    ns = types.SimpleNamespace
    assert stock._vision_ttl(ns(lm_auto_unload=False, lm_unload_delay="0")) is None
    assert stock._vision_ttl(ns(lm_auto_unload=True, lm_unload_delay="0")) == 20   # immediate -> 20s so a batch doesn't thrash
    assert stock._vision_ttl(ns(lm_auto_unload=True, lm_unload_delay="30")) == 30
    assert stock._vision_ttl(ns(lm_auto_unload=True, lm_unload_delay="bad")) == 60


def test_score_candidate_sets_ttl(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"score\\":0.7,\\"watermark\\":false}"}}]}'

    def fake_urlopen(req, timeout=40):
        captured["data"] = req.data
        return FakeResp()

    monkeypatch.setattr(stock.urllib.request, "urlopen", fake_urlopen)
    import json as _json
    score = stock.score_candidate(b"img", "a fridge", "http://x/v1", "vl", ttl=45)
    body = _json.loads(captured["data"])
    assert body.get("ttl") == 45           # JIT auto-unload set on the vision call
    assert body["response_format"]["type"] == "json_schema"
    assert score == 0.7
    # No ttl -> the field is omitted (model stays as configured)
    stock.score_candidate(b"img", "x", "http://x/v1", "vl", ttl=None)
    assert "ttl" not in _json.loads(captured["data"])


def test_score_candidate_rejects_accidental_zero_to_ten_scale(monkeypatch):
    calls = 0

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"score\\":5,\\"watermark\\":false}"}}]}'

    def fake_urlopen(request, timeout=40):
        nonlocal calls
        calls += 1
        return FakeResp()

    monkeypatch.setattr(stock.urllib.request, "urlopen", fake_urlopen)
    assert stock.score_candidate(b"img", "a fridge", "http://x/v1", "vl") == 0.0
    assert calls == 2  # one bounded schema-repair attempt, then fail closed


# ---- trim: cut vs loop ----
def test_build_stock_clip_cut_and_loop(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["cmd"] = cmd
        Path(cmd[-1]).write_text("x")
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(stock.subprocess, "run", fake_run)

    monkeypatch.setattr(stock, "_probe_duration", lambda p, f: 30.0)  # long source
    stock.build_stock_clip(tmp_path / "s.mp4", tmp_path / "o.mp4", "ffmpeg", 15)
    assert "-stream_loop" not in captured["cmd"] and "15.00" in captured["cmd"]

    monkeypatch.setattr(stock, "_probe_duration", lambda p, f: 8.0)   # short source -> loop
    stock.build_stock_clip(tmp_path / "s.mp4", tmp_path / "o2.mp4", "ffmpeg", 15)
    assert "-stream_loop" in captured["cmd"] and "15.00" in captured["cmd"]

    monkeypatch.setattr(stock, "_probe_duration", lambda p, f: 30.0)
    stock.build_stock_clip(tmp_path / "s.mp4", tmp_path / "o3.mp4", "ffmpeg", 15, ceiling=10)
    assert "10.00" in captured["cmd"]  # ceiling caps the length


def test_uniquify_filter():
    f = stock._uniquify_filter(mirror=False)
    assert "crop=" in f and "eq=" in f and "hue=" in f and "hflip" not in f
    assert "hflip" in stock._uniquify_filter(mirror=True)


def test_build_stock_clip_uniquify_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        captured["cmd"] = cmd
        Path(cmd[-1]).write_text("x")
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(stock.subprocess, "run", fake_run)
    monkeypatch.setattr(stock, "_probe_duration", lambda p, f: 30.0)

    stock.build_stock_clip(tmp_path / "s.mp4", tmp_path / "on.mp4", "ffmpeg", 5, uniquify=True)
    assert "-vf" in captured["cmd"] and "crop=" in " ".join(captured["cmd"])

    stock.build_stock_clip(tmp_path / "s.mp4", tmp_path / "off.mp4", "ffmpeg", 5, uniquify=False)
    assert "-vf" not in captured["cmd"]


# ---- orchestration: gating / dedup / blind ----
def _mock_boundary(monkeypatch, candidates, scores=None):
    monkeypatch.setattr(stock, "_download", lambda url, timeout=60: b"BYTES")
    monkeypatch.setattr(stock, "build_stock_clip", lambda src, dst, *a, **k: Path(dst))
    monkeypatch.setattr(stock, "load_registry", lambda: {"clips": []})
    monkeypatch.setattr(stock, "save_registry", lambda r: None)
    monkeypatch.setattr(stock, "average_hash", lambda b: None)
    monkeypatch.setattr(stock, "gather_candidates",
                        lambda q, s, per_source=12, log=None: [dict(c) for c in candidates])
    if scores is not None:
        it = iter(scores)
        monkeypatch.setattr(stock, "score_candidate", lambda *a, **k: next(it))


def test_fetch_vision_picks_best(monkeypatch, tmp_path):
    cands = [
        {"source": "pexels", "id": "1", "preview_url": "p", "video_url": "v", "page_url": "pg1"},
        {"source": "pexels", "id": "2", "preview_url": "p", "video_url": "v", "page_url": "pg2"},
    ]
    _mock_boundary(monkeypatch, cands, scores=[0.4, 0.82])
    r = stock.fetch_stock_for_card(tmp_path, _card(asset_query="server room"), _settings())
    assert r is not None
    assert r["stock_score"] == 0.82 and r["source_url"] == "pg2"


def test_fetch_below_floor_returns_none(monkeypatch, tmp_path):
    cands = [{"source": "pexels", "id": "1", "preview_url": "p", "video_url": "v", "page_url": "pg"}]
    _mock_boundary(monkeypatch, cands, scores=[0.3])  # 0.3 < floor 0.4, no retries
    assert stock.fetch_stock_for_card(tmp_path, _card(asset_query="x y"), _settings()) is None


def test_fetch_accepts_between_floor_and_accept(monkeypatch, tmp_path):
    # 0.45 is below "accept" (0.6) but above "floor" (0.4) -> taken (was skipped before).
    cands = [{"source": "pexels", "id": "5", "preview_url": "p", "video_url": "v", "page_url": "pg5"}]
    _mock_boundary(monkeypatch, cands, scores=[0.45])
    r = stock.fetch_stock_for_card(tmp_path, _card(asset_query="server room"), _settings())
    assert r is not None and r["stock_score"] == 0.45


def test_fetch_retry_rephrases_and_keeps_best(monkeypatch, tmp_path):
    # Round 0 (query "server room") -> 0.35 (below floor). Rephrase -> "data center"
    # -> 0.8 (accepted). Verifies the retry path and that the better clip wins.
    pools = {
        "server room": [{"source": "pexels", "id": "1", "preview_url": "p", "video_url": "v1", "page_url": "pg1"}],
        "data center": [{"source": "pixabay", "id": "2", "preview_url": "p", "video_url": "v2", "page_url": "pg2"}],
    }
    scores = {"1": 0.35, "2": 0.8}
    monkeypatch.setattr(stock, "_download", lambda url, timeout=60: b"BYTES")
    monkeypatch.setattr(stock, "build_stock_clip", lambda src, dst, *a, **k: Path(dst))
    monkeypatch.setattr(stock, "load_registry", lambda: {"clips": []})
    monkeypatch.setattr(stock, "save_registry", lambda r: None)
    monkeypatch.setattr(stock, "average_hash", lambda b: None)
    monkeypatch.setattr(stock, "gather_candidates",
                        lambda q, s, per_source=12, log=None: [dict(c) for c in pools.get(q, [])])
    monkeypatch.setattr(stock, "_rephrase_query", lambda *a, **k: "data center")
    # score by candidate id (embed id via a closure over the current candidate)
    calls = {"i": 0}
    seq = ["1", "2"]  # candidates are scored in gather order across rounds

    def fake_score(preview, description, lm_url, vision_model, timeout=40, ttl=None):
        cid = seq[calls["i"]] if calls["i"] < len(seq) else "0"
        calls["i"] += 1
        return scores.get(cid, 0.0)

    monkeypatch.setattr(stock, "score_candidate", fake_score)
    r = stock.fetch_stock_for_card(tmp_path, _card(asset_query="server room"),
                                   _settings(stock_retry_queries=1))
    assert r is not None and r["stock_score"] == 0.8 and r["source"] == "pixabay"


def test_score_pool(monkeypatch):
    monkeypatch.setattr(stock, "_download", lambda url, timeout=60: b"BYTES")
    monkeypatch.setattr(stock, "average_hash", lambda b: None)
    seq = iter([0.4, 0.7, 0.2])
    monkeypatch.setattr(stock, "score_candidate", lambda *a, **k: next(seq))
    pool = [{"source": "s", "id": str(i), "preview_url": "p", "video_url": "v"} for i in range(3)]
    best, score, phash, seen = stock._score_pool(pool, {"clips": []}, False, "vl", "d", "u", None)
    assert score == 0.7 and best["id"] == "1" and seen == 3
    # blind: first non-duplicate at 0.5
    b2, s2, p2, seen2 = stock._score_pool(pool, {"clips": []}, True, "", "d", "u", None)
    assert s2 == 0.5 and b2["id"] == "0"


def test_fetch_blind_takes_first(monkeypatch, tmp_path):
    cands = [{"source": "pexels", "id": "1", "preview_url": "", "video_url": "v", "page_url": "pg"}]
    _mock_boundary(monkeypatch, cands)  # no scores; blind
    r = stock.fetch_stock_for_card(tmp_path, _card(asset_query="server room"),
                                   _settings(lm_vision_model=""))
    assert r is not None and r["source"] == "pexels"


def test_fetch_disabled_returns_none(tmp_path):
    off = types.SimpleNamespace(pexels_api_key="", pixabay_api_key="")
    assert stock.fetch_stock_for_card(tmp_path, _card(), off) is None
