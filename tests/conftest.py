"""Isolated application fixture: no live hooks and no user settings changes."""
import tkinter as tk
from unittest.mock import Mock

import pytest

import main


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(main, "RECORD_FILE", tmp_path / "recording.json")
    monkeypatch.setattr(main, "LEGACY_CONFIG_FILE", tmp_path / "legacy-config.json")
    monkeypatch.setattr(main, "LEGACY_RECORD_FILE", tmp_path / "legacy-recording.json")
    # Keep pynput's real parser, key matching and auto-repeat suppression.
    for method in ("start", "stop", "join"):
        monkeypatch.setattr(main.keyboard.GlobalHotKeys, method, Mock())
    root = tk.Tk()
    root.withdraw()
    instance = main.ClickerApp(root)
    try:
        yield instance
    finally:
        instance.close()
