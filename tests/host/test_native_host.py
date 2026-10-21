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
from ytmp3_host import MP3, core, native_messaging
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
    request: dict[str, object] = {"url": "https://vimeo.com/1"}
    if action is not None:
        request["action"] = action
    [response] = run_host(monkeypatch, frame(request))
    assert response == {"status": "error", "message": "Invalid YouTube URL", "final": True}


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


# --- headless download --------------------------------------------------------


@pytest.mark.parametrize("url", ["", "https://vimeo.com/1", "not a url"])
def test_invalid_url(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    [response] = run_host(monkeypatch, frame({"action": "download", "url": url}))
    assert response == {"status": "error", "message": "Invalid YouTube URL", "final": True}


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
    [response] = run_host(monkeypatch, frame({"action": "download", "url": URL, "folder": str(out_dir)}))
    assert response["status"] == "error" and response["final"] is True
    assert response["message"].endswith("failed: [youtube] abc123: Video unavailable")


def test_timeout_is_one_error(
    monkeypatch: pytest.MonkeyPatch, fake_yt_dlp: FakeYtDlp, fake_embed: list[EmbedCall], out_dir: Path
) -> None:
    fake_yt_dlp.set("hang")
    monkeypatch.setattr(native_messaging, "download_audio", functools.partial(core.download_audio, timeout=1))
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


def test_headless_path_does_not_load_the_gui(tmp_path: Path) -> None:
    # host startup must stay light: customtkinter/Tk only load for the GUI actions
    code = (
        "import sys, ytmp3_host.MP3, ytmp3_host.native_messaging\n"
        "loaded = [m for m in ('customtkinter', 'tkinter', 'PIL', 'ytmp3_host.gui') if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=host_env(tmp_path))
    assert result.returncode == 0, result.stderr
