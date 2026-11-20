"""主题模块单测：偏好读写、模式解析、切换后颜色真的变了、坏配置回退深色。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app_theme as th  # noqa: E402


def _bind(tmp_path, monkeypatch):
    monkeypatch.setattr(th, "pc",
                        SimpleNamespace(data_dir=lambda: tmp_path))


def test_load_pref_missing_file_falls_back_dark(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    assert th.load_pref() == "dark"


def test_save_and_load_pref_roundtrip(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    assert th.save_pref("light") is True
    assert json.loads((tmp_path / "theme.json").read_text(encoding="utf-8")) == {"mode": "light"}
    assert th.load_pref() == "light"


def test_broken_json_falls_back_dark(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    (tmp_path / "theme.json").write_text("{not json", encoding="utf-8")
    assert th.load_pref() == "dark"


def test_illegal_mode_falls_back_dark(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    (tmp_path / "theme.json").write_text('{"mode": "purple"}', encoding="utf-8")
    assert th.load_pref() == "dark"


def test_set_mode_switches_palette(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    t = th.Theme()
    dark_bg = list(t.bg)
    t.set_mode("light")
    assert t.mode == "light" and t.pref == "light"
    assert list(t.bg) != dark_bg
    assert (tmp_path / "theme.json").read_text(encoding="utf-8").find("light") >= 0
    t.set_mode("dark")
    assert list(t.bg) == dark_bg


def test_toggle_roundtrip(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    t = th.Theme()
    t.set_mode("dark", save=False)
    assert t.toggle() == "light"
    assert t.toggle() == "dark"


def test_system_mode_without_jnius_is_dark(monkeypatch):
    """桌面/无 jnius 时 system 必须回退，不能抛异常。"""
    monkeypatch.setattr(th, "system_mode", lambda: "dark")
    assert th.resolve_mode("system") == "dark"
    assert th.resolve_mode("LIGHT") == "light"
    assert th.resolve_mode("") == "dark"


def test_theme_has_all_palette_keys():
    for key in th.DARK:
        assert key in th.LIGHT, key
        assert hasattr(th.THEME, key), key