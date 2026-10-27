"""Pure helpers of core.py: filenames, artist and tags, folders, URL check, error messages."""

import subprocess
import sys
from pathlib import Path

import pytest

from ytmp3_host import core
from ytmp3_host.core import (
    DEFAULT_FILENAME,
    UNKNOWN_ARTIST,
    artist_for,
    artist_from_filename,
    build_download_command,
    describe_error,
    downloaded_mp3,
    extra_tags,
    is_web_url,
    is_youtube_url,
    parse_info,
    requested_filename,
    resolve_folder,
    safe_filename,
    title_filename,
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


@pytest.mark.parametrize(
    ("filename", "info", "expected"),
    [
        # the filename's artist wins: the user sees it and may have typed it
        ("Artist - Song", {"artists": ["Someone Else"], "channel": "Channel"}, "Artist"),
        ("Song", {"artists": ["Queen", "David Bowie"], "channel": "Queen - Topic"}, "Queen, David Bowie"),
        ("Song", {"channel": "Queen - Topic"}, "Queen"),
        ("Song", {"channel": "jawed"}, "jawed"),
        ("Song", {"artists": ["Forss"], "uploader": "Forss"}, "Forss"),  # SoundCloud: no channel
        # Mixcloud's artists are the mix's tracklist: the uploader made the mix
        ("Mix", {"artists": ["A", "B"], "uploader": "Spartacus", "extractor_key": "Mixcloud"}, "Spartacus"),
        ("Song", {"artists": [None, " ", 5], "uploader": "Up"}, "Up"),
        ("Song", {"artists": [], "channel": ""}, UNKNOWN_ARTIST),
        ("Song", {}, UNKNOWN_ARTIST),
    ],
)
def test_artist_for(filename: str, info: dict[str, object], expected: str) -> None:
    assert artist_for(filename, info) == expected


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"title": "Flickermood", "artists": ["Forss"]}, "Forss - Flickermood"),
        ({"title": "Under Pressure (Official Video)", "artists": ["Queen", "David Bowie"]}, "Queen, David Bowie - Under Pressure"),
        # the title already names an artist
        ({"title": "Rick Astley - Never Gonna Give You Up", "artists": ["Rick Astley"]}, "Rick Astley - Never Gonna Give You Up"),
        # a dash inside a word is not an artist separator
        ({"title": "Spider-Man Theme", "artists": ["X"]}, "X - Spider-Man Theme"),
        # only a site's artists, not the channel: a vlog stays "Me at the zoo"
        ({"title": "Me at the zoo", "channel": "jawed"}, "Me at the zoo"),
        ({"title": "Party Time", "artists": ["A", "B"], "extractor_key": "Mixcloud"}, "Party Time"),
        ({"title": "AC/DC?", "artists": ["AC/DC"]}, "ACDC - ACDC"),
        ({"artists": ["Forss"]}, DEFAULT_FILENAME),
        ({}, DEFAULT_FILENAME),
    ],
)
def test_title_filename(info: dict[str, object], expected: str) -> None:
    assert title_filename(info) == expected


def test_extra_tags_keeps_only_the_values_a_video_has() -> None:
    info = {"album": "Whenever You Need Somebody", "release_year": 1987, "track_number": 1, "album_artist": "x",
            "webpage_url": "https://www.youtube.com/watch?v=abc", "channel": "Rick Astley"}
    assert extra_tags(info) == {
        "album": "Whenever You Need Somebody",
        "date": "1987",
        "track": "1",
        "comment": "https://www.youtube.com/watch?v=abc",
    }
    assert extra_tags({"album": "", "release_year": None}) == {}


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '"title"', ""])
def test_parse_info_ignores_anything_but_a_json_object(text: str) -> None:
    assert parse_info(text) == {}


def test_downloaded_mp3_accepts_only_existing_absolute_mp3s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mp3 = tmp_path / "Song.MP3"
    mp3.write_bytes(b"x")
    exe = tmp_path / "evil.exe"
    exe.write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    assert downloaded_mp3(str(mp3)) == mp3
    assert downloaded_mp3(str(exe)) is None  # play must never run a program
    assert downloaded_mp3(str(tmp_path / "gone.mp3")) is None
    assert downloaded_mp3(str(tmp_path)) is None
    assert downloaded_mp3("Song.MP3") is None  # relative
    assert downloaded_mp3("") is None


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


@pytest.mark.parametrize("folder", ["Music", "C:", "\\", "C:Music"])
def test_resolve_folder_rejects_relative_paths(folder: str) -> None:
    assert resolve_folder(folder) is None


def test_folder_is_writable_leaves_nothing_behind(tmp_path: Path) -> None:
    assert core.folder_is_writable(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_folder_is_writable_probe_is_gone_when_the_host_is_killed(tmp_path: Path) -> None:
    # the host killed while the probe is open: Windows still deletes it (O_TEMPORARY)
    code = (
        "import os, sys, time; from ytmp3_host import core\n"
        "core.os.close = lambda fd: (print('open', flush=True), time.sleep(60))\n"
        "core.folder_is_writable(core.Path(sys.argv[1]))\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "open"
    assert len(list(tmp_path.glob("_ytmp3_*.probe"))) == 1
    # the venv's python.exe is a launcher with the real Python as its child
    core.kill_process_tree(child)
    child.wait()
    assert list(tmp_path.iterdir()) == []


def test_folder_is_writable_false_when_windows_refuses(tmp_path: Path) -> None:
    # a folder that can't take a file, like a missing one
    assert not core.folder_is_writable(tmp_path / "missing")


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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc", True),
        ("https://soundcloud.com/artist/track", True),
        ("http://artist.bandcamp.com/track/song", True),
        ("https://", False),
        ("youtube.com/watch?v=abc", False),
        ("-o C:\\x https://youtu.be/abc", False),  # would be read as a yt-dlp option
        ("file:///C:/song.mp3", False),
        ("http://[::1", False),  # urlsplit raises ValueError
        ("", False),
        (None, False),
    ],
)
def test_is_web_url(url: object, expected: bool) -> None:
    assert is_web_url(url) is expected


def test_build_download_command_keeps_quality_and_contract(tmp_path: Path) -> None:
    stem = tmp_path / "_ytmp3_x"
    cmd = build_download_command("https://youtu.be/abc", stem)

    def value(flag: str) -> str:
        return cmd[cmd.index(flag) + 1]

    assert cmd[0] == str(core.YT_DLP_PATH)
    assert cmd[-1] == "https://youtu.be/abc"
    assert value("--audio-format") == "mp3"
    assert value("--audio-quality") == "0"  # LAME V0, see CLAUDE.md priorities
    assert value("--playlist-items") == "1"  # a playlist URL still gives one file
    assert value("-o") == f"{stem}.%(ext)s"
    assert value("--print") == (
        f"before_dl:{core.INFO_MARKER}%(.{{title,artists,album,release_year,track_number,channel,uploader,webpage_url,extractor_key}})j"
    )
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
