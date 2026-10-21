"""Pure helpers of core.py: filenames, artist, folders, URL check, error messages."""

import subprocess
from pathlib import Path

import pytest

from ytmp3_host import core
from ytmp3_host.core import (
    DEFAULT_FILENAME,
    UNKNOWN_ARTIST,
    artist_from_filename,
    build_download_command,
    describe_error,
    is_youtube_url,
    requested_filename,
    resolve_folder,
    safe_filename,
    title_from_filename,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Artist - Song (Official Video) [HD]", "Artist - Song"),
        ("Artist - Song (Official Music Video)", "Artist - Song"),
        ("Artist - Song [Official Lyric Video]", "Artist - Song"),
        ("Artist - Song (Lyrics) (Live)", "Artist - Song"),
        ("Artist - Song (official video - HD)", "Artist - Song"),
        # noise words outside brackets, or next to other words, are part of the title
        ("Paul McCartney - Live and Let Die", "Paul McCartney - Live and Let Die"),
        ("Stardust - Music Sounds Better With You", "Stardust - Music Sounds Better With You"),
        ("Artist - Song (Live at Wembley)", "Artist - Song (Live at Wembley)"),
        ("Artist - Song (HDR Remaster)", "Artist - Song (HDR Remaster)"),
        # characters Windows forbids
        ('AC/DC - Who? "Me": <yes> | *no*', "ACDC - Who Me yes no"),
        ("Back\\slash", "Backslash"),
        ("bell\x07 and null\x00", "bell and null"),
        ("tab\tnew\nline\x1f  spaces", "tab new line spaces"),
        ("  padded  ", "padded"),
        ("Artist - Song ()", "Artist - Song"),
        ("Artíst – Søng 日本", "Artíst – Søng 日本"),
    ],
)
def test_safe_filename(title: str, expected: str) -> None:
    assert safe_filename(title) == expected


@pytest.mark.parametrize("name", ["", "   ", "(Official Video)", '???***', None, 42, ["a"]])
def test_safe_filename_falls_back_when_nothing_is_left(name: object) -> None:
    assert safe_filename(name) == DEFAULT_FILENAME


@pytest.mark.parametrize(("name", "expected"), [("CON", "_CON"), ("nul", "_nul"), ("Com1", "_Com1"), ("LPT9.x", "_LPT9.x")])
def test_safe_filename_avoids_windows_device_names(name: str, expected: str) -> None:
    assert safe_filename(name) == expected


@pytest.mark.parametrize("name", ["Console", "COM10", "Nulled", "Aux Cord"])
def test_safe_filename_keeps_names_that_only_look_reserved(name: str) -> None:
    assert safe_filename(name) == name


@pytest.mark.parametrize(
    ("name", "expected"),
    [("", ""), ("  ", ""), ("Youtube MP3", ""), ("youtube mp3", ""), ("<>", ""), ("My: Song", "My Song")],
)
def test_requested_filename(name: str, expected: str) -> None:
    assert requested_filename(name) == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Artist - Song", "Artist"),
        ("Jay-Z - Empire State of Mind", "Jay-Z"),
        ("AC-DC - Thunderstruck - Live", "AC-DC"),
        ("Artist – Song", "Artist"),
        ("Artist — Song", "Artist"),
        ("Artist-Song", "Artist"),
        ("Song without artist", UNKNOWN_ARTIST),
        ("- Song", UNKNOWN_ARTIST),
        ("-Song", UNKNOWN_ARTIST),
    ],
)
def test_artist_from_filename(filename: str, expected: str) -> None:
    assert artist_from_filename(filename) == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Artist - Song", "Song"),
        ("Jay-Z - Empire State of Mind", "Empire State of Mind"),
        ("AC-DC - Thunderstruck - Live", "Thunderstruck - Live"),
        ("Artist – Song", "Song"),
        ("Artist-Song", "Song"),
        ("Song without artist", "Song without artist"),
        ("- Song", "Song"),
        ("Artist -", "Artist -"),
    ],
)
def test_title_from_filename(filename: str, expected: str) -> None:
    assert title_from_filename(filename) == expected


def test_resolve_folder_empty_creates_default(default_folder: Path) -> None:
    assert not default_folder.exists()
    assert resolve_folder("  ") == default_folder
    assert default_folder.is_dir()


def test_resolve_folder_existing(tmp_path: Path) -> None:
    assert resolve_folder(f"  {tmp_path}  ") == tmp_path.resolve()


def test_resolve_folder_missing_returns_none(tmp_path: Path) -> None:
    assert resolve_folder(str(tmp_path / "missing")) is None


def test_resolve_folder_file_is_not_a_folder(tmp_path: Path) -> None:
    file = tmp_path / "file.txt"
    file.write_text("x")
    assert resolve_folder(str(file)) is None


