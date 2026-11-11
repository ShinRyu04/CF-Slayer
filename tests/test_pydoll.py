from __future__ import annotations

from pathlib import Path

import pytest

import solver.pydoll_backend as pydoll_backend
from solver.chain import BackendChain, ChainSolveFailed
from solver.errors import SolveFailed
from solver.pydoll_backend import (
    PydollBackend,
    chrome_major_version,
    chrome_user_agent,
    find_chrome,
)


def test_explicit_chrome_path_wins(tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"")
    assert find_chrome(str(chrome)) == str(chrome)


def test_missing_explicit_chrome_path_does_not_fall_back(tmp_path):
    assert find_chrome(str(tmp_path / "missing.exe")) is None


def test_version_is_read_from_the_application_folder(tmp_path):
    chrome = tmp_path / "Application" / "157.0.6099.62" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"")
    assert chrome_major_version(str(chrome)) == "157"
    ua = chrome_user_agent(str(chrome))
    assert ua is not None
    assert "Chrome/157.0.0.0" in ua
    assert "Headless" not in ua


def test_version_missing_returns_none(tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"")
    assert chrome_major_version(str(chrome)) is None
    assert chrome_user_agent(str(chrome)) is None


def test_available_reports_missing_chrome(tmp_path):
    backend = PydollBackend(chrome_path=str(tmp_path / "nope.exe"))
    ok, reason = backend.available()
    assert ok is False
    assert "CF_SLAYER_CHROME_PATH" in reason


def test_available_reports_missing_package(monkeypatch, tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"")
    monkeypatch.setattr(pydoll_backend.importlib.util, "find_spec", lambda name: None)
    backend = PydollBackend(chrome_path=str(chrome))
    ok, reason = backend.available()
    assert ok is False
    assert "pydoll not installed" in reason


def test_solve_without_chrome_raises_solve_failed(tmp_path):
    backend = PydollBackend(chrome_path=str(tmp_path / "nope.exe"))
    with pytest.raises(SolveFailed, match="Chrome"):
        backend.solve("https://target.example/", timeout=5.0)


def test_chain_skips_pydoll_without_chrome():
    pydoll = PydollBackend(chrome_path="C:/definitely/missing/chrome.exe")
    chain = BackendChain([pydoll])
    with pytest.raises(ChainSolveFailed) as info:
        chain.solve("https://target.example/", timeout=1.0)
    assert info.value.attempts[0].backend == "pydoll"
    assert "Chrome not found" in str(info.value.attempts[0].error)


def test_chrome_executable_is_detected_on_this_windows_machine():
    # Windows acceptance check: a real Chrome install must be found.
    path = find_chrome()
    if path is None:
        pytest.skip("no Chrome on this machine")
    assert Path(path).is_file()
