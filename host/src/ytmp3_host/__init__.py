"""Native-messaging host of the YouTube MP3 Downloader Firefox extension."""

# Bump together with `version` in pyproject.toml and MyAppVersion in
# packaging/installer/mp3_installer.iss (tests/host/test_versions.py checks they match).
# The extension's MIN_HOST_VERSION (extension/background.js) must never be above it.
__version__ = "2.0.0"
