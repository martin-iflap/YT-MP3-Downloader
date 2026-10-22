"""Release lookups on GitHub.

Uses the web redirect from /<repo>/releases/latest to /<repo>/releases/tag/<tag>
rather than the REST API, which allows only 60 unauthenticated requests an hour per
IP. Only the redirect's Location header is read (a HEAD request that is not
followed), so a lookup transfers no page content.
"""

import http.client
import logging
import urllib.error
import urllib.request
from typing import IO

logger = logging.getLogger(__name__)

GITHUB_URL = "https://github.com"
YT_DLP_REPO = "yt-dlp/yt-dlp"
LOOKUP_TIMEOUT_S = 5
TAG_PATH = "/releases/tag/"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return a redirect to the caller as the response instead of following it.

    urllib treats every non-2xx answer as an error and asks its handlers for a
    replacement response; the base class answers 3xx with the page it redirects to.
    Returning the 3xx response itself makes open() return it like any 2xx answer.
    Other errors (4xx, 5xx) still raise HTTPError.
    """

    def _keep_response(
        self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str, headers: http.client.HTTPMessage
    ) -> IO[bytes]:
        return fp

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = _keep_response


# built once: honors the system proxy settings like any urllib opener
_opener = urllib.request.build_opener(_NoRedirect)


def latest_release_tag(repo: str, timeout: float = LOOKUP_TIMEOUT_S) -> str | None:
    """Look up the tag of a repository's latest release.

    Args:
        repo: The repository as "owner/name".
        timeout: Seconds to wait for GitHub.
    Returns:
        The tag, e.g. "2026.08.19", or None if it could not be determined: offline,
        timed out, the repository has no releases, or GitHub answered unexpectedly.
        Never raises for network problems, so callers can treat None as "unknown".
    """
    url = f"{GITHUB_URL}/{repo}/releases/latest"
    try:
        with _opener.open(urllib.request.Request(url, method="HEAD"), timeout=timeout) as response:
            status, location = response.status, response.headers.get("Location", "")
    except urllib.error.HTTPError as e:  # 4xx, 5xx
        e.close()
        logger.warning("Unexpected answer from %s: HTTP %s", url, e.code)
        return None
    # URLError: offline, DNS, TLS; OSError: timeout, reset; HTTPException: not an HTTP answer
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        logger.warning("Could not look up the latest release of %s: %s", repo, e)
        return None

    # without releases GitHub redirects to the releases list, which has no tag
    if 300 <= status < 400 and TAG_PATH in location:
        return location.rsplit(TAG_PATH, 1)[1].strip("/") or None
    logger.warning("Unexpected answer from %s: HTTP %s %r", url, status, location)
    return None