def test_resolve_folder_names_missing_default(default_folder: Path) -> None:
    assert resolve_folder(str(default_folder)) == default_folder
    assert default_folder.is_dir()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc", True),
        ("https://music.youtube.com/watch?v=abc", True),
        ("https://youtu.be/abc", True),
        ("https://vimeo.com/123", False),
        ("youtu.be", False),
        ("", False),
    ],
)
def test_is_youtube_url(url: str, expected: bool) -> None:
    assert is_youtube_url(url) is expected


def test_build_download_command_keeps_quality_and_contract(tmp_path: Path) -> None:
    stem = tmp_path / "_ytmp3_x"
    cmd = build_download_command("https://youtu.be/abc", stem)

    def value(flag: str) -> str:
        return cmd[cmd.index(flag) + 1]

    assert cmd[0] == str(core.YT_DLP_PATH)
    assert cmd[-1] == "https://youtu.be/abc"
    assert value("--audio-format") == "mp3"
    assert value("--audio-quality") == "0"  # LAME V0, see CLAUDE.md priorities
    assert value("-o") == f"{stem}.%(ext)s"
    assert value("--print") == f"before_dl:{core.TITLE_MARKER}%(title)s"
    assert "--no-simulate" in cmd  # --print must never turn the call into a dry run
    assert value("--ffmpeg-location") == str(core.FFMPEG_PATH)
    assert {"--no-playlist", "--write-thumbnail", "-x", "--newline", "--progress"} <= set(cmd)


# --- describe_error -----------------------------------------------------------

YT_DLP_CMD = ["C:\\app\\yt-dlp.exe", "--no-playlist", "https://youtu.be/abc"]
FFMPEG_CMD = ["C:\\app\\ffmpeg.exe", "-i", "in.mp3", "out.mp3"]


def test_describe_error_yt_dlp_picks_last_error_line() -> None:
    output = "[youtube] abc\nWARNING: meh\nERROR: first problem\n\nERROR: Video unavailable\n[info] done\n"
    error = subprocess.CalledProcessError(1, YT_DLP_CMD, output)
    assert describe_error(error) == "yt-dlp.exe failed: Video unavailable"


def test_describe_error_without_error_lines_uses_last_line() -> None:
    error = subprocess.CalledProcessError(1, YT_DLP_CMD, "line one\nsomething broke\n  \n")
    assert describe_error(error) == "yt-dlp.exe failed: something broke"


def test_describe_error_without_output_uses_exit_code() -> None:
    assert describe_error(subprocess.CalledProcessError(3, YT_DLP_CMD)) == "yt-dlp.exe failed: exit code 3"


def test_describe_error_ffmpeg_reads_stderr() -> None:
    error = subprocess.CalledProcessError(1, FFMPEG_CMD, None, "in.mp3: Invalid data found when processing input\n")
    assert describe_error(error) == "ffmpeg.exe failed: in.mp3: Invalid data found when processing input"


def test_describe_error_does_not_depend_on_the_binary_name() -> None:
    error = subprocess.CalledProcessError(1, ["C:\\app\\yt-dlp.new.exe"], "ERROR: boom")
    assert describe_error(error) == "yt-dlp.new.exe failed: boom"


def test_describe_error_truncates_long_reasons() -> None:
    error = subprocess.CalledProcessError(1, YT_DLP_CMD, "ERROR: " + "x" * 1000)
    assert describe_error(error) == "yt-dlp.exe failed: " + "x" * 300


def test_describe_error_never_shows_the_command_line() -> None:
    error = subprocess.CalledProcessError(1, YT_DLP_CMD, "ERROR: boom")
    assert "--no-playlist" not in describe_error(error)
    timeout = subprocess.TimeoutExpired(YT_DLP_CMD, 300)
    assert describe_error(timeout) == "Download timed out after 300 s (yt-dlp.exe)"


def test_describe_error_file_exists() -> None:
    error = FileExistsError(17, "File with this name already exists", "C:\\music\\Artist - Song.mp3")
    assert describe_error(error) == "File with this name already exists: Artist - Song.mp3"


def test_describe_error_os_errors() -> None:
    missing = FileNotFoundError(2, "ffmpeg.exe not found, please reinstall the app", "C:\\app\\ffmpeg.exe")
    assert describe_error(missing) == "ffmpeg.exe not found, please reinstall the app: C:\\app\\ffmpeg.exe"
    assert describe_error(PermissionError(13, "Access is denied")) == "Access is denied"


def test_describe_error_unexpected() -> None:
    assert describe_error(RuntimeError("boom")) == "Unexpected error: boom"
    assert describe_error(OSError("no strerror")) == "Unexpected error: no strerror"
