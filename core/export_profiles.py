"""Validated, named delivery specifications for FFmpeg exports.

Profiles are deliberately closed over a small registry.  API clients select a
name instead of supplying raw FFmpeg values, which keeps command construction
predictable and prevents user-controlled strings from becoming encoder flags.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from types import MappingProxyType
from typing import Literal, Mapping


ExportProfileName = Literal["preview", "delivery", "master"]
DEFAULT_EXPORT_PROFILE: ExportProfileName = "delivery"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_VIDEO_CODECS = {"h264"}
_PIXEL_FORMATS = {"yuv420p"}
_AUDIO_CODECS = {"aac"}
_CPU_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower",
}
_NVENC_PRESETS = {"p1", "p2", "p3", "p4", "p5", "p6", "p7"}
_SAMPLE_RATES = {44_100, 48_000}


def _bounded_int(field: str, value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class ExportProfile:
    """One fully validated output contract.

    All values later placed in the FFmpeg argument vector are either bounded
    numbers or members of an explicit allow-list.  ``video_bitrate_kbps`` is an
    optional target; ``max_video_bitrate_kbps`` is an optional VBV ceiling.
    """

    name: str
    label: str
    width: int
    height: int
    fps: int
    video_codec: str = "h264"
    pixel_format: str = "yuv420p"
    crf: int = 18
    video_bitrate_kbps: int | None = None
    max_video_bitrate_kbps: int | None = None
    cpu_preset: str = "veryfast"
    nvenc_preset: str = "p5"
    nvenc_cq: int = 19
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 192
    audio_sample_rate: int = 48_000
    audio_channels: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_RE.fullmatch(self.name):
            raise ValueError("profile name must be a safe lowercase identifier")
        if not isinstance(self.label, str) or not self.label.strip() or len(self.label) > 80:
            raise ValueError("profile label must contain 1-80 characters")
        width = _bounded_int("width", self.width, 320, 7680)
        height = _bounded_int("height", self.height, 240, 4320)
        if width % 2 or height % 2:
            raise ValueError("profile dimensions must be even")
        _bounded_int("fps", self.fps, 1, 120)
        _bounded_int("crf", self.crf, 0, 51)
        _bounded_int("nvenc_cq", self.nvenc_cq, 0, 51)
        if self.video_codec not in _VIDEO_CODECS:
            raise ValueError(f"unsupported video codec: {self.video_codec!r}")
        if self.pixel_format not in _PIXEL_FORMATS:
            raise ValueError(f"unsupported pixel format: {self.pixel_format!r}")
        if self.audio_codec not in _AUDIO_CODECS:
            raise ValueError(f"unsupported audio codec: {self.audio_codec!r}")
        if self.cpu_preset not in _CPU_PRESETS:
            raise ValueError(f"unsupported x264 preset: {self.cpu_preset!r}")
        if self.nvenc_preset not in _NVENC_PRESETS:
            raise ValueError(f"unsupported NVENC preset: {self.nvenc_preset!r}")
        if self.video_bitrate_kbps is not None:
            _bounded_int("video_bitrate_kbps", self.video_bitrate_kbps, 128, 200_000)
        if self.max_video_bitrate_kbps is not None:
            _bounded_int(
                "max_video_bitrate_kbps", self.max_video_bitrate_kbps, 128, 300_000,
            )
            if (
                self.video_bitrate_kbps is not None
                and self.max_video_bitrate_kbps < self.video_bitrate_kbps
            ):
                raise ValueError("max video bitrate cannot be below the target bitrate")
        _bounded_int("audio_bitrate_kbps", self.audio_bitrate_kbps, 64, 512)
        if self.audio_sample_rate not in _SAMPLE_RATES:
            raise ValueError(f"unsupported audio sample rate: {self.audio_sample_rate}")
        _bounded_int("audio_channels", self.audio_channels, 1, 2)
        if self.audio_channels not in {1, 2}:
            raise ValueError("audio_channels must be 1 (mono) or 2 (stereo)")

    @property
    def video_size(self) -> str:
        return f"{self.width}:{self.height}"

    @property
    def audio_bitrate(self) -> str:
        return f"{self.audio_bitrate_kbps}k"

    def as_public_dict(self) -> dict[str, object]:
        """JSON-ready representation used by the API and export manifest."""
        return asdict(self)


_PROFILES: dict[ExportProfileName, ExportProfile] = {
    "preview": ExportProfile(
        name="preview",
        label="Preview 720p",
        width=1280,
        height=720,
        fps=25,
        crf=23,
        video_bitrate_kbps=3_500,
        max_video_bitrate_kbps=5_000,
        cpu_preset="veryfast",
        nvenc_preset="p3",
        nvenc_cq=23,
        audio_bitrate_kbps=128,
    ),
    # Backward-compatible default: the previous exporter normalized every
    # narrated delivery to 1920x1080/25, x264 CRF 18 (or NVENC CQ 19), AAC 192k.
    "delivery": ExportProfile(
        name="delivery",
        label="Delivery 1080p",
        width=1920,
        height=1080,
        fps=25,
        crf=18,
        cpu_preset="veryfast",
        nvenc_preset="p5",
        nvenc_cq=19,
        audio_bitrate_kbps=192,
    ),
    "master": ExportProfile(
        name="master",
        label="Master 1080p",
        width=1920,
        height=1080,
        fps=25,
        crf=16,
        max_video_bitrate_kbps=30_000,
        cpu_preset="slow",
        nvenc_preset="p6",
        nvenc_cq=16,
        audio_bitrate_kbps=320,
    ),
}

EXPORT_PROFILES: Mapping[ExportProfileName, ExportProfile] = MappingProxyType(_PROFILES)


def resolve_export_profile(
    profile: ExportProfile | ExportProfileName | str | None = None,
) -> ExportProfile:
    """Resolve a registry profile; custom objects are revalidated by construction."""
    if profile is None:
        return EXPORT_PROFILES[DEFAULT_EXPORT_PROFILE]
    if isinstance(profile, ExportProfile):
        # Frozen instances are already validated by __post_init__.
        return profile
    try:
        return EXPORT_PROFILES[profile]  # type: ignore[index]
    except (KeyError, TypeError):
        allowed = ", ".join(EXPORT_PROFILES)
        raise ValueError(f"unknown export profile {profile!r}; expected one of: {allowed}") from None


def export_profile_catalog() -> dict[str, object]:
    return {
        "default": DEFAULT_EXPORT_PROFILE,
        "profiles": [profile.as_public_dict() for profile in EXPORT_PROFILES.values()],
    }
