"""Windows 对齐六项里可单测的部分：known_bad / 纠正流水 / 更新源派生。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import songgrab_core as core  # noqa: E402


def _bind(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "KNOWN_BAD_FILE", tmp_path / "known_bad.json")
    monkeypatch.setattr(core, "CORRECTIONS_FILE", tmp_path / "corrections.jsonl")
    monkeypatch.setattr(core, "EVAL_MODE", False)


def test_known_bad_put_active_undo(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    assert core.known_bad_active("晴天", "BV1xx") is False
    assert core.known_bad_put("晴天+周杰伦", "BV1xx", reason="用户标记不合适")
    assert core.known_bad_active("晴天", "BV1xx") is True
    data = json.loads((tmp_path / "known_bad.json").read_text(encoding="utf-8"))
    rec = data["晴天||BV1xx"]
    assert rec["reason"] == "用户标记不合适" and rec["revoked"] is False
    assert core.known_bad_undo("晴天", "BV1xx")
    assert core.known_bad_active("晴天", "BV1xx") is False


def test_known_bad_undo_without_record(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    assert core.known_bad_undo("不存在", "BV1") is False


def test_record_correction_appends_events(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    assert core.record_correction("晴天", "BVA", "BVB", kind="repick")
    assert core.record_correction("晴天", "BVB", "BVC", tier="pending")
    rows = [json.loads(line) for line in
            (tmp_path / "corrections.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["old_bvid"], r["new_bvid"]) for r in rows] == [
        ("BVA", "BVB"), ("BVB", "BVC")]
    assert rows[0]["song"] == "晴天" and rows[0]["kind"] == "repick"
    assert rows[1]["tier"] == "pending"
