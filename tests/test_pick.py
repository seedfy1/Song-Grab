"""选片规则单测（纯 assert，不依赖 pytest）。

运行：python tests/test_pick.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from songgrab_core import (  # noqa: E402
    Card, pick_card, title_matched, parse_playlist_text, sanitize_filename,
    parse_duration, normalize,
)

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}  {extra}")
        FAILS.append(name)


def card(title, play=100000, dur=200, bvid="BV1xx", music=True):
    return Card(bvid=bvid, title=title, play=play, duration=dur, author="up", from_music=music)


# ---------------------------------------------------------------- 相关性

check("完全相等", title_matched("【初音ミク】三日月Step【r-906】", "三日月Step"))
check("同名不同曲-バベル", not title_matched("【电音部】いただきバベル / 黒鉄たま", "バベル"))
check("同名不同曲-平気", not title_matched("平気なんですよ本当に / 初音ミク+Sohbana", "平気？"))
check("多词英文", title_matched("【Daft Punk】Get lucky 1080p", "Get Lucky"))
check("多词英文2", title_matched("大肥鱼单曲 - Let Me Go 动画PV 【AI MV大赛】", "Let Me Go"))
check("繁体归一", title_matched("驟雨の狭間 (Rainshower) / Silentroom", "骤雨の狭间"))
check("无关标题", not title_matched("15天花20万元打造黄金面具", "黄金数"))

# ---------------------------------------------------------------- 数字解析

check("时长 3:10", parse_duration("3:10") == 190)
check("时长 1:02:03", parse_duration("1:02:03") == 3723)

# ---------------------------------------------------------------- 选片优先级

cards = [
    card("【本家投稿】X / r-906", play=500000),
    card("百万级装备听《X》Hi-Res", play=800000),
]
r = pick_card(cards, "X", priority_words=["本家", "百万级装备听", "百万级录音棚听"])
check("本家压过百万级", r.card.title.startswith("【本家投稿】"), r.card.title)

cards2 = [
    card("百万级装备听《X》Hi-Res", play=300000),
    card("普通搬运 X", play=900000),
]
r2 = pick_card(cards2, "X", priority_words=["百万级装备听"])
check("百万级压过普通", r2.card.title.startswith("百万级装备听"), r2.card.title)

# 默认优先词为空 → 纯按播放量（候选里没有书名号时）
cards2b = [card("普通搬运 X", 900000), card("另一个 X", 500000)]
r2b = pick_card(cards2b, "X")
check("默认无优先词按播放量", r2b.card.play == 900000, r2b.card.title)

cards3 = [card("普通搬运 X", play=900000), card("另一个 X", play=500000)]
r3 = pick_card(cards3, "X")
check("无优先词取最高播放", r3.card.play == 900000)

# 词条顺序可自定义：把「百万级录音棚听」提到第一位
cards4 = [
    card("【本家投稿】X", play=500000),
    card("百万级录音棚听《X》", play=300000),
]
r4 = pick_card(cards4, "X", priority_words=["百万级录音棚听", "本家"])
check("自定义词条顺序生效", r4.card.title.startswith("百万级录音棚听"), r4.card.title)

# 词条为空 → 书名号优先于纯播放量（标题带《X》更像歌曲本身）
r5 = pick_card(cards4, "X", priority_words=[])
check("书名号优先", "《X》" in r5.card.title, r5.card.title)

# 书名号那条不过门槛时，退回播放量最高
cards5 = [card("普通搬运 X", 900000), card("某视频《X》", 5000)]
r5b = pick_card(cards5, "X", priority_words=[], min_play=10000)
check("书名号过低时退回播放量", r5b.card.play == 900000, r5b.card.title)

# 正则优先词
cards6 = [card("普通 X", 900000), card("X【Hi-Res】", 100000)]
r6 = pick_card(cards6, "X", priority_words=[r"(Hi-?res|无损)"])
check("正则优先词", "Hi-Res" in r6.card.title, r6.card.title)

# 坏正则退回子串，不崩
r7 = pick_card(cards6, "X", priority_words=["((("])
check("坏正则不崩", r7.card is not None, r7.reason)

# 空优先词列表里的空串被忽略
r8 = pick_card(cards6, "X", priority_words=["", "  "])
check("空词条忽略", r8.card is not None, r8.reason)

# ---------------------------------------------------------------- 过滤

r6 = pick_card([card("X 翻唱", 900000)], "X")
check("排除翻唱", r6.card is None, r6.reason)

r7 = pick_card([card("X 教程", 900000)], "X")
check("排除教程", r7.card is None, r7.reason)

r8 = pick_card([card("X", play=5000)], "X", min_play=10000)
check("播放量门槛", r8.card is None, r8.reason)

r9 = pick_card([card("X", dur=30)], "X", min_dur=90)
check("时长门槛", r9.card is None, r9.reason)

r10 = pick_card([card("完全无关", 900000)], "X")
check("相关性门槛", r10.card is None, r10.reason)

# ---------------------------------------------------------------- 歌单解析

text = """# 注释行
1. 三日月Step ✅
2、 春日寂寞
- 延误列车

Get Lucky
// 也是注释
"""
songs = parse_playlist_text(text)
check("歌单解析", songs == ["三日月Step", "春日寂寞", "延误列车", "Get Lucky"], str(songs))

r11 = pick_card([card("X", play=1000)], "X", min_play=0)
check("门槛0=不限", r11.card is not None)

# ---------------------------------------------------------------- 文件名

check("非法字符清洗", sanitize_filename('a/b:c*d?"e"<f>|g') == "a b c d e f g", sanitize_filename('a/b:c*d?"e"<f>|g'))
long = "长" * 100
check("字节截断", len(sanitize_filename(long).encode("utf-8")) <= 120)

print()
if FAILS:
    print(f"共 {len(FAILS)} 项失败：{FAILS}")
    sys.exit(1)
print("全部通过")
