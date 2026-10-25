"""The native-messaging contract, driven through MP3.main() with real framed bytes.

The extension relies on: exactly one `final: true` response per request, warnings
with `final: false`, and nothing but valid frames on stdout.
"""

import functools
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import EmbedCall, FakeYtDlp
from ytmp3_host import MP3, __version__, config, core, health, native_messaging, updater
from ytmp3_host.native_messaging import LENGTH_PREFIX

URL = "https://www.youtube.com/watch?v=abc123"


def frame(message: object) -> bytes:
    """Encode message the way Firefox sends it: 4-byte native-endian length, then UTF-8 JSON."""
    data = json.dumps(message).encode("utf-8")
    return LENGTH_PREFIX.pack(len(data)) + data


def parse_frames(data: bytes) -> list[dict[str, Any]]:
    """Decode stdout; fails if it holds anything but complete frames."""
    messages = []
    while data:
        assert len(data) >= LENGTH_PREFIX.size, f"trailing bytes on stdout: {data!r}"
        (length,) = LENGTH_PREFIX.unpack(data[: LENGTH_PREFIX.size])
        body, data = data[LENGTH_PREFIX.size : LENGTH_PREFIX.size + length], data[LENGTH_PREFIX.size + length :]
        assert len(body) == length, "truncated frame on stdout"
        messages.append(json.loads(body.decode("utf-8")))
    return messages


def run_host(monkeypatch: pytest.MonkeyPatch, stdin: bytes) -> list[dict[str, Any]]:
    """Run MP3.main() in this process until stdin is exhausted and return the decoded responses.

    sys.stdin/sys.stdout are replaced with in-memory text streams over bytes, which is
    what read_message/send_message use (through .buffer). End of stdin is what Firefox
    does when it closes the port, so main() must exit with code 0.
    """
    stdout = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
    fake_stdout = io.TextIOWrapper(stdout, write_through=True)
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    with pytest.raises(SystemExit) as info:
        MP3.main()
    fake_stdout.flush()  # catch a stray print() as well
    assert info.value.code == 0
    return parse_frames(stdout.getvalue())


def requests(*messages: object) -> bytes:
    """Several framed requests back to back, as one stdin."""
    return b"".join(frame(m) for m in messages)


def finals(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in responses if r["final"]]


def assert_one_final_per_request(responses: list[dict[str, Any]], request_count: int) -> None:
    """The contract background.js relies on: one final per request, last message final, warnings never final."""
    assert len(finals(responses)) == request_count
    assert responses[-1]["final"] is True
    assert all(r["status"] in {"success", "error", "warning"} for r in responses)
    assert all(r["final"] is False for r in responses if r["status"] == "warning")


# --- framing ------------------------------------------------------------------


def test_round_trip_of_unicode_message(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(stdout))
    native_messaging.send_message({"message": "日本 – ok", "final": True})
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdout.getvalue())))
    assert native_messaging.read_message() == {"message": "日本 – ok", "final": True}


@pytest.mark.parametrize("stdin", [b"", b"\x05\x00", frame({"a": 1})[:-2]], ids=["empty", "half-prefix", "half-body"])
def test_closed_stdin_exits_cleanly(monkeypatch: pytest.MonkeyPatch, stdin: bytes) -> None:
    assert run_host(monkeypatch, stdin) == []


def test_unsendable_message_exits_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO()))
    with pytest.raises(SystemExit) as info:
        native_messaging.send_message({"not json": object()})
    assert info.value.code == 1


# --- dispatch and protocol errors ---------------------------------------------


def test_invalid_json_is_reported_and_the_host_keeps_serving(monkeypatch: pytest.MonkeyPatch, default_folder: Path) -> None:
    bad = b"{not json"
    stdin = LENGTH_PREFIX.pack(len(bad)) + bad + frame({"action": "get_current_folder"})
    responses = run_host(monkeypatch, stdin)
    assert [r["status"] for r in responses] == ["error", "success"]
    assert "Native messaging error" in responses[0]["message"]
    assert responses[1]["folder"] == str(default_folder)


@pytest.mark.parametrize("message", [[1, 2], "text", 3, None])
def test_non_object_message_is_an_error(monkeypatch: pytest.MonkeyPatch, message: object) -> None:
    [response] = run_host(monkeypatch, frame(message))
    assert response["status"] == "error" and response["final"] is True


