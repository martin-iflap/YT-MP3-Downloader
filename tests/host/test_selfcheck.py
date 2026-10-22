"""selfcheck: what the installer runs on a new host version before switching Firefox to it."""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from ytmp3_host import native_messaging, selfcheck

type UseFfmpeg = Callable[[str], None]


@pytest.fixture
def ffmpeg(monkeypatch: pytest.MonkeyPatch) -> UseFfmpeg:
    """Point the ffmpeg check at a Python one-liner (a working one by default); call it with other code."""
    def use(code: str) -> None:
        monkeypatch.setattr(selfcheck, "ffmpeg_version_command", lambda: [sys.executable, "-c", code])
    use("pass")
    return use


def test_a_working_version_passes(ffmpeg: UseFfmpeg, capsys: pytest.CaptureFixture[str]) -> None:
    assert selfcheck.self_check() == 0
    assert capsys.readouterr() == ("", "")


def test_a_broken_ffmpeg_fails(ffmpeg: UseFfmpeg, capsys: pytest.CaptureFixture[str]) -> None:
    ffmpeg("raise SystemExit(1)")
    assert selfcheck.self_check() == 1
    assert "does not run" in capsys.readouterr().err


def test_a_missing_ffmpeg_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(selfcheck, "ffmpeg_version_command", lambda: [str(tmp_path / "ffmpeg.exe"), "-version"])
    assert selfcheck.self_check() == 1
    assert "ffmpeg.exe does not run" in capsys.readouterr().err


def test_a_module_missing_from_the_build_fails(
    ffmpeg: UseFfmpeg, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(selfcheck, "MODULES", (*selfcheck.MODULES, "ytmp3_host.not_in_the_build"))
    assert selfcheck.self_check() == 1
    assert "Could not import ytmp3_host.not_in_the_build" in capsys.readouterr().err


def test_every_host_module_is_checked() -> None:
    package = Path(selfcheck.__file__).parent
    modules = {f"ytmp3_host.{p.stem}" for p in package.glob("*.py")} - {"ytmp3_host.__init__"}
    # MP3 runs the check; config and selfcheck are imported by then
    assert modules - set(selfcheck.MODULES) == {"ytmp3_host.MP3", "ytmp3_host.config", "ytmp3_host.selfcheck"}


def test_the_framing_check_leaves_stdio_as_it_was(ffmpeg: UseFfmpeg) -> None:
    stdin, stdout = sys.stdin, sys.stdout
    assert selfcheck.self_check() == 0
    assert (sys.stdin, sys.stdout) == (stdin, stdout)


def test_garbled_framing_fails(ffmpeg: UseFfmpeg, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def send_empty_object(message: native_messaging.Message, exit_code: int = 1) -> None:
        sys.stdout.buffer.write(native_messaging.LENGTH_PREFIX.pack(2) + b"{}")
    monkeypatch.setattr(native_messaging, "send_message", send_empty_object)
    assert selfcheck.self_check() == 1
    assert "Native messaging garbled" in capsys.readouterr().err
