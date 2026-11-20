"""Windows 1.1.4 L45 B 支灰度的单测：分桶稳定/比例、裸词剥离、gate 与补搜并集。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import songgrab_core as core  # noqa: E402


def _cards(n, song="晴天"):
    return [SimpleNamespace(bvid=f"BV{i}", title=f"周杰伦 {song} 官方") for i in range(n)]


def test_ab_branch_stable_and_roughly_ten_percent(monkeypatch):
    monkeypatch.setattr(core, "_B_GATE_ENABLED", True)
    monkeypatch.setattr(core, "_B_BUCKET_PCT", 10)
    lines = [f"歌曲{i} - 歌手{i}" for i in range(1000)]
    first = [core._ab_branch(x) for x in lines]
    assert first == [core._ab_branch(x) for x in lines]   # 稳定可复现
    ratio = sum(1 for b in first if b == "B") / len(first)
    assert 0.05 <= ratio <= 0.20, ratio


def test_ab_branch_disabled_or_empty(monkeypatch):
    monkeypatch.setattr(core, "_B_GATE_ENABLED", False)
    assert core._ab_branch("晴天") == "A"
    monkeypatch.setattr(core, "_B_GATE_ENABLED", True)
    assert core._ab_branch("") == "A"


def test_bare_query_strips_modifiers():
    assert core._bare_query("晴天 (Live) + 周杰伦") == "晴天"
    assert core._bare_query("晴天 feat. 阿信") == "晴天"
    assert core._bare_query("李白（伴奏版）") == "李白"
    assert core._bare_query("") == ""


def test_title_cover_basic():
    assert core._title_cover("晴天", "周杰伦《晴天》官方MV") == 1.0
    assert core._title_cover("晴天", "稻香") == 0.0
    assert core._title_cover("", "x") == 0.0


def test_b_gate_passes_with_enough_cards_and_title_cover():
    cards = _cards(5)
    assert core._b_gate(cards, "晴天") is True
    assert core._b_gate(_cards(2), "晴天") is False


def test_b_augment_skips_search_when_gate_passes(monkeypatch):
    def boom(_q):
        raise AssertionError("gate 已过，不应补搜")
    monkeypatch.setattr(core, "search_both", boom)
    cards = _cards(6)
    assert core._b_augment(cards, "晴天", "晴天", "") == cards


def test_b_augment_merges_without_duplicates(monkeypatch):
    cards = _cards(2)                      # 不足 5 张 → gate 必不过
    extra = [SimpleNamespace(bvid="BVnew", title="晴天 其它版本"), cards[0]]
    monkeypatch.setattr(core, "search_both", lambda q: extra)
    got = core._b_augment(cards, "晴天 (Live)", "晴天", "")
    assert [c.bvid for c in got] == ["BV0", "BV1", "BVnew"]


def test_b_augment_falls_back_when_nothing_new(monkeypatch):
    cards = _cards(2)
    monkeypatch.setattr(core, "search_both", lambda q: list(cards))
    assert core._b_augment(cards, "晴天 (Live)", "晴天", "") == cards