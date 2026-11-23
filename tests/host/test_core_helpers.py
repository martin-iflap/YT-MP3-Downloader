"""Pure helpers of core and the modules around it: filenames, artist and tags, folders, URL check, error messages."""

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import point_yt_dlp_at
from ytmp3_host import core, files, ytdlp
from ytmp3_host.core import EMBED_TIMEOUT_CAP_S, describe_error
from ytmp3_host.files import downloaded_file, resolve_folder
from ytmp3_host.media_format import Mp3Format, Mp4Format
from ytmp3_host.metadata import artist_for, extra_tags, parse_info, title_filename
from ytmp3_host.naming import (
    DEFAULT_FILENAME,
    MAX_FILENAME_CHARS,
    UNKNOWN_ARTIST,
    artist_from_filename,
    filename_limit,
    filename_too_long,
    is_web_url,
    is_youtube_url,
    requested_filename,
    safe_filename,
    title_from_filename,
)
from ytmp3_host.ytdlp import build_download_command


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Artist - Song (Official Video) [HD]", "Artist - Song"),
        ("Artist - Song (Official Music Video)", "Artist - Song"),
        ("Artist - Song [Official Lyric Video]", "Artist - Song"),
        ("Artist - Song (Lyrics) (Live)", "Artist - Song (Live)"),  # a live version is another recording
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
        ("Artist-Song", UNKNOWN_ARTIST),  # only a spaced dash separates
        ("Anti-Hero", UNKNOWN_ARTIST),
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
        ("Anti-Hero", "Anti-Hero"),
        ("Song without artist", "Song without artist"),
        ("- Song", "- Song"),
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
        ("Anti-Hero", {"channel": "Taylor Swift"}, "Taylor Swift"),
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
        # the text before the dash is not the artist
        ({"title": "Bohemian Rhapsody - Remastered 2011", "artists": ["Queen"]}, "Queen - Bohemian Rhapsody - Remastered 2011"),
        ({"title": "AC/DC - Thunderstruck", "artists": ["AC/DC"]}, "ACDC - Thunderstruck"),
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


@pytest.mark.parametrize("title", ["x" * 300, "😀" * 200], ids=["ascii", "two-unit-characters"])
def test_a_title_too_long_for_windows_is_shortened(title: str) -> None:
    name = title_filename({"title": title})
    assert not filename_too_long(name)
    assert name and set(name) == set(title[0])  # no half character at the cut


