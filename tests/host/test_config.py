"""config.py: the Documents folder the default download folder lives in."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ytmp3_host import config


def test_documents_folder_is_the_real_one() -> None:
    folder = config.documents_folder()
    assert folder.is_absolute()
    assert folder.is_dir()


def failing_windll(result: int) -> SimpleNamespace:
    """A ctypes.windll whose SHGetKnownFolderPath returns result without a path."""
    return SimpleNamespace(
        shell32=SimpleNamespace(SHGetKnownFolderPath=lambda *args: result),
        ole32=SimpleNamespace(CoTaskMemFree=lambda pointer: None),
    )


def test_documents_folder_falls_back_when_windows_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.ctypes, "windll", failing_windll(-2147024894))  # E_FILE_NOT_FOUND
    assert config.documents_folder() == Path.home() / "Documents"


def test_documents_folder_falls_back_without_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.ctypes, "windll", SimpleNamespace())
    assert config.documents_folder() == Path.home() / "Documents"
