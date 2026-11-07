"""选片链回归测试（对应 docs/选片规则_冻结版.md）。

全部用构造的候选，不联网 —— B站接口会风控，测试必须离线可跑。
跑法：python tests/test_pick.py  或  pytest tests/test_pick.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import picker as P  # noqa: E402


def C(title, play=100000, duration=240, author="UP", music=True,
      typeid=3, videos=1):
    return P.Cand(raw=None, bvid=f"BV{abs(hash(title)) % 10**9:09d}",
                  title=title, play=play, duration=duration, author=author,
                  from_music=music, typeid=typeid, videos=videos)


def pick(cards, kw, **kw2):
    return P.pick(cards, kw, **kw2)


# ---------------------------------------------------------------- §2.1 匹配
def test_match_rank():
    assert P.match_rank("周杰伦《晴天》", "晴天") == 3
    assert P.match_rank("「晴天」官方MV", "晴天") == 3
    assert P.match_rank("晴天", "晴天") == 2
    assert P.match_rank("周杰伦 - 晴天MV", "晴天") == 1
    assert P.match_rank("周杰伦 - 晴天MV", "晴天 周杰伦") == 0   # 组合词不是歌名
    assert P.match_rank("今天天气不错", "晴天") == 0
    assert P.match_rank("", "晴天") == 0


def test_shell_of():
    # 【X】歌名：X 不是歌名/技术词/作者 → 二创壳
    assert P.shell_of("【鬼畜】晴天", "晴天", "") == "鬼畜"
    assert P.shell_of("【4K修复】晴天", "晴天", "") is None
    assert P.shell_of("【晴天】周杰伦", "晴天", "") is None


def test_kind_of():
    assert P.kind_of("晴天 钢琴版纯音乐") == 1
    assert P.kind_of("晴天 cover 翻唱") == 2
    assert P.kind_of("周杰伦《晴天》官方MV") == 0


# ---------------------------------------------------------------- §2 硬过滤
def test_all_covers_rejected():
    cards = [C("晴天 cover 翻唱"), C("晴天 钢琴版"), C("晴天 纯音乐 钢琴")]
    r = pick(cards, "晴天")
    assert r.cand is None, r.reason
    assert "翻唱" in r.reason or "器乐" in r.reason


def test_album_and_long_rejected():
    cards = [C("晴天 整张专辑连播", 500000, 3000),
             C("周杰伦《晴天》", 80000, 269)]
    r = pick(cards, "晴天")
    assert r.cand.title == "周杰伦《晴天》", r.reason


def test_title_block_rejected():
    cards = [C("晴天 吉他教学", 900000, 600),
             C("周杰伦《晴天》", 50000, 269)]
    r = pick(cards, "晴天")
    assert "教学" not in r.cand.title


def test_duration_window():
    cards = [C("周杰伦《晴天》", 50000, 30),      # < min_dur
             C("周杰伦《晴天》完整版", 50000, 900),  # > max_dur
             C("周杰伦《晴天》", 50000, 269)]
    r = pick(cards, "晴天", min_dur=50, max_dur=720)
    assert r.cand.duration == 269


def test_clip_rejected():
    ref = 269
    cards = [C("晴天 精彩片段", 900000, 60),
             C("周杰伦《晴天》", 50000, 269)]
    r = P.pick(cards, "晴天", ref_dur=ref)
    assert r.cand.duration == 269, r.reason


# ---------------------------------------------------------------- §3 评分
def test_play_floor():
    assert P._play_floor(0) == 1.0
    assert P._play_floor(500) == 0.45
    assert P._play_floor(5000) == 0.65
    assert P._play_floor(50000) == 1.0


def test_high_play_does_not_beat_title():
    """播放量不进分：标题干净的 3 万播要压过 500 万播的杂标题。"""
    hot = C("【鬼畜】晴天 全程高能", 5000000, 200, typeid=21, music=False)
    clean = C("周杰伦《晴天》", 30000, 269)
    r = pick([hot, clean], "晴天")
    assert r.cand.bvid == clean.bvid, r.reason


def test_music_partition_bonus():
    a = C("周杰伦《晴天》", 50000, 269, music=True)
    b = C("周杰伦《晴天》", 50000, 269, music=False, typeid=21)
    r = pick([b, a], "晴天")
    assert r.cand.bvid == a.bvid


def test_title_score_range():
    assert P.title_score("周杰伦《晴天》", 269) == 1.0
    assert P.title_score("晴天 慢速降调回响", 269) < 1.0
    assert P.title_score("晴天", 30) < 1.0        # 时长异常
    s = P.title_score("晴天" + "《x》" * 3, 269)    # 多书名号
    assert s < 1.0


# ---------------------------------------------------------------- §4 优先词
def test_priority_word():
    cards = [C("晴天 官方MV", 9000000, 269),
             C("晴天 演唱会现场版", 10000, 269)]
    r = pick(cards, "晴天", priority_words=["官方"])
    assert "官方" in r.cand.title, r.reason
    # 词条不命中就回到加权排序，不是报错
    r2 = pick(cards, "晴天", priority_words=["不存在的词"])
    assert r2.cand is not None and r2.cand.play == 9000000, r2.reason


def test_hi_res_stage():
    """§4 高音质档：音乐分区 + Hi-Res/无损 + 加权 ≥ 第一名 25% → 置顶。"""
    cards = [C("周杰伦《晴天》", 9000000, 269),
             C("晴天 Hi-Res 无损 周杰伦", 10000, 269)]
    r = pick(cards, "晴天")
    assert "Hi-Res" in r.cand.title, r.reason


def test_priority_regex_broken_falls_back():
    cards = [C("周杰伦《晴天》完整版", 50000, 269),
             C("周杰伦《晴天》", 90000, 269)]
    r = pick(cards, "晴天", priority_words=["((("])
    assert r.cand is not None


def test_whitelist_direct():
    cards = [C("JLRS 日落fm｜晴天", 3000, 269, author="JLRS日落fm"),
             C("周杰伦《晴天》", 9000000, 269)]
    r = pick(cards, "晴天")
    assert r.cand.author == "JLRS日落fm", r.reason


# ---------------------------------------------------------------- §5 作者链
def test_choose_prefers_title_author_match():
    """标题写着别的作者 → 判翻唱跳过；作者对得上就采信。"""
    pool = [C("某歌手《晴天》", 9000000, 269),
            C("周杰伦 - 晴天", 50000, 269)]
    for c in pool:
        c.score = 1.0
    cand, who, why = P.choose_card(pool, "晴天", canonical="周杰伦", ref_dur=269)
    assert cand is not None and who == "周杰伦", why
    assert "周杰伦" in cand.title


def test_choose_no_author_hint_uses_cluster():
    """没有作者锚点时，靠时长簇的众数作者兜底。"""
    pool = [C("晴天 现场版", 9000000, 269),
            C("周杰伦《晴天》", 50000, 266),
            C("晴天 周杰伦 录音室", 1000, 268)]
    for c in pool:
        c.score = 1.0
    cand, who, why = P.choose_card(pool, "晴天", canonical="", ref_dur=267)
    assert cand is not None, why


def test_choose_skips_conflicting_title_author():
    pool = [C("周杰伦《晴天》", 100, 269), C("某歌手《晴天》", 100, 269)]
    cand, who, why = P.choose_card(pool, "晴天", canonical="周杰伦", ref_dur=269)
    assert cand.title.startswith("周杰伦"), why


def test_artists_in_title():
    assert "周杰伦" in P._artists_in_title("周杰伦《晴天》")
    assert "周杰伦" in P._artists_in_title("【4K修复】周杰伦 - 晴天MV")
    assert "周杰伦" in P._artists_in_title("《晴天》- 周杰伦")


def test_empty_pool():
    r = pick([], "晴天")
    assert r.cand is None and r.reason


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    bad = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok  {name}")
        except AssertionError as e:
            bad += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:
            bad += 1
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
    print(f"{len(fns) - bad}/{len(fns)} 通过")
    sys.exit(1 if bad else 0)
