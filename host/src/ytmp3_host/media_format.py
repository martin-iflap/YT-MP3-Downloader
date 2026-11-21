from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, assert_never, get_args

type Mp4Quality = Literal["best", "1080", "720", "480"]
type Mp4Codec = Literal["best", "compatible"]

MP4_QUALITIES: tuple[Mp4Quality, ...] = get_args(Mp4Quality.__value__)
MP4_CODECS: tuple[Mp4Codec, ...] = get_args(Mp4Codec.__value__)


@dataclass(frozen=True, slots=True)
class Mp3Format: ...    # the bitrate later goes here, if we ever add it ;)


@dataclass(frozen=True, slots=True)
class Mp4Format:
    """The requested MP4 format and its options.
     - quality: The requested quality (best, 1080, 720, 480).
     - codec: The requested codec (best, compatible).
     Options are validated against the allowed values in MP4_QUALITIES and MP4_CODECS.
    """
    quality: Mp4Quality = "best"
    codec: Mp4Codec = "best"


# type alias for the union of the two format classes we use throughout the host
type MediaFormat = Mp3Format | Mp4Format


def _option[T: str](request: Mapping[str, object], key: str, allowed: tuple[T, ...]) -> T:
    """Read one option of a request.
    Raises:
        ValueError: If the option is present but not one of allowed (including null or a number).
    """
    value = request.get(key, allowed[0])  # a missing key means the default
    for choice in allowed:
        if value == choice:
            return choice
    raise ValueError(f"Unknown {key}: {value!r}")


def media_format_from_request(request: Mapping[str, object]) -> MediaFormat:
    """The format a download request asks for; a request without one is an MP3 (older extensions).
    Raises:
        ValueError: If the format or one of its options is unknown.
    """
    match request.get("format", "mp3"):
        case "mp3":
            return Mp3Format()
        case "mp4":
            return Mp4Format(
                quality=_option(request, "quality", MP4_QUALITIES),
                codec=_option(request, "codec", MP4_CODECS),
            )
        case other:
            raise ValueError(f"Unknown format: {other!r}")


def get_suffix(media_format: MediaFormat) -> str:
    """Get the file extension for a given media format."""
    match media_format:
        case Mp3Format():
            return ".mp3"
        case Mp4Format():
            return ".mp4"
        case _ as unreachable:
            assert_never(unreachable)