def test_invalid_utf8_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = LENGTH_PREFIX.pack(2) + b"\xff\xfe"
    [response] = run_host(monkeypatch, stdin)
    assert response["status"] == "error"


@pytest.mark.parametrize("action", [None, "no_such_action", 5])
def test_missing_or_unknown_action_falls_back_to_download(monkeypatch: pytest.MonkeyPatch, action: object) -> None:
    request: dict[str, object] = {"url": "not a url"}
    if action is not None:
        request["action"] = action
    [response] = run_host(monkeypatch, frame(request))
    assert response == {"status": "error", "message": "Invalid URL", "final": True}


def test_raising_handler_still_sends_one_final(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(_request: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setitem(MP3.ACTIONS, "get_current_folder", _broken)
    responses = run_host(monkeypatch, requests({"action": "get_current_folder"}, {"action": "download", "url": ""}))
    assert_one_final_per_request(responses, 2)
    assert responses[0]["message"] == "Error processing action 'get_current_folder': boom"


def test_get_current_folder_creates_the_default(monkeypatch: pytest.MonkeyPatch, default_folder: Path) -> None:
    [response] = run_host(monkeypatch, frame({"action": "get_current_folder"}))
    assert response == {"status": "success", "folder": str(default_folder), "final": True}
    assert default_folder.is_dir()


# --- show_in_folder and play --------------------------------------------------


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    """Record show_in_folder/open_file calls instead of opening Explorer or a player."""
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(native_messaging, "show_in_folder", lambda file: calls.append(("show", file)))
    monkeypatch.setattr(native_messaging, "open_file", lambda file: calls.append(("play", file)))
    return calls


def test_show_in_folder_and_play_open_the_downloaded_mp3(
    monkeypatch: pytest.MonkeyPatch, opened: list[tuple[str, Path]], tmp_path: Path
) -> None:
    mp3 = tmp_path / "Artist, Name - Song.mp3"
    mp3.write_bytes(b"x")
    responses = run_host(monkeypatch, requests(
        {"action": "show_in_folder", "path": str(mp3)}, {"action": "play", "path": str(mp3)}
    ))
    assert responses == [{"status": "success", "final": True}] * 2
    assert opened == [("show", mp3), ("play", mp3)]


@pytest.mark.parametrize("name", ["gone.mp3", "program.exe", None])
def test_play_refuses_anything_but_an_existing_mp3(
    monkeypatch: pytest.MonkeyPatch, opened: list[tuple[str, Path]], tmp_path: Path, name: str | None
) -> None:
    (tmp_path / "program.exe").write_bytes(b"x")
    request: dict[str, object] = {"action": "play"}
    if name:
        request["path"] = str(tmp_path / name)
    [response] = run_host(monkeypatch, frame(request))
    assert response == {"status": "error", "message": "The MP3 was moved or deleted", "final": True}
    assert opened == []


def test_open_failure_is_one_final_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mp3 = tmp_path / "Song.mp3"
    mp3.write_bytes(b"x")

    def _fail(file: Path) -> None:
        raise OSError(0, "Explorer could not open the folder", str(file.parent))

    monkeypatch.setattr(native_messaging, "show_in_folder", _fail)
    [response] = run_host(monkeypatch, frame({"action": "show_in_folder", "path": str(mp3)}))
    assert response["status"] == "error" and response["final"] is True
    assert "Explorer could not open the folder" in response["message"]


def test_play_opens_through_explorer_outside_the_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    launched: list[object] = []
    monkeypatch.setattr(updater, "launch_detached", launched.append)
    mp3 = tmp_path / "A, B & C - 100% Song.mp3"
    core.open_file(mp3)
    # one command line, path quoted: explorer.exe splits an unquoted path at commas
    assert launched == [f'explorer.exe "{mp3}"']


# --- get_version --------------------------------------------------------------


def test_get_version_is_one_final_with_the_host_and_tool_state(
    monkeypatch: pytest.MonkeyPatch, fake_yt_dlp: FakeYtDlp
) -> None:
    monkeypatch.setattr(health.shutil, "which", lambda name: None)
    responses = run_host(monkeypatch, requests({"action": "get_version"}, {"action": "download", "url": "bad"}))
    assert_one_final_per_request(responses, 2)
    assert responses[0] == {
        "status": "success",
        "version": __version__,
        # the latest releases are unknown: no_github (conftest) keeps tests off the network
        "latest": None,
        "outdated": None,
        "self_update": False,
        "yt_dlp": {"installed": True, "version": "2026.08.19", "latest": None, "outdated": None},
        "ffmpeg": {"installed": True},
        "js_runtime": {"name": "deno", "installed": False},
        "final": True,
    }


def test_get_version_with_a_broken_yt_dlp_still_succeeds(monkeypatch: pytest.MonkeyPatch, fake_yt_dlp: FakeYtDlp) -> None:
    # the report itself works; telling the user is the extension's job
    fake_yt_dlp.set("fail")
    [response] = run_host(monkeypatch, frame({"action": "get_version"}))
    assert response["status"] == "success"
    assert response["yt_dlp"]["version"] is None


# --- update_yt_dlp / update_host ----------------------------------------------


# MP3 binds the updater functions under its own names (a lazy import), so they are patched there
UPDATE_FUNCTIONS = {"update_yt_dlp": "install_yt_dlp", "update_host": "install_host_update"}


@pytest.mark.parametrize(
    ("action", "result", "response"),
    [
        ("update_yt_dlp", "2026.09.01", {"yt_dlp": {"version": "2026.09.01"}}),
        ("update_host", "v2.1.0", {"version": "v2.1.0"}),
        ("update_host", None, {"version": None}),
    ],
    ids=["yt-dlp", "host", "host-already-latest"],
)
def test_update_actions_report_what_they_installed(
    monkeypatch: pytest.MonkeyPatch, action: str, result: str | None, response: dict[str, object]
) -> None:
    monkeypatch.setattr(MP3, UPDATE_FUNCTIONS[action], lambda: result)
    [answer] = run_host(monkeypatch, frame({"action": action}))
    assert answer == {"status": "success", **response, "final": True}


@pytest.mark.parametrize("action", ["update_yt_dlp", "update_host"])
def test_a_failed_update_is_one_final_error_with_its_reason(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    def _fail() -> None:
        raise updater.UpdateError("GitHub could not be reached")

    monkeypatch.setattr(MP3, UPDATE_FUNCTIONS[action], _fail)
    responses = run_host(monkeypatch, requests({"action": action}, {"action": "get_current_folder"}))
    assert_one_final_per_request(responses, 2)
    assert responses[0] == {"status": "error", "message": "GitHub could not be reached", "final": True}


# --- headless download --------------------------------------------------------


@pytest.mark.parametrize("url", ["", "not a url", "--exec=calc https://youtu.be/x", "file:///C:/x.mp3", 5])
def test_invalid_url(monkeypatch: pytest.MonkeyPatch, url: object) -> None:
    [response] = run_host(monkeypatch, frame({"action": "download", "url": url}))
    assert response == {"status": "error", "message": "Invalid URL", "final": True}


def test_successful_download(
    monkeypatch: pytest.MonkeyPatch, fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set(title="Artist - Song (Official Video)")
    [response] = run_host(monkeypatch, frame({"action": "download", "url": URL, "folder": str(out_dir), "filename": ""}))
    assert response == {
        "status": "success",
        "filename": "Artist - Song",
        "path": str(out_dir.resolve() / "Artist - Song.mp3"),
        "final": True,
    }


def test_invalid_folder_warns_then_downloads_to_default(
    monkeypatch: pytest.MonkeyPatch, fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], default_folder: Path, tmp_path: Path
) -> None:
    responses = run_host(
        monkeypatch, frame({"action": "download", "url": URL, "folder": str(tmp_path / "nope"), "filename": "Name"})
    )
    assert_one_final_per_request(responses, 1)
    assert responses[0] == {"status": "warning", "message": "Invalid folder path, using default", "final": False}
    assert responses[1]["path"] == str(default_folder / "Name.mp3")


def test_warnings_are_repeated_in_the_final_message(
    monkeypatch: pytest.MonkeyPatch, fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set("no_thumb", title="Artist - Song")
    responses = run_host(monkeypatch, frame({"action": "download", "url": URL, "folder": str(out_dir)}))
    assert_one_final_per_request(responses, 1)
    warning, final = responses
    assert warning["status"] == "warning"
    assert final["status"] == "success"
    assert final["message"] == f"Downloaded: Artist - Song. {warning['message']}."


def test_existing_file_error_carries_the_filename(
    monkeypatch: pytest.MonkeyPatch, fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    (out_dir / "Taken.mp3").write_bytes(b"x")
    [response] = run_host(
        monkeypatch, frame({"action": "download", "url": URL, "folder": str(out_dir), "filename": "Taken"})
    )
    assert response == {
        "status": "error",
        "message": "File with this name already exists: Taken.mp3",
        "filename": "Taken",
        "final": True,
    }


def test_yt_dlp_failure_is_one_readable_error(
    monkeypatch: pytest.MonkeyPatch, fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set("fail")
    # offline (no_github), so the yt-dlp update finds nothing new and there is no retry
    info, response = run_host(monkeypatch, frame({"action": "download", "url": URL, "folder": str(out_dir)}))
    assert info == {"status": "info", "message": "The download failed, checking for a yt-dlp update...", "final": False}
    assert response["status"] == "error" and response["final"] is True
    assert response["message"].endswith("failed: [youtube] abc123: Video unavailable")


def test_timeout_is_one_error(
    monkeypatch: pytest.MonkeyPatch, fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set("hang")
    monkeypatch.setattr(core, "download_audio", functools.partial(core.download_audio, timeout=1))
    [response] = run_host(monkeypatch, frame({"action": "download", "url": URL, "folder": str(out_dir)}))
    assert response["status"] == "error" and response["final"] is True
    assert response["message"].startswith("Download timed out after 1 s")


def test_several_requests_in_one_session(
    monkeypatch: pytest.MonkeyPatch, fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    stdin = requests(
        {"action": "download", "url": URL, "folder": str(out_dir), "filename": "A"},
        {"action": "download", "url": URL, "folder": str(out_dir), "filename": "A"},
        {"action": "download", "url": "bad"},
        {"action": "get_current_folder"},
    )
    responses = run_host(monkeypatch, stdin)
    assert_one_final_per_request(responses, 4)
    assert [r["status"] for r in responses] == ["success", "error", "error", "success"]


# --- the real entry point in its own process ----------------------------------


def host_env(tmp_path: Path) -> dict[str, str]:
    """Environment for a host started as a child process, where monkeypatch can't reach."""
    env = dict(os.environ)
    env["USERPROFILE"] = str(tmp_path)  # Path.home(): keep the default folder out of Documents
    env["YTMP3_LOG_LEVEL"] = "CRITICAL"  # don't write mp3.log into the repo
    return env


def test_host_process_over_real_pipes(tmp_path: Path) -> None:
    stdin = requests({"action": "download", "url": "bad"}, {"action": "get_current_folder"})
    result = subprocess.run(
        [sys.executable, "-m", "ytmp3_host.MP3"], input=stdin, capture_output=True, env=host_env(tmp_path), timeout=60
    )
    assert result.returncode == 0, result.stderr
    responses = parse_frames(result.stdout)
    assert [r["status"] for r in responses] == ["error", "success"]
    assert responses[1]["folder"] == str(tmp_path / "Documents" / "YT-MP3-Downloads")


def test_self_check_runs_instead_of_the_request_loop(tmp_path: Path) -> None:
    # what the installer runs; stdin stays open, so the request loop would block
    result = subprocess.run(
        [sys.executable, "-m", "ytmp3_host.MP3", MP3.SELF_CHECK_ARG],
        stdin=subprocess.PIPE, capture_output=True, text=True, env=host_env(tmp_path), timeout=60,
    )
    assert result.stdout == ""
    if config.FFMPEG_PATH.exists():
        assert (result.returncode, result.stderr) == (0, "")
    else:
        assert result.returncode == 1
        [problem] = result.stderr.strip().splitlines()  # the modules and the framing are fine
        assert problem.startswith(f"{config.FFMPEG_PATH} does not run")


def test_headless_path_does_not_load_the_gui_health_or_updater(tmp_path: Path) -> None:
    # host startup must stay light: customtkinter/Tk only load for the GUI actions,
    # health and the updater (and urllib with them) only for get_version and the updates
    code = (
        "import sys, ytmp3_host.MP3, ytmp3_host.native_messaging\n"
        "heavy = ('customtkinter', 'tkinter', 'PIL', 'ytmp3_host.gui', 'ytmp3_host.health', 'ytmp3_host.updater',\n"
        "         'ytmp3_host.selfcheck', 'urllib.request')\n"
        "loaded = [m for m in heavy if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=host_env(tmp_path))
    assert result.returncode == 0, result.stderr
