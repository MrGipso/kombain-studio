"""ffmpeg path escaping and the bounded subprocess runner used for clips."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core import export
from core.process import run_captured

FFMPEG = shutil.which("ffmpeg")


def test_concat_line_escapes_single_quotes(tmp_path):
    clip = tmp_path / "Mike's dir" / "clip.mp4"

    line = export.concat_file_line(clip)

    escaped = str(clip.resolve()).replace("'", "'\\''")
    assert line == f"file '{escaped}'"


def test_filter_path_escapes_colons_and_quotes():
    path = Path("C:/Users/Mike's/overlay.txt")

    assert export.ffmpeg_filter_path(path) == "C\\:/Users/Mike'\\\\\\''s/overlay.txt"


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not on PATH")
def test_ffmpeg_reads_concat_list_under_quoted_directory(tmp_path):
    root = tmp_path / "Mike's dir"
    root.mkdir()
    clip = root / "clip.mp4"
    made = run_captured(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
         "color=c=black:s=64x64:d=0.2", str(clip)],
        timeout=60,
    )
    assert made.returncode == 0, made.stderr

    concat = export.build_concat_file_from_paths([clip, clip], root)
    result = run_captured(
        [FFMPEG, "-v", "error", "-f", "concat", "-safe", "0", "-i", concat,
         "-f", "null", "-"],
        timeout=60,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not on PATH")
def test_ffmpeg_drawtext_reads_textfile_under_quoted_directory(tmp_path):
    def drawtext(directory: Path):
        directory.mkdir()
        text = directory / "overlay.txt"
        text.write_text("hello", encoding="utf-8")
        vf = f"drawtext=textfile='{export.ffmpeg_filter_path(text)}':fontcolor=white"
        return run_captured(
            [FFMPEG, "-v", "error", "-f", "lavfi", "-i",
             "color=c=black:s=64x64:d=0.1", "-vf", vf, "-frames:v", "1",
             "-f", "null", "-"],
            timeout=60,
        )

    control = drawtext(tmp_path / "plain dir")
    if control.returncode != 0:
        pytest.skip(f"drawtext unavailable in this ffmpeg build: {control.stderr[-200:]}")

    result = drawtext(tmp_path / "Mike's dir")

    assert result.returncode == 0, result.stderr


def test_run_captured_kills_a_wedged_child_and_reports_failure():
    result = run_captured(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.5,
    )

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == -1
    assert "Timed out after 0.5 s" in result.stderr


def test_run_captured_decodes_utf8_regardless_of_locale():
    result = run_captured(
        [sys.executable, "-c",
         "import sys; sys.stderr.buffer.write('ошибка'.encode('utf-8'))"],
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stderr == "ошибка"


def test_run_captured_still_raises_for_a_missing_binary(tmp_path):
    # Callers such as sfx.normalize_clip rely on OSError for "not installed".
    with pytest.raises(OSError):
        run_captured([str(tmp_path / "missing-ffmpeg.exe")], timeout=5)


def test_run_captured_terminates_child_when_job_is_cancelled():
    import time

    from core.process import ProcessCancelled

    started = time.monotonic()
    polls = {"count": 0}

    def cancel_after_first_poll():
        polls["count"] += 1
        return polls["count"] > 1

    with pytest.raises(ProcessCancelled):
        run_captured(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=60,
            cancel_check=cancel_after_first_poll,
            cancel_poll_seconds=0.05,
        )

    assert time.monotonic() - started < 10


def test_run_captured_propagates_the_callers_cancellation_exception():
    class JobCancelled(Exception):
        pass

    def cancelled():
        raise JobCancelled("stop")

    with pytest.raises(JobCancelled):
        run_captured(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=60,
            cancel_check=cancelled,
        )


def test_run_captured_with_cancel_check_returns_output_and_times_out():
    finished = run_captured(
        [sys.executable, "-c",
         "import sys; print('ok'); "
         "sys.stderr.buffer.write('ошибка'.encode('utf-8')); sys.exit(3)"],
        timeout=30,
        cancel_check=lambda: False,
        cancel_poll_seconds=0.05,
    )
    assert finished.returncode == 3
    assert finished.stdout.strip() == "ok"
    assert finished.stderr == "ошибка"

    wedged = run_captured(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.5,
        cancel_check=lambda: False,
        cancel_poll_seconds=0.05,
    )
    assert wedged.returncode == -1
    assert "Timed out after 0.5 s" in wedged.stderr
