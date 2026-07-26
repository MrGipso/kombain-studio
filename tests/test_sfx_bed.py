"""Offline tests for the SFX bed assembly and the export audio mix graph.

Covers the film-style continuity rules: span merging, crossfade hand-over
between adjacent spans, the room-tone layer, per-clip loudness normalization,
and the gentle voice-sidechain on the SFX bed in the final mix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core import sfx
from core.export import _audio_mix_filtergraph


@dataclass
class _Card:
    sfx_output: str = ""


@dataclass
class _Settings:
    sfx_enabled: bool = True
    woosh_url: str = "http://127.0.0.1:8500"
    ffmpeg: str = "ffmpeg"
    sfx_room_tone: bool = True


@dataclass
class _FakeRun:
    """subprocess.run stand-in that records the command and creates outputs."""

    create: bool = True
    returncode: int = 0
    calls: list = field(default_factory=list)

    def __call__(self, cmd, capture_output=True, text=True):
        self.calls.append(cmd)
        if self.create:
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x" * 16)

        class Proc:
            pass

        proc = Proc()
        proc.returncode = self.returncode
        proc.stderr = "" if self.returncode == 0 else "boom"
        return proc


def _clip(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"flac")
    return p


def test_bed_merges_spans_and_crossfades_adjacent(tmp_path, monkeypatch):
    hum = _clip(tmp_path, "hum.flac")
    rain = _clip(tmp_path, "rain.flac")
    cards = [
        (_Card(str(hum)), (0, 4)),
        (_Card(str(hum)), (4, 10)),   # merges with the previous span
        (_Card(str(rain)), (10, 15)),
    ]
    fake = _FakeRun()
    monkeypatch.setattr(sfx.subprocess, "run", fake)

    bed = sfx.build_sfx_bed(cards, tmp_path / "exports", "ffmpeg", 15)

    assert bed is not None
    cmd = fake.calls[0]
    # Two clip inputs (merged hum + rain) and the silent lavfi base.
    assert cmd.count("-i") == 3
    graph = cmd[cmd.index("-filter_complex") + 1]
    # Span A [0..10) extends 1s into span B for the crossfade hand-over.
    assert "atrim=0:11" in graph
    # Span B keeps its own timing and fades in over the overlap.
    assert "adelay=10000:all=1" in graph
    # Last span reaches total_end and is not extended past it.
    assert "atrim=0:5" in graph
    assert "amix=inputs=3:normalize=0" in graph


def test_bed_does_not_crossfade_across_gaps(tmp_path, monkeypatch):
    hum = _clip(tmp_path, "hum.flac")
    rain = _clip(tmp_path, "rain.flac")
    cards = [
        (_Card(str(hum)), (0, 4)),
        (_Card(""), (4, 9)),          # silent shot: spans are not adjacent
        (_Card(str(rain)), (9, 15)),
    ]
    fake = _FakeRun()
    monkeypatch.setattr(sfx.subprocess, "run", fake)

    assert sfx.build_sfx_bed(cards, tmp_path / "exports", "ffmpeg", 15) is not None
    graph = fake.calls[0][fake.calls[0].index("-filter_complex") + 1]
    assert "atrim=0:4" in graph      # no extension into the gap
    assert "atrim=0:6" in graph


def test_bed_includes_room_tone_layer(tmp_path, monkeypatch):
    hum = _clip(tmp_path, "hum.flac")
    tone = _clip(tmp_path, "room_tone.flac")
    cards = [(_Card(str(hum)), (0, 10))]
    fake = _FakeRun()
    monkeypatch.setattr(sfx.subprocess, "run", fake)

    bed = sfx.build_sfx_bed(
        cards, tmp_path / "exports", "ffmpeg", 20,
        volume_db=-24.0, room_tone=tone,
    )

    assert bed is not None
    cmd = fake.calls[0]
    assert str(tone) in cmd
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "volume=-32.0dB[rt]" in graph          # bed volume - 8 dB
    assert "[rt]" in graph.split("amix")[0]
    assert "amix=inputs=3:normalize=0" in graph   # span + room tone + silence


def test_normalize_clip_success_replaces_file(tmp_path, monkeypatch):
    clip = _clip(tmp_path, "sfx_a.flac")
    fake = _FakeRun(create=True, returncode=0)
    monkeypatch.setattr(sfx.subprocess, "run", fake)

    assert sfx.normalize_clip(clip, "ffmpeg") is True
    cmd = fake.calls[0]
    assert f"loudnorm=I={sfx.SFX_TARGET_LUFS}:TP=-2:LRA=11" in cmd
    assert clip.exists()
    assert not clip.with_suffix(".norm.flac").exists()


def test_normalize_clip_failure_keeps_original(tmp_path, monkeypatch):
    clip = _clip(tmp_path, "sfx_a.flac")
    original = clip.read_bytes()
    fake = _FakeRun(create=False, returncode=1)
    monkeypatch.setattr(sfx.subprocess, "run", fake)

    assert sfx.normalize_clip(clip, "ffmpeg") is False
    assert clip.read_bytes() == original


def test_ensure_room_tone_generates_once_and_respects_flags(tmp_path, monkeypatch):
    calls = []

    def fake_generate(prompt, dst, url, timeout=180, seed=None):
        calls.append((prompt, seed))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"flac")
        return Path(dst)

    monkeypatch.setattr(sfx, "generate_sfx", fake_generate)
    monkeypatch.setattr(sfx, "normalize_clip", lambda *a, **k: True)

    settings = _Settings()
    first = sfx.ensure_room_tone(tmp_path, settings)
    assert first is not None and first.name == sfx.ROOM_TONE_FILE
    assert len(calls) == 1 and calls[0][1] is not None  # deterministic seed

    # Cached: no second generation.
    assert sfx.ensure_room_tone(tmp_path, settings) == first
    assert len(calls) == 1

    # generate=False without a cache -> None, and no generation happens.
    (tmp_path / "sfx" / sfx.ROOM_TONE_FILE).unlink()
    assert sfx.ensure_room_tone(tmp_path, settings, generate=False) is None
    assert len(calls) == 1

    # Disabled by setting.
    settings.sfx_room_tone = False
    assert sfx.ensure_room_tone(tmp_path, settings) is None


def test_duration_bucket_steps_and_cap():
    assert sfx._duration_bucket(3) == 5
    assert sfx._duration_bucket(5) == 5
    assert sfx._duration_bucket(12) == 15
    assert sfx._duration_bucket(30) == 30
    assert sfx._duration_bucket(48) == 30


def test_generate_sfx_sends_seconds_only_when_given(monkeypatch, tmp_path):
    import json as jsonlib
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b"flac-bytes"

    def urlopen(req, timeout):
        seen.append(jsonlib.loads(req.data))
        return Resp()

    monkeypatch.setattr(sfx.urllib.request, "urlopen", urlopen)
    sfx.generate_sfx("rain", tmp_path / "a.flac", "http://x", seconds=15)
    sfx.generate_sfx("rain", tmp_path / "b.flac", "http://x")
    assert seen[0]["args"]["seconds"] == 15.0
    assert "seconds" not in seen[1]["args"]


def _span_cards():
    @dataclass
    class Card:
        sfx_query: str = ""
        sfx_output: str = ""

    hum1, hum2 = Card("server hum"), Card("server hum")
    rain = Card("rain on window")
    return [
        (hum1, (0, 4)),
        (hum2, (4, 10)),    # same query, adjacent -> one 10s span
        (rain, (10, 22)),   # 12s -> bucket 15
    ], (hum1, hum2, rain)


def test_ensure_sfx_for_spans_generates_to_span_length(tmp_path, monkeypatch):
    cards, (hum1, hum2, rain) = _span_cards()
    calls = []

    def fake_generate(prompt, dst, url, timeout=180, seed=None, seconds=None):
        calls.append((prompt, seconds))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"flac")
        return Path(dst)

    monkeypatch.setattr(sfx, "woosh_ping", lambda *a, **k: True)
    monkeypatch.setattr(sfx, "provider_supports_seconds", lambda *a, **k: True)
    monkeypatch.setattr(sfx, "generate_sfx", fake_generate)
    monkeypatch.setattr(sfx, "normalize_clip", lambda *a, **k: True)

    changed = sfx.ensure_sfx_for_spans(tmp_path, cards, _Settings())

    assert changed is True
    assert calls == [("server hum", 10.0), ("rain on window", 15.0)]
    assert hum1.sfx_output == hum2.sfx_output
    assert hum1.sfx_output.endswith("_10s.flac")
    assert rain.sfx_output.endswith("_15s.flac")


def test_ensure_sfx_for_spans_falls_back_to_fixed_length(tmp_path, monkeypatch):
    cards, (hum1, hum2, rain) = _span_cards()
    calls = []

    def fake_generate(prompt, dst, url, timeout=180, seed=None, seconds=None):
        calls.append((prompt, seconds))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"flac")
        return Path(dst)

    monkeypatch.setattr(sfx, "woosh_ping", lambda *a, **k: True)
    monkeypatch.setattr(sfx, "provider_supports_seconds", lambda *a, **k: False)
    monkeypatch.setattr(sfx, "generate_sfx", fake_generate)
    monkeypatch.setattr(sfx, "normalize_clip", lambda *a, **k: True)

    assert sfx.ensure_sfx_for_spans(tmp_path, cards, _Settings()) is True
    assert [seconds for _q, seconds in calls] == [None, None]
    assert hum1.sfx_output.endswith(".flac") and "_10s" not in hum1.sfx_output


def test_ensure_sfx_for_spans_keeps_manual_sounds(tmp_path, monkeypatch):
    cards, (hum1, hum2, rain) = _span_cards()
    manual = tmp_path / "manual.wav"
    manual.write_bytes(b"wav")
    rain.sfx_output = str(manual)

    monkeypatch.setattr(sfx, "woosh_ping", lambda *a, **k: True)
    monkeypatch.setattr(sfx, "provider_supports_seconds", lambda *a, **k: True)
    generated = []

    def fake_generate(prompt, dst, url, timeout=180, seed=None, seconds=None):
        generated.append(prompt)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"flac")
        return Path(dst)

    monkeypatch.setattr(sfx, "generate_sfx", fake_generate)
    monkeypatch.setattr(sfx, "normalize_clip", lambda *a, **k: True)

    sfx.ensure_sfx_for_spans(tmp_path, cards, _Settings())

    assert generated == ["server hum"]        # rain span fully covered manually
    assert rain.sfx_output == str(manual)     # never replaced


def test_consolidate_sfx_queries_one_ambience_per_scene():
    from core.shots import consolidate_sfx_queries

    @dataclass
    class Shot:
        scene_index: int
        sfx_query: str = ""
        sfx_accent_query: str = ""

    shots = [
        Shot(1, "distant city morning ambience"),
        Shot(1, "phone haptic vibration sound"),
        Shot(1, "electronic device hum and clicking"),
        Shot(1, "card machine beep and jet engine roar"),
        Shot(2, "phone ringtone beep"),          # only events in scene 2
        Shot(2, ""),
        Shot(3, "waves crashing on rocks"),
        Shot(3, "seagull cry snap"),
    ]
    logs: list[str] = []
    consolidate_sfx_queries(shots, log_callback=logs.append)

    # Scene 1: the first loopable query wins for every shot.
    assert [s.sfx_query for s in shots[:4]] == ["distant city morning ambience"] * 4
    # Scene 2: nothing loopable -> empty (room tone covers it).
    assert [s.sfx_query for s in shots[4:6]] == ["", ""]
    # Scene 3: ambience kept, event query replaced.
    assert [s.sfx_query for s in shots[6:]] == ["waves crashing on rocks"] * 2
    # Event queries move to the accent layer instead of vanishing.
    assert shots[1].sfx_accent_query == "phone haptic vibration sound"
    assert shots[4].sfx_accent_query == "phone ringtone beep"
    assert shots[7].sfx_accent_query == "seagull cry snap"
    assert shots[0].sfx_accent_query == ""
    # Assert on the stable prefix rather than the phrasing: the message text is
    # user-facing copy and changes with the sound strategy.
    assert logs and logs[0].startswith("Звук:")


def test_consolidate_sfx_queries_caps_ambience_beds():
    """Sound is spot work: only the longest scenes keep an ambience bed."""
    from core.shots import MAX_AMBIENCE_BEDS, consolidate_sfx_queries

    @dataclass
    class Shot:
        scene_index: int
        start: str = "00:00:00"
        end: str = "00:00:10"
        sfx_query: str = ""
        sfx_accent_query: str = ""

    # One scene per index, each longer than the previous one.
    shots = [
        Shot(index, "00:00:00", f"00:00:{index * 5:02d}", "city traffic ambience")
        for index in range(1, MAX_AMBIENCE_BEDS + 4)
    ]
    consolidate_sfx_queries(shots)

    kept = [shot.scene_index for shot in shots if shot.sfx_query]
    assert len(kept) == MAX_AMBIENCE_BEDS
    # The longest scenes survive; the shortest lose their bed.
    assert kept == sorted(kept)[-MAX_AMBIENCE_BEDS:]
    assert 1 not in kept


def test_consolidate_sfx_queries_limits_accents_and_fixes_layers():
    """One accent per scene, and misfiled values are corrected, not generated."""
    from core.shots import consolidate_sfx_queries

    @dataclass
    class Shot:
        scene_index: int
        start: str = "00:00:00"
        end: str = "00:00:10"
        sfx_query: str = ""
        sfx_accent_query: str = ""

    shots = [
        Shot(1, sfx_accent_query="door knock"),
        Shot(1, sfx_accent_query="phone vibration buzz"),  # second in the scene
        Shot(2, sfx_accent_query="city traffic ambience"),  # sustained, not an event
        Shot(3, sfx_accent_query="glass shatter"),          # own scene: kept
    ]
    consolidate_sfx_queries(shots)

    assert shots[0].sfx_accent_query == "door knock"
    assert shots[1].sfx_accent_query == ""
    assert shots[2].sfx_accent_query == ""
    assert shots[3].sfx_accent_query == "glass shatter"


def test_sfx_loopability_classifier():
    from core.shots import _sfx_query_is_loopable

    assert _sfx_query_is_loopable("server room fan hum")
    assert _sfx_query_is_loopable("rain on window")
    assert _sfx_query_is_loopable("city traffic ambience")
    # Ambience hint outweighs an event word inside a composite query.
    assert _sfx_query_is_loopable("electronic device hum and clicking")
    assert not _sfx_query_is_loopable("phone haptic vibration sound")
    assert not _sfx_query_is_loopable("doorbell ding")
    assert not _sfx_query_is_loopable("")


def test_bed_places_accents_once_at_shot_start(tmp_path, monkeypatch):
    hum = _clip(tmp_path, "hum.flac")
    accent = _clip(tmp_path, "sfx_acc_beep.flac")

    @dataclass
    class Card:
        sfx_output: str = ""
        sfx_accent_output: str = ""

    cards = [
        (Card(str(hum)), (0, 6)),
        (Card(str(hum), str(accent)), (6, 10)),
    ]
    fake = _FakeRun()
    monkeypatch.setattr(sfx.subprocess, "run", fake)

    bed = sfx.build_sfx_bed(cards, tmp_path / "exports", "ffmpeg", 10, volume_db=-24.0)

    assert bed is not None
    cmd = fake.calls[0]
    assert str(accent) in cmd
    graph = cmd[cmd.index("-filter_complex") + 1]
    accent_branch = next(part for part in graph.split(";") if "[a1]" in part)
    assert "aloop" not in accent_branch          # events are never looped
    assert "adelay=6000:all=1" in accent_branch  # placed at the shot start
    assert "volume=-16.0dB" in accent_branch     # bed volume + 8 dB
    assert "amix=inputs=3:normalize=0" in graph  # span + accent + silence


def test_ensure_sfx_accents_generates_and_respects_manual(tmp_path, monkeypatch):
    @dataclass
    class Card:
        sfx_accent_query: str = ""
        sfx_accent_output: str = ""

    manual = tmp_path / "manual.wav"
    manual.write_bytes(b"wav")
    a = Card("phone vibration")
    b = Card("phone vibration")            # same query -> shared clip
    c = Card("doorbell ding", str(manual))  # manual file kept
    d = Card("")

    calls = []

    def fake_generate(prompt, dst, url, timeout=180, seed=None, seconds=None):
        calls.append((prompt, seconds))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"flac")
        return Path(dst)

    monkeypatch.setattr(sfx, "woosh_ping", lambda *a_, **k: True)
    monkeypatch.setattr(sfx, "provider_supports_seconds", lambda *a_, **k: True)
    monkeypatch.setattr(sfx, "generate_sfx", fake_generate)
    monkeypatch.setattr(sfx, "normalize_clip", lambda *a_, **k: True)

    changed = sfx.ensure_sfx_accents(tmp_path, [a, b, c, d], _Settings())

    assert changed is True
    assert calls == [("phone vibration", float(sfx.ACCENT_MAX_SECONDS))]
    assert a.sfx_accent_output == b.sfx_accent_output
    assert "sfx_acc_" in a.sfx_accent_output
    assert c.sfx_accent_output == str(manual)
    assert d.sfx_accent_output == ""


def test_mix_graph_ducks_sfx_gently_under_voice():
    graph = _audio_mix_filtergraph(
        0, 60, -18.0, True, music_index=2, sfx_index=3, duck_sfx=True,
    )
    assert "[voc]asplit=3[vocm][vkey][vskey]" in graph
    assert "[mus0][vkey]sidechaincompress=threshold=0.03:ratio=8" in graph
    assert "[sfxbed][vskey]sidechaincompress=threshold=0.06:ratio=2" in graph
    assert "amix=inputs=3:normalize=0" in graph


def test_mix_graph_without_sfx_matches_legacy_shape():
    graph = _audio_mix_filtergraph(0, 60, -18.0, True, music_index=2)
    assert "[voc]asplit=2[vocm][vkey]" in graph
    assert "sidechaincompress=threshold=0.03:ratio=8" in graph
    assert "vskey" not in graph
    assert "amix=inputs=2:normalize=0" in graph

    plain = _audio_mix_filtergraph(0, 60, -18.0, False)
    assert "asplit" not in plain and "anull" in plain


def test_mix_graph_sfx_duck_can_be_disabled():
    graph = _audio_mix_filtergraph(
        0, 60, -18.0, False, sfx_index=2, duck_sfx=False,
    )
    assert "asplit" not in graph
    assert "[sfxbed]" in graph and "sfxduck" not in graph
    assert "amix=inputs=2:normalize=0" in graph
