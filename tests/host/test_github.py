"""github.latest_release_tag against a local HTTP server that answers like github.com.

A real server (not a fake opener) so the redirect handling of urllib itself is
tested: the lookup must send one HEAD request and read the tag from the redirect
without following it.
"""

import http.server
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from ytmp3_host import github

LATEST = "/owner/repo/releases/latest"


@dataclass
class FakeGitHub:
    """What the local server answers: path -> (status, Location header or None), plus what it received."""
    base_url: str
    routes: dict[str, tuple[int, str | None]] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)
    delay_s: float = 0.0


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    """Serve FakeGitHub routes on 127.0.0.1 and point github.GITHUB_URL at it.

    The opener gets an empty ProxyHandler: a system proxy must not intercept localhost.
    """
    handler_state: list[FakeGitHub] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self) -> None:
            state = handler_state[0]
            state.requests.append((self.command, self.path))
            time.sleep(state.delay_s)
            status, location = state.routes.get(self.path, (404, None))
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_HEAD = do_GET = _answer

        def log_message(self, format: str, *args: object) -> None:  # keep the test output clean
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state = FakeGitHub(f"http://127.0.0.1:{server.server_address[1]}")
    handler_state.append(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(github, "GITHUB_URL", state.base_url)
    monkeypatch.setattr(github, "_opener", urllib.request.build_opener(github._NoRedirect, urllib.request.ProxyHandler({})))
    yield state
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_reads_the_tag_from_the_redirect_without_following_it(fake_github: FakeGitHub, status: int) -> None:
    fake_github.routes[LATEST] = (status, f"{fake_github.base_url}/owner/repo/releases/tag/2026.08.19")
    assert github.latest_release_tag("owner/repo") == "2026.08.19"
    assert fake_github.requests == [("HEAD", LATEST)]


def test_tag_is_returned_as_it_is(fake_github: FakeGitHub) -> None:
    fake_github.routes[LATEST] = (302, "https://github.com/owner/repo/releases/tag/v2.0.0")
    assert github.latest_release_tag("owner/repo") == "v2.0.0"


@pytest.mark.parametrize(
    ("status", "location"),
    [
        (302, "https://github.com/owner/repo/releases"),  # repository without releases
        (302, "https://github.com/owner/repo/releases/tag/"),  # empty tag
        (404, None),  # no such repository
        (200, None),  # no redirect at all
        (500, None),
    ],
    ids=["no-releases", "empty-tag", "404", "200", "500"],
)
def test_unexpected_answers_are_unknown(fake_github: FakeGitHub, status: int, location: str | None) -> None:
    fake_github.routes[LATEST] = (status, location)
    assert github.latest_release_tag("owner/repo") is None


def test_timeout_is_unknown(fake_github: FakeGitHub) -> None:
    fake_github.delay_s = 3
    fake_github.routes[LATEST] = (302, "https://github.com/owner/repo/releases/tag/1.0")
    start = time.monotonic()
    assert github.latest_release_tag("owner/repo", timeout=0.5) is None
    assert time.monotonic() - start < 2.5


def test_an_answer_that_is_not_http_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """E.g. a captive portal or broken proxy: urllib raises http.client.BadStatusLine, not URLError."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()

        def answer_garbage() -> None:
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(b"garbage\r\n\r\n")

        thread = threading.Thread(target=answer_garbage, daemon=True)
        thread.start()
        monkeypatch.setattr(github, "GITHUB_URL", f"http://127.0.0.1:{server.getsockname()[1]}")
        monkeypatch.setattr(github, "_opener", urllib.request.build_opener(github._NoRedirect, urllib.request.ProxyHandler({})))
        assert github.latest_release_tag("owner/repo", timeout=2) is None
        thread.join(timeout=2)


def test_offline_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(github, "GITHUB_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(github, "_opener", urllib.request.build_opener(github._NoRedirect, urllib.request.ProxyHandler({})))
    assert github.latest_release_tag("owner/repo", timeout=2) is None