def test_filename_limit_counts_like_windows() -> None:
    assert not filename_too_long("x" * MAX_FILENAME_CHARS)
    assert filename_too_long("x" * (MAX_FILENAME_CHARS + 1))
    assert filename_too_long("😀" * (MAX_FILENAME_CHARS // 2 + 1))  # two UTF-16 units each


def test_filename_limit_keeps_the_whole_path_within_max_path() -> None:
    assert filename_limit(Path("C:\\")) == MAX_FILENAME_CHARS
    deep = Path("C:\\" + "d" * 200)
    assert len(str(deep / f"{'x' * filename_limit(deep)}.mp3")) == 259
    assert len(title_filename({"title": "x" * 300}, deep)) == filename_limit(deep)


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


def test_downloaded_file_accepts_only_existing_absolute_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mp3 = tmp_path / "Song.MP3"
    mp3.write_bytes(b"x")
    mp4 = tmp_path / "Video.Mp4"
    mp4.write_bytes(b"x")
    exe = tmp_path / "evil.exe"
    exe.write_bytes(b"x")
    (tmp_path / "Video.mov").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    assert downloaded_file(str(mp3)) == mp3
    assert downloaded_file(str(mp4)) == mp4
    assert downloaded_file(str(tmp_path / "Video.mov")) is None  # only what the host downloads
    assert downloaded_file(str(exe)) is None  # play must never run a program
    assert downloaded_file(str(tmp_path / "gone.mp3")) is None
    assert downloaded_file(str(tmp_path)) is None
    assert downloaded_file("Song.MP3") is None  # relative
    assert downloaded_file("") is None


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
    assert files.folder_is_writable(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_folder_is_writable_probe_is_gone_when_the_host_is_killed(tmp_path: Path) -> None:
    # the host killed while the probe is open: Windows still deletes it (O_TEMPORARY)
    code = (
        "import os, sys, time; from ytmp3_host import files\n"
        "files.os.close = lambda fd: (print('open', flush=True), time.sleep(60))\n"
        "files.folder_is_writable(files.Path(sys.argv[1]))\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "open"
    assert len(list(tmp_path.glob("_ytmp3_*.probe"))) == 1
    # the venv's python.exe is a launcher with the real Python as its child
    ytdlp.kill_process_tree(child)
    child.wait()
    assert list(tmp_path.iterdir()) == []


def test_folder_is_writable_false_when_windows_refuses(tmp_path: Path) -> None:
    everyone_create_files = "*S-1-1-0:(WD)"  # by SID: the group's name depends on the Windows language
    subprocess.run(["icacls", str(tmp_path), "/deny", everyone_create_files], check=True, capture_output=True)
    try:
        assert not files.folder_is_writable(tmp_path)
    finally:  # or pytest could not delete the folder
        subprocess.run(["icacls", str(tmp_path), "/remove:d", "*S-1-1-0"], check=True, capture_output=True)


def test_folder_is_writable_false_for_a_missing_folder(tmp_path: Path) -> None:
    assert not files.folder_is_writable(tmp_path / "missing")


def test_resolve_folder_creates_the_default_when_named(default_folder: Path) -> None:
    assert resolve_folder(str(default_folder)) == default_folder
    assert default_folder.is_dir()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc", True),
        ("https://music.youtube.com/watch?v=abc", True),
        ("https://youtu.be/abc", True),
        ("https://vimeo.com/123", False),
        ("https://example.com/?ref=youtube.com", False),  # only the host counts
        ("https://notyoutube.com/watch?v=abc", False),
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


def test_build_download_command_keeps_quality_and_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # set after ytdlp was imported: the command must look the exe up, an update can change it
    point_yt_dlp_at(monkeypatch, tmp_path / "yt-dlp.exe")
    build = tmp_path / "yt-dlp" / "2026.10.26"
    build.mkdir(parents=True)
    stem = tmp_path / "_ytmp3_x"
    cmd = build_download_command("https://youtu.be/abc", stem, Mp3Format())

    def value(flag: str) -> str:
        return cmd[cmd.index(flag) + 1]

    assert cmd[0] == str(build / "yt-dlp.exe")
    assert cmd[-1] == "https://youtu.be/abc"
    assert value("--audio-format") == "mp3"
    assert value("--audio-quality") == "0"  # LAME V0, see CLAUDE.md priorities
    assert value("--playlist-items") == "1"  # a playlist URL still gives one file
    assert value("-o") == f"{stem}.%(ext)s"
    assert value("--print") == (
        f"before_dl:{ytdlp.INFO_MARKER}%(.{{title,artists,album,release_year,track_number,channel,"
        "uploader,webpage_url,extractor_key,filesize_approx,duration,format_id,vcodec})j"
    )
    assert "--no-simulate" in cmd  # --print must never turn the call into a dry run
    assert value("--ffmpeg-location") == str(ytdlp.FFMPEG_PATH)
    assert {"--no-playlist", "--write-thumbnail", "-x", "--newline", "--progress"} <= set(cmd)


@pytest.mark.parametrize(
    ("media_format", "sort"),
    [
        (Mp4Format(), "hdr:sdr"),  # best: no codec or height preference, the source's best
        (Mp4Format(quality="720"), "res:720,hdr:sdr"),
        (Mp4Format(codec="compatible"), "vcodec:h264,acodec:aac,hdr:sdr"),
        # the codec comes first: an earlier -S key wins, so a lower H.264 beats a higher AV1
        (Mp4Format(quality="480", codec="compatible"), "vcodec:h264,acodec:aac,res:480,hdr:sdr"),
    ],
)
def test_build_download_command_mp4_presets(media_format: Mp4Format, sort: str, tmp_path: Path) -> None:
    cmd = build_download_command("https://youtu.be/abc", tmp_path / "_ytmp3_x", media_format)

    assert cmd[cmd.index("-S") + 1] == sort
    assert cmd[cmd.index("--merge-output-format") + 1] == "mp4"
    assert cmd[cmd.index("--remux-video") + 1] == "mp4"  # a site's single WebM or FLV video
    assert not {"-x", "--audio-format", "--audio-quality"} & set(cmd)  # those would make it an MP3
    assert {"--write-thumbnail", "--no-simulate", "--newline"} <= set(cmd)  # the rest of the contract is shared
    assert cmd[-1] == "https://youtu.be/abc"


def test_mp3_command_has_no_video_sort(tmp_path: Path) -> None:
    cmd = build_download_command("https://youtu.be/abc", tmp_path / "_ytmp3_x", Mp3Format())
    assert not {"-S", "--merge-output-format"} & set(cmd)


def test_a_percent_sign_in_the_folder_is_not_part_of_the_output_template(tmp_path: Path) -> None:
    cmd = build_download_command("https://youtu.be/abc", tmp_path / "%(title)s" / "_ytmp3_x", Mp3Format())
    assert cmd[cmd.index("-o") + 1] == f"{tmp_path}\\%%(title)s\\_ytmp3_x.%(ext)s"


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


class TestDownloadTimeout:
    def test_nofilesize_noduration_is_fallback(self) -> None:
        info = {"title": "Song"}
        assert ytdlp.download_timeout(info, Mp3Format(), 10) == 10

    def test_filesize_uses_min_speed_bps(self) -> None:
        info = {"title": "Song", "filesize_approx": 10_000_000}
        expected = ytdlp.DOWNLOAD_TIMEOUT_BASE_S + 10_000_000 / ytdlp.MIN_SPEED_BPS
        assert ytdlp.download_timeout(info, Mp3Format(), 10) == expected
        assert ytdlp.download_timeout(info, Mp4Format(), 10) == expected

    def test_duration_uses_the_most_data_the_format_can_need(self) -> None:
        info = {"duration": 600}
        base, speed = ytdlp.DOWNLOAD_TIMEOUT_BASE_S, ytdlp.MIN_SPEED_BPS
        assert ytdlp.download_timeout(info, Mp4Format(), 10) == base + 600 * 3_000_000 / speed  # 4K60 HDR
        assert ytdlp.download_timeout(info, Mp3Format(), 10) == base + 600 * 40_000 / speed  # 320 kbit/s
        assert ytdlp.download_timeout(info, Mp4Format(quality="480"), 10) == base + 600 * 3_000_000 / speed

    def test_the_size_wins_over_the_duration(self) -> None:
        info = {"filesize_approx": 10_000_000, "duration": 36_000}
        expected = ytdlp.DOWNLOAD_TIMEOUT_BASE_S + 10_000_000 / ytdlp.MIN_SPEED_BPS
        assert ytdlp.download_timeout(info, Mp4Format(), 10) == expected

    def test_a_small_download_keeps_the_fallback(self) -> None:
        # what every MP3 is: a few MB of audio must not shorten the 900 s it had before
        info = {"filesize_approx": 5_000_000, "duration": 240}
        assert ytdlp.download_timeout(info, Mp3Format(), 900) == 900

    def test_is_capped(self) -> None:
        info = {"filesize_approx": 100_000_000_000}
        assert ytdlp.download_timeout(info, Mp4Format(), 10) == ytdlp.DOWNLOAD_TIMEOUT_CAP_S

    def test_the_cap_wins_over_the_fallback(self) -> None:
        # the stale temp file cleanup relies on no download running longer than the cap
        above = ytdlp.DOWNLOAD_TIMEOUT_CAP_S + 1
        cap = ytdlp.DOWNLOAD_TIMEOUT_CAP_S
        assert ytdlp.download_timeout({"filesize_approx": 5_000_000}, Mp4Format(), above) == cap
        assert ytdlp.download_timeout({}, Mp4Format(), above) == cap

    @pytest.mark.parametrize("junk", ["NA", "123", 0, -5, None, [1], {"a": 1}])
    def test_junk_values_count_as_missing(self, junk: object) -> None:
        assert ytdlp.download_timeout({"filesize_approx": junk}, Mp4Format(), 10) == 10
        # junk size falls through to the duration
        info = {"filesize_approx": junk, "duration": 60}
        expected = ytdlp.DOWNLOAD_TIMEOUT_BASE_S + 60 * 3_000_000 / ytdlp.MIN_SPEED_BPS
        assert ytdlp.download_timeout(info, Mp4Format(), 10) == expected
        assert ytdlp.download_timeout({"duration": junk}, Mp4Format(), 10) == 10


def test_stale_temp_age_is_longer_than_download_plus_embed() -> None:
    assert files.STALE_TEMP_AGE_S > ytdlp.DOWNLOAD_TIMEOUT_CAP_S + EMBED_TIMEOUT_CAP_S


def test_the_fallback_download_timeout_is_below_the_cap() -> None:
    # download_timeout caps it anyway, but a fallback above the cap would be silently shortened
    assert ytdlp.DOWNLOAD_TIMEOUT_S <= ytdlp.DOWNLOAD_TIMEOUT_CAP_S


def downloading(downloaded: int, **sizes: float) -> dict[str, object]:
    return {"status": "downloading", "downloaded_bytes": downloaded, **sizes}


def finished(size: int) -> dict[str, object]:
    return {"status": "finished", "downloaded_bytes": size, "total_bytes": size}


class TestProgressTracker:
    """The edge cases the fake yt-dlp doesn't print; test_download_audio covers the usual ones."""

    def test_an_hls_download_counts_against_its_growing_estimate(self) -> None:
        # fragment downloads only estimate their size, and the estimate grows with each fragment
        tracker = ytdlp.ProgressTracker()
        assert tracker.update(downloading(250, total_bytes_estimate=1000.0)) == ytdlp.Progress("downloading", 0.25)
        assert tracker.update(downloading(600, total_bytes_estimate=2000.0)) == ytdlp.Progress("downloading", 0.3)
        assert tracker.update(finished(2100)) == ytdlp.Progress("downloading", 1.0)

    def test_an_estimate_that_shrinks_never_moves_the_fraction_back(self) -> None:
        tracker = ytdlp.ProgressTracker()
        tracker.update(downloading(500, total_bytes=1000))
        assert tracker.update(downloading(600, total_bytes_estimate=4000.0)) is None  # 0.15 would go back

    def test_an_unknown_size_reports_downloading_without_a_fraction(self) -> None:
        tracker = ytdlp.ProgressTracker()
        assert tracker.update(downloading(500)) == ytdlp.Progress("downloading")
        assert tracker.update(downloading(900)) is None  # the same for the user

    def test_the_stream_count_and_size_come_from_the_info(self) -> None:
        tracker = ytdlp.ProgressTracker()
        tracker.use_info({"format_id": "399+251", "filesize_approx": 4000})
        assert (tracker.streams, tracker.expected_bytes) == (2, 4000.0)
        tracker.use_info({"format_id": 251})  # not text: one stream, as without a format_id
        assert (tracker.streams, tracker.expected_bytes) == (1, None)

    def test_the_expected_size_counts_streams_not_started_yet(self) -> None:
        tracker = ytdlp.ProgressTracker()
        tracker.use_info({"format_id": "399+251", "filesize_approx": 4000})
        assert tracker.update(finished(3000)) == ytdlp.Progress("downloading", 0.75)

    def test_an_expected_size_too_small_still_leaves_room_for_later_streams(self) -> None:
        # the video alone is above filesize_approx: the bar must not be full before the audio
        tracker = ytdlp.ProgressTracker()
        tracker.use_info({"format_id": "399+251", "filesize_approx": 3000})
        assert tracker.update(finished(3000)) == ytdlp.Progress("downloading", 0.9)
        assert tracker.update(downloading(500, total_bytes=1000)) is None  # 87.5 % would go back
        assert tracker.update(downloading(900, total_bytes=1000)) == ytdlp.Progress("downloading", 0.975)
        assert tracker.update(finished(1000)) == ytdlp.Progress("downloading", 1.0)

    def test_the_last_streams_own_size_beats_the_expected_one(self) -> None:
        # filesize_approx is an estimate; once every size is known the download ends at 100 %
        tracker = ytdlp.ProgressTracker()
        tracker.expected_bytes = 1000
        assert tracker.update(downloading(1000, total_bytes=2000)) == ytdlp.Progress("downloading", 0.5)
        tracker.expected_bytes = 4000
        assert tracker.update(downloading(1500, total_bytes=2000)) == ytdlp.Progress("downloading", 0.75)

    def test_a_last_stream_of_unknown_size_never_shows_100_before_it_ends(self) -> None:
        tracker = ytdlp.ProgressTracker()
        tracker.use_info({"format_id": "399+251"})
        tracker.update(finished(3000))
        assert tracker.update(downloading(500)) is None  # still 0.9
        assert tracker.update(finished(1000)) == ytdlp.Progress("downloading", 1.0)

    def test_processing_is_reported_once(self) -> None:
        tracker = ytdlp.ProgressTracker()
        tracker.update(finished(1000))
        assert tracker.update({"status": "started", "postprocessor": "ExtractAudio"}) == ytdlp.Progress("processing")
        assert tracker.update({"status": "finished", "postprocessor": "ExtractAudio"}) is None
        assert tracker.update({"status": "started", "postprocessor": "MoveFiles"}) is None

    @pytest.mark.parametrize("event", [{"status": "error", "downloaded_bytes": 10, "total_bytes": 100}, {}])
    def test_errors_and_unknown_events_report_nothing(self, event: dict[str, object]) -> None:
        assert ytdlp.ProgressTracker().update(event) is None

    @pytest.mark.parametrize("payload", ["not json", "[1, 2]", ""])
    def test_a_broken_progress_line_is_an_empty_event(self, payload: str) -> None:
        assert ytdlp._progress_event(ytdlp.PROGRESS_MARKER + payload) == {}


def file_of_size(path: Path, size: int) -> Path:
    """A file of size bytes, without writing them (NTFS extends it sparsely)."""
    with path.open("wb") as f:
        f.truncate(size)
    return path


class TestEmbedThumbTimeout:
    def test_an_empty_file_gets_the_base(self, tmp_path: Path) -> None:
        assert core.embed_thumb_timeout(file_of_size(tmp_path / "a.mp3", 0)) == core.EMBED_TIMEOUT_S

    def test_scales_with_the_size(self, tmp_path: Path) -> None:
        file = file_of_size(tmp_path / "a.mp4", 200_000_000)
        assert core.embed_thumb_timeout(file) == core.EMBED_TIMEOUT_S + 200_000_000 / core.EMBED_MIN_SPEED_BPS

    def test_is_capped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # a slow speed instead of a file of tens of GB
        monkeypatch.setattr(core, "EMBED_MIN_SPEED_BPS", 1)
        file = file_of_size(tmp_path / "a.mp4", core.EMBED_TIMEOUT_CAP_S + 1)
        assert core.embed_thumb_timeout(file) == core.EMBED_TIMEOUT_CAP_S

    def test_a_missing_file_gets_the_base(self, tmp_path: Path) -> None:
        assert core.embed_thumb_timeout(tmp_path / "gone.mp4") == core.EMBED_TIMEOUT_S

    def test_embed_thumbnail_runs_ffmpeg_with_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        temp_file = file_of_size(tmp_path / "_ytmp3_x.mp4", 200_000_000)
        timeouts: list[float] = []

        def _run(*_args: object, timeout: float, **_kwargs: object) -> None:
            timeouts.append(timeout)

        monkeypatch.setattr(core.subprocess, "run", _run)
        core.embed_thumbnail(temp_file, None, "Artist", "Song", tmp_path / "Artist - Song.mp4")
        assert timeouts == [core.embed_thumb_timeout(temp_file)]
        assert timeouts[0] > core.EMBED_TIMEOUT_S
