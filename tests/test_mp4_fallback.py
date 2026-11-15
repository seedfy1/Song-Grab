"""mp4 只对直链生效时的提示（歌名模式静默降级 → 必须给出可见说明）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import songgrab_core as core  # noqa: E402


class _Card:
    def __init__(self):
        self.bvid = "BV1xx411c7mD"
        self.title = "周杰伦《七里香》"
        self.play = 123456
        self.duration = 299
        self.author = "UP主"


class _Pick:
    def __init__(self, card):
        self.card = card
        self.reason = "关键词直中"
        self.pool = [card]
        self.scores = {card.bvid: 3.0}


def test_song_name_mp4_falls_back_with_visible_hint(tmp_path, monkeypatch):
    card = _Card()
    monkeypatch.setattr(core, "known_good_lookup", lambda *a, **k: None)
    monkeypatch.setattr(core, "lookup_artist_candidates", lambda *a, **k: [])
    monkeypatch.setattr(core, "load_artists", lambda *a, **k: {})
    monkeypatch.setattr(core, "choose_card",
                        lambda *a, **k: (_Pick(card), [card], "七里香", "周杰伦"))
    cfg = {"output_format": "mp4", "output_dir": str(tmp_path)}
    res = core.describe_song("七里香", cfg)
    assert not res.get("fail"), res.get("fail")
    assert "mp4 需 BV 号或视频链接，已按音频下载" in res["reason"]
    assert res["fname"].endswith(".m4a"), res["fname"]


def test_direct_link_keeps_mp4(tmp_path, monkeypatch):
    card = _Card()
    monkeypatch.setattr(core, "known_good_lookup", lambda *a, **k: None)
    monkeypatch.setattr(core, "resolve_video", lambda ref: {
        "bvid": card.bvid, "title": card.title, "play": card.play,
        "duration": card.duration, "author": card.author})
    cfg = {"output_format": "mp4", "output_dir": str(tmp_path)}
    res = core.describe_song("BV1xx411c7mD 七里香", cfg)
    assert res["direct"] is True
    assert res["fname"].endswith(".mp4"), res["fname"]
    assert "mp4 需 BV 号" not in res.get("reason", "")
