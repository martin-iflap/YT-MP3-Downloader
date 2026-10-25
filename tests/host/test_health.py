"""health: the facts the get_version action reports (host and yt-dlp versions, ffmpeg, JS runtime).

yt-dlp is fake_yt_dlp.py (see the fake_yt_dlp fixture); the GitHub lookup is
replaced per test, so nothing here touches the network.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import FakeYtDlp
from ytmp3_host import __version__, health
from ytmp3_host.github import HOST_REPO, YT_DLP_REPO


# --- versions -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.0.0", (2, 0, 0)),
        ("v2.0.0", (2, 0, 0)),
        (" 2026.08.19\n", (2026, 8, 19)),
        ("2026.08.19.123456", (2026, 8, 19, 123456)),  # yt-dlp nightly
        ("7", (7,)),
        ("", None),
        ("v", None),
        ("2.0.", None),
        ("2.0.0-beta", None),
        ("v-1.1", None),  # the 1.x release tag style
        ("[debug] 2.0", None),
        (None, None),
        (2, None),
    ],
)
def test_parse_version(text: object, expected: tuple[int, ...] | None) -> None:
    assert health.parse_version(text) == expected


@pytest.mark.parametrize(
    ("version", "latest", "expected"),
    [
        ("2026.08.18", "2026.08.19", True),
        ("2026.08.19", "2026.08.19", False),
        ("2026.08.19.123456", "2026.08.19", False),  # nightly of the same day
        ("2026.10.01", "2026.9.30", False),  # numeric, not text order
        ("2.0", "2.0.0", False),
        ("2.0.0", "2.0", False),
        ("1.9.9", "2.0", True),
        ("1.2.0", "2.0.0", True),  # the first part decides; padding never shifts parts
        ("2.0.0", "1.2.0", False),
        ("1.10", "1.9", False),
        ("2", "2.0.1", True),  # (2, 0, 0) < (2, 0, 1)
        (None, "2026.08.19", None),
        ("2026.08.19", None, None),
        ("broken", "2026.08.19", None),
    ],
)
def test_is_outdated(version: str | None, latest: str | None, expected: bool | None) -> None:
    assert health.is_outdated(version, latest) is expected


# --- read_version -------------------------------------------------------------


def test_read_version_returns_the_printed_version(fake_yt_dlp: FakeYtDlp) -> None:
    fake_yt_dlp.set(version="2026.07.01")
    assert health.read_version(health.yt_dlp_version_command()) == "2026.07.01"


@pytest.mark.parametrize("mode", ["fail", "bad_version"])
def test_read_version_of_a_broken_tool_is_none(fake_yt_dlp: FakeYtDlp, mode: str) -> None:
    fake_yt_dlp.set(mode)
    assert health.read_version(health.yt_dlp_version_command()) is None


def test_read_version_kills_a_hanging_tool(fake_yt_dlp: FakeYtDlp) -> None:
    fake_yt_dlp.set("hang")
    start = time.monotonic()
    assert health.read_version(health.yt_dlp_version_command(), timeout=1) is None
    # the fake and the child holding its stdout sleep 30 s; only killing the tree returns early
    assert time.monotonic() - start < 10


def test_read_version_of_a_truncated_download_is_none(tmp_path: Path) -> None:
    # not a valid executable: Windows refuses to start it (OSError), the host must not crash
    exe = tmp_path / "yt-dlp.exe"
    exe.write_bytes(b"MZ half a download")
    assert health.read_version([str(exe), "--version"]) is None


def test_read_version_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert health.read_version([str(tmp_path / "missing.exe"), "--version"]) is None


# --- host_status --------------------------------------------------------------


@dataclass
class Latest:
    """The latest release tag per repository that the fake lookup answers, and the repos it was asked for."""
    tags: dict[str, str | None]
    looked_up: list[str] = field(default_factory=list)


@pytest.fixture
def latest(monkeypatch: pytest.MonkeyPatch) -> Latest:
    """Make the latest releases: host v{__version__} (this one), yt-dlp 2026.08.19. Tests change latest.tags."""
    state = Latest({HOST_REPO: f"v{__version__}", YT_DLP_REPO: "2026.08.19"})

    def _lookup(repo: str) -> str | None:
        state.looked_up.append(repo)
        return state.tags[repo]

    monkeypatch.setattr(health, "latest_release_tag", _lookup)
    return state


@pytest.fixture(autouse=True)
def no_deno_on_path(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Hide a deno installed on this machine's PATH (autouse); returns the names looked up."""
    looked_up: list[str] = []
    monkeypatch.setattr(health.shutil, "which", lambda name: looked_up.append(name))
    return looked_up


