"""selfcheck: what the installer runs on a new host version before switching Firefox to it."""

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from ytmp3_host import __version__, core, selfcheck
from ytmp3_host.native_messaging import LENGTH_PREFIX

type UseHost = Callable[[str], None]


def framed(message: dict[str, object]) -> bytes:
    data = json.dumps(message).encode()
    return LENGTH_PREFIX.pack(len(data)) + data


PONG = framed({"status": "success", "version": __version__, "final": True})


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> UseHost:
    """Make the request check start a Python one-liner instead of the host (one that answers
    the ping correctly by default), and skip the ffmpeg check; call it with other code."""
    def use(code: str) -> None:
        monkeypatch.setattr(selfcheck, "host_command", lambda: [sys.executable, "-c", code])
    use(f"import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write({PONG!r})")
    monkeypatch.setattr(selfcheck, "_check_ffmpeg", lambda: [])
    return use


def test_a_working_version_passes(host: UseHost, capsys: pytest.CaptureFixture[str]) -> None:
    assert selfcheck.self_check() == 0
    assert capsys.readouterr() == ("", "")


# --- the request round trip ---------------------------------------------------


def test_the_real_host_answers_a_ping() -> None:
    # unfrozen, host_command() is `python -m ytmp3_host.MP3`: the real request loop in a new process
    assert selfcheck._check_request() == []


@pytest.mark.parametrize("code", [
    "raise SystemExit(1)",
    "import sys; sys.stdin.buffer.read()",  # exits without answering
], ids=["crashes", "silent"])
def test_a_host_that_does_not_answer_fails(host: UseHost, code: str, capsys: pytest.CaptureFixture[str]) -> None:
    host(code)
    assert selfcheck.self_check() == 1
    assert "The host did not answer a request" in capsys.readouterr().err


@pytest.mark.parametrize("answer", [
    framed({"status": "success", "version": "0.0.1", "final": True}),  # another version answered
    PONG[:-1],  # cut short
    PONG + PONG,  # more than one answer
    LENGTH_PREFIX.pack(2) + b"{}",
], ids=["other-version", "truncated", "two-answers", "empty-object"])
def test_a_wrong_answer_fails(host: UseHost, answer: bytes, capsys: pytest.CaptureFixture[str]) -> None:
    host(f"import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write({answer!r})")
    assert selfcheck.self_check() == 1
    assert "The host did not answer a request" in capsys.readouterr().err


def test_a_host_that_can_not_start_fails(host: UseHost, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(selfcheck, "host_command", lambda: [str(tmp_path / "mp3.exe")])
    assert selfcheck.self_check() == 1
    assert "The host does not start" in capsys.readouterr().err


# --- the ffmpeg steps -----------------------------------------------------------


def test_the_ffmpeg_steps_of_a_download_work(ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "FFMPEG_PATH", ffmpeg)
    assert selfcheck._check_ffmpeg() == []


def test_a_missing_ffmpeg_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(core, "FFMPEG_PATH", tmp_path / "ffmpeg.exe")
    [problem] = selfcheck._check_ffmpeg()
    assert "ffmpeg.exe does not work" in problem


def test_a_broken_ffmpeg_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "FFMPEG_PATH", Path(sys.executable))  # runs, but fails on ffmpeg's arguments
    [problem] = selfcheck._check_ffmpeg()
    assert "does not work" in problem


def test_an_ffmpeg_that_writes_no_tags_fails(ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "FFMPEG_PATH", ffmpeg)
    monkeypatch.setattr(core, "embed_thumbnail", lambda temp_mp3, thumb, artist, title, output: output.write_bytes(temp_mp3.read_bytes()))
    [problem] = selfcheck._check_ffmpeg()
    assert "wrote a wrong MP3" in problem


def test_the_ffmpeg_check_leaves_no_temp_files(ffmpeg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(core, "FFMPEG_PATH", ffmpeg)
    monkeypatch.setattr(selfcheck.tempfile, "tempdir", str(tmp_path))
    selfcheck._check_ffmpeg()
    assert list(tmp_path.iterdir()) == []


# --- the build -------------------------------------------------------------------


def test_a_module_missing_from_the_build_fails(
    host: UseHost, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(selfcheck, "MODULES", (*selfcheck.MODULES, "ytmp3_host.not_in_the_build"))
    assert selfcheck.self_check() == 1
    assert "Could not import ytmp3_host.not_in_the_build" in capsys.readouterr().err


def test_a_build_without_https_fails(
    host: UseHost, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ssl

    def _broken() -> None:
        raise ssl.SSLError("no certificates")
    monkeypatch.setattr(ssl, "create_default_context", _broken)
    assert selfcheck.self_check() == 1
    assert "HTTPS does not work" in capsys.readouterr().err


def test_every_host_module_is_checked() -> None:
    package = Path(selfcheck.__file__).parent
    modules = {f"ytmp3_host.{p.stem}" for p in package.glob("*.py")} - {"ytmp3_host.__init__"}
    # MP3 runs the check; config and selfcheck are imported by then
    assert modules - set(selfcheck.MODULES) == {"ytmp3_host.MP3", "ytmp3_host.config", "ytmp3_host.selfcheck"}
