"""File names: cleaning up titles, reading artist and title from a name, and URL checks."""

import re
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_FILENAME = "Youtube MP3"
UNKNOWN_ARTIST = "<unknown>"
YOUTUBE_HOSTS = ("youtube.com", "youtu.be/")
NOISE_WORDS = ("Official", "Music", "Video", "HD", "Lyrics", "Lyric", "Audio", "Explicit", "Clean", "4K")
# a file name can have 255 UTF-16 units on Windows, and ".mp3" takes four
MAX_FILENAME_CHARS = 251
# a longer whole path works in ffmpeg, but Python can't see the file then (unless Windows allows long paths)
MAX_PATH_CHARS = 259

WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"{port}{i}" for port in ("COM", "LPT") for i in range(1, 10)]
)

FORBIDDEN_CHARS_RE = re.compile(r'[\\/*?:"<>|]')
# control characters \x1c-\x1f count as whitespace and are collapsed by WHITESPACE_RE
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0e-\x1b]")
_NOISE_WORD = "(?:" + "|".join(map(re.escape, NOISE_WORDS)) + ")"
# a bracket group made only of noise words, e.g. "(Official Music Video)" or "[HD]";
# noise words outside brackets are part of the title ("Live and Let Die")
NOISE_GROUP_RE = re.compile(
    rf"[(\[]\s*{_NOISE_WORD}(?:[\s,&/+-]+{_NOISE_WORD})*\s*[)\]]", flags=re.IGNORECASE
)
EMPTY_BRACKETS_RE = re.compile(r"[(\[]\s*[)\]]")
WHITESPACE_RE = re.compile(r"\s+")
ARTIST_SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")


def is_web_url(url: object) -> bool:
    """Check that a URL can be handed to yt-dlp: http(s) with a host.
    yt-dlp supports many sites besides YouTube and reports the ones it can't download.
    The scheme check also keeps the URL from being read as a yt-dlp option ("-...").

    Args:
        url: The URL to check, from a request (so possibly not a string).
    Returns:
        True if the URL is an http or https URL with a host name.
    """
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:  # e.g. a broken IPv6 host
        return False


def is_youtube_url(url: str) -> bool:
    """Loosely check that a URL points to YouTube, for YouTube-only repairs (Deno).

    Args:
        url: The URL to check.
    Returns:
        True if the URL contains a YouTube host name.
    """
    return any(host in url for host in YOUTUBE_HOSTS)


def clean_title(name: str) -> str:
    """Remove control characters, bracketed YouTube title noise, empty brackets and repeated whitespace.

    Args:
        name: The raw title.
    Returns:
        The cleaned title, which may still hold characters a filename can't (for the tags).
    """
    name = CONTROL_CHARS_RE.sub("", name)
    name = NOISE_GROUP_RE.sub("", name)
    name = EMPTY_BRACKETS_RE.sub("", name)
    return WHITESPACE_RE.sub(" ", name).strip()


def _utf16_units(name: str) -> bytes:
    return name.encode("utf-16-le", errors="surrogatepass")


def filename_limit(folder: Path | None = None) -> int:
    """The longest filename (without extension, in UTF-16 units like Windows counts) that fits.
    Args:
        folder: The folder it goes in; None for the limit of the name alone.
    """
    if folder is None:
        return MAX_FILENAME_CHARS
    room = MAX_PATH_CHARS - len(_utf16_units(str(folder))) // 2 - len("\\.mp3")
    return max(1, min(MAX_FILENAME_CHARS, room))


def filename_too_long(name: str, folder: Path | None = None) -> bool:
    """Whether a filename without extension is longer than filename_limit."""
    return len(_utf16_units(name)) > filename_limit(folder) * 2


def filename_too_long_message(folder: Path) -> str:
    """What the user is told when the filename they asked for does not fit in folder."""
    return f"The filename is too long for this folder (at most {filename_limit(folder)} characters)"


def fit_filename(name: str, folder: Path | None = None) -> str:
    """Shorten a filename made from a video title to what Windows allows.

    Args:
        name: The sanitized filename without extension.
        folder: The folder it goes in, see filename_limit.
    Returns:
        The name, cut at filename_limit without splitting a character.
    """
    if not filename_too_long(name, folder):
        return name
    return _utf16_units(name)[:filename_limit(folder) * 2].decode("utf-16-le", errors="ignore").rstrip()


def safe_filename(name: object) -> str:
    """Turn a video title or user input into a valid Windows filename.
    Removes characters Windows forbids in filenames, bracketed YouTube title noise
    ("(Official Video)", "[Lyrics]", ...), empty brackets and repeated whitespace.
    A name Windows reserves for a device (CON, NUL, COM1, ...) gets a "_" prefix.

    Args:
        name: The raw name. Anything that is not a non-empty string is treated as empty.
    Returns:
        The cleaned name, or DEFAULT_FILENAME if nothing meaningful is left.

    Example:
        >>> safe_filename('Artist - Song (Official Video) [HD]')
        'Artist - Song'
    """
    if not isinstance(name, str):
        return DEFAULT_FILENAME
    name = clean_title(FORBIDDEN_CHARS_RE.sub("", name))
    # Windows ignores everything after the first dot (and trailing spaces) here
    if name.split(".", 1)[0].rstrip().upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name or DEFAULT_FILENAME


def requested_filename(name: str) -> str:
    """Sanitize a filename requested by the user.

    Args:
        name: The filename from the request or the GUI field, possibly empty.
    Returns:
        The sanitized filename, or "" if nothing meaningful was given, which tells
        download_audio to name the file after the video title.
    """
    filename = safe_filename(name)
    return "" if filename.lower() == DEFAULT_FILENAME.lower() else filename


def artist_from_filename(filename: str) -> str:
    """Derive the artist metadata from a filename.

    Args:
        filename: The final filename without extension, e.g. "Artist - Song".
    Returns:
        The text before the first spaced dash (" - ", " – ", " — "), so "Jay-Z - Song"
        gives "Jay-Z"; UNKNOWN_ARTIST without one ("Anti-Hero" names no artist).
    """
    match = ARTIST_SEPARATOR_RE.search(filename)
    return (filename[:match.start()].strip() if match else "") or UNKNOWN_ARTIST


def title_from_filename(filename: str) -> str:
    """Derive the title metadata from a filename, the counterpart of artist_from_filename.

    Args:
        filename: The final filename without extension, e.g. "Artist - Song".
    Returns:
        The text after the spaced dash that artist_from_filename splits at, or the
        whole filename if there is none (or nothing after it).
    """
    match = ARTIST_SEPARATOR_RE.search(filename)
    return (filename[match.end():].strip() if match else "") or filename