def test_everything_installed_and_current(fake_yt_dlp: FakeYtDlp, latest: Latest) -> None:
    fake_yt_dlp.set(version="2026.08.19")
    assert health.host_status() == {
        "version": __version__,
        "latest": f"v{__version__}",
        "outdated": False,
        "self_update": False,  # only an installed host updates itself, never a source checkout
        "yt_dlp": {"installed": True, "version": "2026.08.19", "latest": "2026.08.19", "outdated": False},
        "ffmpeg": {"installed": True},
        "js_runtime": {"name": "deno", "installed": False},
    }
    assert sorted(latest.looked_up) == sorted([HOST_REPO, YT_DLP_REPO])


def test_newer_host_release_is_reported(fake_yt_dlp: FakeYtDlp, latest: Latest) -> None:
    latest.tags[HOST_REPO] = "v99.0.0"
    status = health.host_status()
    assert (status["version"], status["latest"], status["outdated"]) == (__version__, "v99.0.0", True)


@pytest.mark.parametrize("tag", [None, "v-1.1"], ids=["offline", "1.x-style-tag"])
def test_unknown_host_release_is_not_outdated(fake_yt_dlp: FakeYtDlp, latest: Latest, tag: str | None) -> None:
    latest.tags[HOST_REPO] = tag
    status = health.host_status()
    assert (status["latest"], status["outdated"]) == (tag, None)


def test_an_installed_host_can_update_itself(fake_yt_dlp: FakeYtDlp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "INSTALLED", True)
    assert health.host_status()["self_update"] is True


def test_outdated_yt_dlp(fake_yt_dlp: FakeYtDlp, latest: Latest) -> None:
    fake_yt_dlp.set(version="2026.07.01")
    assert health.host_status()["yt_dlp"] == {
        "installed": True, "version": "2026.07.01", "latest": "2026.08.19", "outdated": True,
    }


def test_offline_reports_the_installed_version_and_unknown_outdated(fake_yt_dlp: FakeYtDlp) -> None:
    # no_github (conftest) answers None, like an unreachable GitHub
    assert health.host_status()["yt_dlp"] == {"installed": True, "version": "2026.08.19", "latest": None, "outdated": None}


def test_broken_yt_dlp_is_installed_without_a_version(fake_yt_dlp: FakeYtDlp, latest: Latest) -> None:
    fake_yt_dlp.set("fail")
    assert health.host_status()["yt_dlp"] == {"installed": True, "version": None, "latest": "2026.08.19", "outdated": None}


def test_missing_yt_dlp_is_not_run(
    fake_yt_dlp: FakeYtDlp, latest: Latest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "bin" / "yt-dlp.exe").unlink()

    def _must_not_run(command: list[str], timeout: float = 0) -> str:
        raise AssertionError("ran a missing yt-dlp.exe")

    monkeypatch.setattr(health, "read_version", _must_not_run)
    status = health.host_status()
    assert status["yt_dlp"] == {"installed": False, "version": None, "latest": "2026.08.19", "outdated": None}
    assert status["ffmpeg"] == {"installed": True}


def test_missing_ffmpeg(fake_yt_dlp: FakeYtDlp, tmp_path: Path) -> None:
    (tmp_path / "bin" / "ffmpeg.exe").unlink()
    assert health.host_status()["ffmpeg"] == {"installed": False}


def test_bundled_js_runtime_is_found(fake_yt_dlp: FakeYtDlp, tmp_path: Path) -> None:
    (tmp_path / "bin" / "deno.exe").write_bytes(b"")
    assert health.find_js_runtime() == tmp_path / "bin" / "deno.exe"
    assert health.host_status()["js_runtime"] == {"name": "deno", "installed": True}


def test_js_runtime_on_path_is_found(fake_yt_dlp: FakeYtDlp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    deno = tmp_path / "elsewhere" / "deno.exe"
    monkeypatch.setattr(health.shutil, "which", lambda name: str(deno) if name == "deno" else None)
    assert health.find_js_runtime() == deno


def test_no_js_runtime(fake_yt_dlp: FakeYtDlp, no_deno_on_path: list[str]) -> None:
    assert health.find_js_runtime() is None
    assert no_deno_on_path == ["deno"]


def test_a_bug_in_the_lookup_surfaces(fake_yt_dlp: FakeYtDlp, monkeypatch: pytest.MonkeyPatch) -> None:
    # latest_release_tag never raises for network problems; a bug in it must surface (main()
    # reports it as the final error), not be swallowed or hang the thread pool
    def _broken(repo: str) -> str:
        raise RuntimeError("bug")

    monkeypatch.setattr(health, "latest_release_tag", _broken)
    with pytest.raises(RuntimeError, match="bug"):
        health.host_status()
