"""选片链测试（离线，构造 Card，不联网）。

对应冻结规格 `docs/选片规则_冻结版.md`：match_rank / 过滤链 / 评分 / 待选回退 / 宽容模式。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import songgrab_core as core  # noqa: E402

C = core.Card


def card(bvid, title, play=100000, duration=269, author="UP", **kw):
    return C(bvid=bvid, title=title, play=play, duration=duration, author=author, **kw)


def pick(cards, keyword="晴天", **kw):
    return core.pick_card(cards, keyword, **kw)


def test_match_rank():
    assert core.match_rank("周杰伦《晴天》", "晴天") >= 1
    assert core.match_rank("《晴天》", "晴天") == 2
    assert core.match_rank("稻香", "晴天") == 0
    assert core.match_rank("随便", "") == 0


def test_normal_pick_by_weighted_score():
    pr = pick([card("BVgood", "晴天-周杰伦")])
    assert pr.card is not None and pr.card.bvid == "BVgood", pr.reason
    assert pr.stage == "ok"


def test_title_block_all_rejected_still_offers_candidates():
    pr = pick([card("BVtb", "晴天 合集 十首"), card("BVtc", "晴天 教程")])
    assert pr.card is None
    assert pr.stage == "pending", pr.stage
    assert pr.candidates, pr.reason
    assert "待选" in pr.reason, pr.reason


def test_duration_window_rejects():
    pr = pick([card("BVd", "晴天", duration=30)])
    assert pr.card is None
    assert "短于" in pr.reason or pr.candidates == [], pr.reason
    pr2 = pick([card("BVe", "晴天", duration=3000)])
    assert pr2.card is None, pr2.reason


def test_clip_rejected_gives_candidates():
    pr = pick([card("BVc", "晴天", duration=100)], ref_dur=269.0)
    assert pr.card is None
    assert pr.stage == "pending" and pr.candidates, pr.reason
    assert "切片" in pr.reason or "片段" in pr.reason, pr.reason


def test_all_covers_gives_candidates():
    pr = pick([card("BV1", "晴天 翻唱", play=100000),
               card("BV2", "晴天 cover 女声", play=50000)])
    assert pr.stage == "pending", pr.stage
    assert pr.card is None
    assert pr.candidates, pr.reason


def test_empty_pool():
    pr = pick([])
    assert pr.card is None and not pr.candidates


def test_lenient_want_cover():
    """歌名里自带「翻唱」时不做内容过滤，直接给结果。"""
    pr = pick([card("BV1", "晴天 翻唱", play=100000)], want="晴天 翻唱")
    assert pr.card is not None and pr.card.bvid == "BV1", pr.reason
    assert pr.stage == "ok"


def test_low_play_still_selectable():
    """播放量不进分：1 万以下照样能选（只是乘个系数）。"""
    pr = pick([card("BVlp", "晴天-周杰伦", play=500)])
    assert pr.card is not None and pr.card.bvid == "BVlp", pr.reason


def test_high_play_does_not_beat_title_match():
    """标题完全匹配的，应该赢过播放量高但标题跑偏的。"""
    pr = pick([card("BVhit", "晴天-周杰伦", play=20000),
               card("BVbig", "晴天 钢琴版 合集", play=5000000)])
    assert pr.card is not None and pr.card.bvid == "BVhit", pr.reason


def test_hi_res_prioritized():
    """高音质（Hi-Res/无损）且分数够 → 置顶。"""
    pr = pick([card("BVhr", "【Hi-Res无损】晴天-周杰伦", play=80000),
               card("BVno", "晴天-周杰伦", play=900000)])
    assert pr.card is not None and pr.card.bvid == "BVhr", pr.reason


def test_music_partition_bonus():
    pr = pick([card("BVm", "晴天-周杰伦", play=10000, from_music=True),
               card("BVn", "晴天-周杰伦", play=12000)])
    assert pr.card is not None and pr.card.bvid == "BVm", pr.reason


def test_cover_kind_detection():
    """标题「听完香菜唱《告白气球》」认得出翻唱者；钢琴改编 UP 判成演奏者。"""
    assert core._singer_in_title("听完香菜唱《告白气球》") == "香菜"
    assert core._cover_kind(card("x", "听完香菜唱《告白气球》")) == 2
    assert core._cover_kind(card("y", "Believer", author="Animenzzz")) == 1
    assert core._cover_kind(card("z", "晴天-周杰伦")) == 0


def test_whitelist_up():
    assert core.is_whitelist_up("JLRS-LeoFM")
    assert core.is_whitelist_up("JLRS日落fm")
    assert not core.is_whitelist_up("随便一个UP")


if __name__ == "__main__":
    fails = 0
    names = [n for n in globals() if n.startswith("test_")]
    for name in names:
        try:
            globals()[name]()
            print(f"ok   {name}")
        except Exception as e:
            fails += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(names) - fails} passed, {fails} failed")
    sys.exit(1 if fails else 0)
