"""选片规则 —— 唯一实现。对应 docs/选片规则_冻结版.md（§1–§7）。

约束：本模块不联网、不依赖 UI，只吃候选卡片，纯函数，方便单测。
core.pick_card() / core.trial_song() 都是它的包装。

播放量在 v8 起**不进分**，只做排序的次要 key；资格靠 §2 的过滤链。
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

# ---------------------------------------------------------------- §7 锚点

VARIANT = {
    "間": "间", "驟": "骤", "燈": "灯", "裏": "里", "樂": "乐", "園": "园",
    "會": "会", "戰": "战", "鐵": "铁", "龍": "龙", "髮": "发", "髪": "发",
    "說": "说", "讓": "让", "戀": "恋", "愛": "爱", "夢": "梦", "貓": "猫",
    "鳥": "鸟", "魚": "鱼", "車": "车", "門": "门", "東": "东", "馬": "马",
    "貝": "贝", "風": "风", "雲": "云", "電": "电", "頭": "头", "顏": "颜",
    "數": "数", "學": "学", "國": "国", "對": "对", "時": "时", "語": "语",
    "誰": "谁", "變": "变", "邊": "边", "進": "进", "遠": "远", "過": "过",
    "還": "还", "這": "这", "麼": "么", "樣": "样", "點": "点", "熱": "热",
    "燒": "烧", "現": "现", "實": "实", "開": "开", "關": "关", "無": "无",
    "為": "为", "與": "与", "從": "从", "來": "来", "個": "个", "們": "们",
    "幾": "几", "藝": "艺", "術": "术", "聲": "声", "聽": "听", "覺": "觉",
    "見": "见", "記": "记", "憶": "忆", "讀": "读", "寫": "写", "書": "书",
    "畫": "画", "談": "谈", "論": "论", "證": "证", "識": "识", "體": "体",
    "麗": "丽", "黃": "黄", "紅": "红", "綠": "绿", "藍": "蓝", "銀": "银",
    "錢": "钱", "鐘": "钟", "陽": "阳", "陰": "阴", "靜": "静", "響": "响",
    "飛": "飞", "麥": "麦", "歲": "岁", "華": "华", "葉": "叶", "藥": "药",
    "蟲": "虫", "惡": "恶",
}


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).lower()
    s = "".join(ch for ch in s if ch.isalnum())
    return "".join(VARIANT.get(ch, ch) for ch in s)


TITLE_SPLIT = re.compile(r"[\s【】\[\]（）()『』「」<>《》/／|｜~～\-—_,，.。:：;；+]+")


def tokens(title: str) -> list[str]:
    return [t for t in TITLE_SPLIT.split(str(title)) if t]


def _en(word: str) -> str:
    """英文词加词边界（中文是 \\w，不能用 \\b）。"""
    return rf"(?<![A-Za-z]){word}(?![A-Za-z])"


_NOT_ARTIST_RX = re.compile(
    r"(音阙诗听|supercell|众星|群星|合唱|电影原声|电视剧原声|原声带|OST|厂牌|"
    + _en("feat") + r"|" + _en("ft") + r"|华语群星|网络歌手|未知艺术家|Various)",
    re.IGNORECASE,
)

_DASH_ARTIST_RX = re.compile(
    r"[\-—–]\s*([\u3040-\u30ff\u4e00-\u9fffA-Za-z][\u3040-\u30ff\u4e00-\u9fffA-Za-z0-9·&\.\s]{1,11})\s*$"
)

_FAKE_ARTIST = re.compile(
    r"^(我想|这是|那个|这个|我们|你们|他们|什么|怎么|就是|如果|因为|所以|但是|"
    r"一个|一首|今天|昨天|明天|大家|自己|真的|还是|已经|可以|没有|知道|觉得|"
    r"高音质|无损|完整版|官方|动态歌词|歌词版)$"
)

# ---------------------------------------------------------------- §2 硬过滤

TITLE_BLOCK = re.compile(
    r"(教程|教学|如何|怎么|手书|攻略|实况|通关|记录|谱面|自制谱|全连|首杀|"
    r"理论值|解说|混剪|歌ってみた|翻唱|合集|十选|排行榜|盘点|"
    + _en("amv") + "|" + _en("mad") + "|" + _en("cover") + "|" + _en("reaction")
    + "|" + _en("chart") + "|" + _en("anim") + "|" + _en("animation") + ")",
    re.IGNORECASE,
)

_TITLE_INSTR_RX = re.compile(
    r"(古筝|钢琴版|钢琴|鼓谱|架子鼓|贝斯|太鼓|双排键|电子琴|管风琴|手风琴|"
    r"八音盒|音乐盒|竖琴|萨克斯|二胡|琵琶|笛子|葫芦丝|口琴|拇指琴|尤克里里|"
    r"小提琴|大提琴|吉他版|指弹|民乐|纯器乐|"
    + _en("instrumental") + "|" + _en("inst") + ")",
    re.IGNORECASE,
)

_REACTION_RX = re.compile(
    r"(反应|盘点|为啥|为什么|秘闻|速学|教学|歌词分解|解析|杂谈|科普|冷知识|"
    r"你知道吗|涨知识|乐评|揭秘|" + _en("reaction") + ")",
    re.IGNORECASE,
)

_KARAOKE_RX = re.compile(
    r"(伴奏|消音|卡拉\s*OK|纯伴奏|无人声|"
    + _en("karaoke") + "|" + _en(r"off\s*vocal") + "|" + _en(r"backing\s*track") + ")",
    re.IGNORECASE,
)

_ALBUM_WORD_RX = re.compile(
    r"(专辑|全曲|完整专辑|合集|全收录|精选集|"
    + _en("album") + "|" + _en(r"full\s*album") + ")",
    re.IGNORECASE,
)

_SHELL_LEAD_RX = re.compile(r"^\s*【([^】]{1,20})】")
_SHELL_JUNK = re.compile(
    r"^(\d{3,4}[pP]|\d{2,3}\s*[fF][pP][sS]|4K|8K|HD|HDR|高清|超清|蓝光|修复|"
    r"完整版|官方|MV|PV|OP|ED|无损|Hi-?Res|纯享|字幕|中字|双语|合集|翻唱)$",
    re.IGNORECASE,
)
_SHELL_JUNK_WORD = re.compile(
    r"(\d{3,4}\s*[pP]|\d{2,3}\s*[fF][pP][sS]|4K|8K|HD|HDR|高清|超清|蓝光|修复|"
    r"完整版|官方|MV|PV|OP|ED|无损|Hi-?Res|高音质|纯享|字幕|中字|双语|合集|"
    r"杜比|全景声|收藏|搬运|音频|现场|版)",
    re.IGNORECASE,
)

_SHELL_BAIT = re.compile(
    r"(用|鬼畜|整活|沙雕|搞笑|土味|方言|恶搞|调教|配音|朗读|朗诵|念|rap|"
    r"串烧|改编|混音|手工|AI)",
    re.IGNORECASE,
)

_COVER_RX = re.compile(
    r"(翻唱|男声版|女声版|童声版|戏腔版|升key|降key|变速|加速|清唱|"
    + _en("cover") + "|" + _en("remix") + ")",
    re.IGNORECASE,
)

_REPOST_RX = re.compile(r"(搬运|转载|音乐台|歌单|每日推荐|音乐库|曲库|合集站)")

_CLIP_RATIO = 0.65
_LONG_RATIO = 1.6
_ALBUM_HARD_DUR = 25 * 60
_ALBUM_WORD_DUR = 8 * 60

WHITELIST_UPS = ("JLRS日落fm", "JLRS-LeoFM", "JLRS-jayfm")

WL_BLOCK_RX = re.compile(
    r"合唱|三人合唱|多人合唱|大合唱|万人合唱|接力合唱|接龙|全员合唱|群星|分声部|"
    r"多声部|三声部|混声合唱|轮唱|和声|伴唱|阿卡贝拉|无伴奏|纯人声|单人阿卡贝拉|"
    r"翻唱|翻唱版|男声版|女声版|童声版|戏腔版|升key|降key|变速|加速|清唱|试唱|"
    r"demo|纯享版|现场版|舞台版|导师互唱|改编|重编曲|混音|填词|中文填词|翻填|空耳|"
    r"串烧|组曲|mashup|重置版|伴奏|消音|纯音乐|无人声|卡拉OK|演奏|钢琴版|指弹|"
    r"民乐版|古筝|纯器乐|调教|声库|AI翻唱|AI音乐|ACE|DiffSinger|鬼畜调教|音MAD|"
    r"人力VOCALOID|MMD|"
    + _en("cover") + "|" + _en("karaoke") + "|" + _en("instrumental")
    + "|" + _en("remix") + "|" + _en("live") + "|" + _en("mashup"),
    re.IGNORECASE,
)

MUSIC_TYPEIDS = {3, 28, 29, 30, 31, 59, 130, 136, 193, 267}


# ---------------------------------------------------------------- 数据结构

@dataclass
class Cand:
    raw: Any
    bvid: str
    title: str
    play: int
    duration: int
    author: str
    from_music: bool = False
    typeid: int = 0
    videos: int = 0
    rank: int = 0
    kind: int = 0
    score: float = 1.0

    @property
    def short(self) -> str:
        return f"{self.title[:36]} · {self.play} 播 · {self.duration}s"


@dataclass
class Picked:
    cand: Cand | None
    reason: str
    pool: list[Cand] = field(default_factory=list)
    top: list[Cand] = field(default_factory=list)
    rebuilt: bool = False
    stage: str = ""


def as_cand(card: Any) -> Cand:
    if isinstance(card, Cand):
        return card
    return Cand(
        raw=card,
        bvid=str(getattr(card, "bvid", "") or ""),
        title=str(getattr(card, "title", "") or ""),
        play=int(getattr(card, "play", 0) or 0),
        duration=int(getattr(card, "duration", 0) or 0),
        author=str(getattr(card, "author", "") or ""),
        from_music=bool(getattr(card, "from_music", False)),
        typeid=int(getattr(card, "typeid", 0) or 0),
        videos=int(getattr(card, "videos", 0) or 0),
    )


# ---------------------------------------------------------------- §2.1 标题匹配

def match_rank(title: str, keyword: str) -> int:
    """3 = 书名号/引号包裹；2 = 精确 token；1 = 包含；0 = 不匹配。"""
    k = normalize(keyword)
    if not k or not title:
        return 0
    kw = str(keyword).strip()
    if f"《{kw}》" in title or f"「{kw}」" in title or f"<{kw}>" in title:
        return 3
    if re.search(r"\s", kw) and k in normalize(title):
        return 2
    for tok in tokens(title):
        n = normalize(tok)
        if not n:
            continue
        if n == k:
            return 2
        if k in n and len(n) - len(k) <= 2:
            return 1
    if len(k) >= 2 and k in normalize(title):
        return 1
    return 0


def title_matched(title: str, keyword: str) -> bool:
    return match_rank(title, keyword) > 0


def shell_of(title: str, keyword: str, author: str = "") -> str | None:
    """`【X】歌名` 二创壳：X 非歌名/技术词/曲库作者，且后面就是歌名。"""
    m = _SHELL_LEAD_RX.match(title or "")
    if not m:
        return None
    x = m.group(1).strip()
    nx, nk = normalize(x), normalize(keyword)
    if not nx or nx in nk or nk in nx:
        return None
    if _SHELL_JUNK.match(x):
        return None
    # 「4K修复」「完整版官方」这类组合前缀也当技术词
    if not _SHELL_JUNK_WORD.sub("", x).strip(" ・·/-—–_|"):
        return None
    if author and normalize(author) == nx:
        return None
    rest = normalize(title[m.end():])
    if _SHELL_BAIT.search(x):
        return x
    if rest and rest.startswith(nk) and len(rest) - len(nk) <= 2:
        return x
    return None


def kind_of(title: str) -> int:
    if _TITLE_INSTR_RX.search(title or ""):
        return 1
    if _COVER_RX.search(title or ""):
        return 2
    return 0


# ---------------------------------------------------------------- §6 标题加权

_TS_RULES: list[tuple[float, int, re.Pattern[str] | None, str]] = [
    (-0.62, 1, re.compile(r"[（(]\s*原曲\s*[:：]"), "改编"),
    (-0.35, 2, re.compile(r"(歌单|合集|全曲|整张|连播|伴奏|纯伴奏|消音)"), "歌单/合集/伴奏"),
    (-0.12, 2, re.compile(r"(remix|混音版|" + _en("live") + r"|现场)"), "remix/live"),
    (-0.35, 2, re.compile(r"(剪辑|混剪|" + _en("amv") + "|" + _en("mad") + ")"), "剪辑/AMV"),
    (-0.50, 1, re.compile(r"(AI\s*翻唱|sovits|" + _en("diffsinger") + ")"), "AI 翻唱"),
    (-0.35, 2, re.compile(r"(8D|环绕音|低音增强|" + _en(r"bass\s*boost") + "|慢速|降速|回响)"), "音效处理"),
    (-0.40, 1, re.compile(r"(付费|会员专享|氪金)"), "付费"),
    (-0.45, 1, re.compile(r"(专辑|" + _en("album") + r"|" + _en("ep") + ")"), "专辑"),
    (-0.50, 1, _REACTION_RX, "解说/盘点"),
    (-0.30, 1, re.compile(
        r"(我想|我觉得|你知道吗|为什么|居然|据说|你敢信|没想到|原来|"
        r"这歌|这首歌|听完|一句|开口)"), "口语化梗词"),
    (-0.30, 1, None, "多书名号"),
    (-0.30, 1, re.compile(_en("cover")), "cover"),
    (-0.35, 2, _TITLE_INSTR_RX, "器乐"),
    (-0.20, 2, re.compile(r"(生日会|生放|音乐节|演唱会)"), "现场活动"),
    (-0.45, 1, re.compile(r"(用.*?唱|鬼畜|调教)"), "鬼畜壳"),
    (-0.25, 1, re.compile(r"(" + _en("feat") + "|" + _en("ft") + r")"), "feat"),
    (-0.65, 1, None, "时长异常"),
    (+0.05, 2, re.compile(r"(官方|完整版|无损|原声|本家)"), "官方/完整版"),
    (+0.25, 1, re.compile(r"(Hi-?Res|高解析|无损音质)"), "Hi-Res"),
]


def title_score(title: str, duration: int = 0, ref_dur: int = 0,
                clip_hit: bool = False) -> float:
    t = title or ""
    s = 1.0
    for delta, cap, rx, name in _TS_RULES:
        if name == "多书名号":
            if len(re.findall(r"[《「『]", t)) >= 2:
                s += delta
            continue
        if name == "时长异常":
            if clip_hit:
                continue
            if duration and (duration < 90 or duration > 12 * 60):
                s += delta
            continue
        if rx is None:
            continue
        n = min(cap, len(rx.findall(t)))
        if n:
            s += delta * n
    return max(0.25, min(1.40, s))


# ---------------------------------------------------------------- §3 加权

def _play_floor(play: int) -> float:
    if play <= 0:
        return 1.0
    if play < 1000:
        return 0.45
    if play < 10000:
        return 0.65
    return 1.0


def _tag_score(c: Cand) -> float:
    return 1.05 if (c.from_music or c.typeid in MUSIC_TYPEIDS) else 0.95


def _up_score(c: Cand, canonical: str) -> float:
    if canonical and normalize(canonical) in normalize(c.author):
        return 1.10
    if _REPOST_RX.search(c.author or ""):
        return 0.85
    return 1.0


def _artist_confirmed(c: Cand, canonical: str, ref_dur: int) -> int:
    if not canonical:
        return 0
    nc = normalize(canonical)
    votes = 0
    if nc and nc in normalize(c.author):
        votes += 1
    if nc and nc in normalize(c.title):
        votes += 1
    if ref_dur and c.duration and abs(c.duration - ref_dur) <= 5:
        votes += 1
    return votes


def _artists_in_title(title: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"([^\s【】\[\]（）()《》/|,，。！!…~\-—–]{2,16})\s*《", title or ""):
        out.append(m.group(1).strip())
    for m in re.finditer(r"《[^》]*》\s*[-—–]\s*([^\s【】\[\]（）()《》/|,，。！!…~\-—–]{2,16})", title or ""):
        out.append(m.group(1).strip())
    for m in re.finditer(
            r"(?:^|[\s\]\}）】])([^\s【】\[\]（）()《》/|,，。！!…~\-—–：:]{2,16})\s*[-—–]\s*\S",
            title or ""):
        out.append(m.group(1).strip())
    m = _DASH_ARTIST_RX.search(title or "")
    if m:
        out.append(m.group(1).strip())
    cleaned = []
    for a in out:
        a = re.sub(r"^(4K|8K|HD|高清|修复|完整版|官方|MV|PV|歌词版)", "", a, flags=re.I).strip(" -—_·")
        if 2 <= len(a) <= 16 and not _NOT_ARTIST_RX.search(a) and not _FAKE_ARTIST.match(a):
            cleaned.append(a)
    return cleaned


def artist_from_title(title: str, song: str) -> str:
    if not title or not song:
        return ""
    esc = re.escape(song)
    keep = r"[^\s【】\[\]（）()《》/|,，。！!…~\-—–]{2,16}"
    for pat in (
        r"(" + keep + r")\s*《\s*" + esc,
        r"(" + keep + r")\s*[-—–]\s*" + esc,
        r"《" + esc + r"》\s*[-—–]\s*(" + keep + r")",
    ):
        m = re.search(pat, title)
        if m:
            cand = re.sub(r"^(4K|8K|HD|高清|修复|完整版|官方)", "", m.group(1), flags=re.I).strip(" 【】[]()（）-—_")
            if 2 <= len(cand) <= 16 and not _FAKE_ARTIST.match(cand) and not _NOT_ARTIST_RX.search(cand):
                return cand
    return ""


def _artist_mismatch(c: Cand, canonical: str) -> bool:
    names = _artists_in_title(c.title)
    nc = normalize(canonical)
    if not names or not nc:
        return False
    for n in names:
        nn = normalize(n)
        if nn and (nn in nc or nc in nn):
            return False
    return True


def score_of(c: Cand, canonical: str = "", ref_dur: int = 0,
             clip_hit: bool = False) -> float:
    s = 1.0
    s *= _play_floor(c.play)
    s *= _tag_score(c)
    clip = 1.0
    if ref_dur and c.duration:
        clip = max(0.3, min(1.0, c.duration / ref_dur))
    s *= title_score(c.title, c.duration, ref_dur, clip_hit) * clip
    if c.videos > 1:
        s *= 0.8
    s *= _up_score(c, canonical)
    if c.from_music:
        s *= 1.5
    if canonical and _artist_mismatch(c, canonical):
        s *= 0.5
    if c.kind == 1:
        s *= 0.45
    if canonical and normalize(canonical) in normalize(c.title) and _artist_confirmed(c, canonical, ref_dur) >= 2:
        s *= 1.15
    return s


# ---------------------------------------------------------------- 主流程

class _Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _filter_chain(cards: Sequence[Any], keyword: str, *,
                  min_dur: int, max_dur: int, min_play: int,
                  canonical: str = "", ref_dur: int = 0,
                  pool_notes: list[str] | None = None) -> list[Cand]:
    notes = pool_notes if pool_notes is not None else []
    pool = [as_cand(c) for c in cards]

    for c in pool:
        c.rank = match_rank(c.title, keyword)
    pool = [c for c in pool if c.rank > 0]
    if not pool:
        raise _Reject("候选池无正片（标题都不含歌名）")

    pool = [c for c in pool if not TITLE_BLOCK.search(c.title)]
    if not pool:
        raise _Reject("候选池无正片（全是教程/翻唱/解说）")

    pool = [c for c in pool if not _TITLE_INSTR_RX.search(c.title)]
    if not pool:
        raise _Reject("候选池无正片（全是器乐改编）")

    kept: list[Cand] = []
    shells = 0
    for c in pool:
        if shell_of(c.title, keyword, canonical):
            shells += 1
            continue
        kept.append(c)
    if not kept:
        raise _Reject("候选池无正片（全是鬼畜/二创壳）")
    if shells:
        notes.append(f"剔掉 {shells} 条二创壳")
    pool = kept

    pool = [c for c in pool if not _REACTION_RX.search(c.title)]
    if not pool:
        raise _Reject("候选池无正片（全是反应/解说）")

    pool = [c for c in pool if not _KARAOKE_RX.search(c.title)]
    if not pool:
        raise _Reject("候选池无正片（全是伴奏/消音）")

    pool = [c for c in pool if c.duration == 0 or min_dur <= c.duration <= max_dur]
    if not pool:
        raise _Reject(f"候选池无正片（时长都不在 {min_dur}–{max_dur}s）")

    pool = [c for c in pool if c.play >= min_play]
    if not pool:
        raise _Reject(f"候选池无正片（播放量都低于 {min_play}）")

    def _album(c: Cand) -> bool:
        if c.duration and c.duration > _ALBUM_HARD_DUR:
            return True
        if _ALBUM_WORD_RX.search(c.title) and c.duration > _ALBUM_WORD_DUR:
            return True
        if c.videos > 1 and _ALBUM_WORD_RX.search(c.title):
            return True
        return False

    pool = [c for c in pool if not _album(c)]
    if not pool:
        raise _Reject("候选池无正片（全是专辑/合集）")

    if ref_dur:
        clips = [c for c in pool if c.duration and c.duration < ref_dur * _CLIP_RATIO]
        if clips and len(clips) == len(pool):
            raise _Reject("候选池无正片（只剩切片/片段）")
        clip_ids = {id(c) for c in clips}
        pool = [c for c in pool if id(c) not in clip_ids]
        longs = [c for c in pool
                 if c.duration > ref_dur * _LONG_RATIO
                 and (c.duration > 900 or re.search(r"(循环|歌单|串烧|伴奏)", c.title))]
        if longs and len(longs) == len(pool):
            raise _Reject("候选池无正片（只剩循环/串烧）")
        long_ids = {id(c) for c in longs}
        pool = [c for c in pool if id(c) not in long_ids]

    for c in pool:
        c.kind = kind_of(c.title)
    if pool and all(c.kind == 2 for c in pool):
        raise _Reject("候选池无正片（全是翻唱/器乐改编）")

    return pool


def _whitelist_hit(c: Cand, keyword: str) -> bool:
    if c.author not in WHITELIST_UPS:
        return False
    if normalize(keyword) not in normalize(c.title):
        return False
    return not WL_BLOCK_RX.search(c.title)


def pick(
    cards: Sequence[Any],
    keyword: str,
    *,
    min_play: int = 1,
    min_dur: int = 50,
    max_dur: int = 720,
    priority_words: Sequence[str] | None = None,
    canonical: str = "",
    ref_dur: int = 0,
) -> Picked:
    notes: list[str] = []
    try:
        pool = _filter_chain(
            cards, keyword, min_dur=min_dur, max_dur=max_dur, min_play=min_play,
            canonical=canonical, ref_dur=ref_dur, pool_notes=notes,
        )
    except _Reject as e:
        return Picked(None, e.reason, stage="fail")

    clip_hit = bool(ref_dur) and any(
        c.duration and c.duration < ref_dur * 0.8 for c in pool)
    for c in pool:
        c.score = score_of(c, canonical, ref_dur, clip_hit)
    pool.sort(key=lambda c: (c.kind == 0, c.score, c.play), reverse=True)

    words = [str(w).strip() for w in (priority_words or []) if str(w).strip()]

    wl = [c for c in pool if _whitelist_hit(c, keyword)]
    if wl:
        wl.sort(key=lambda c: (c.score, c.play), reverse=True)
        return Picked(wl[0], "白名单 UP 直取", pool=pool, top=pool[:5], stage="ok")

    for w in words:
        try:
            group = [c for c in pool if re.search(w, c.title, re.IGNORECASE)]
        except re.error:
            group = [c for c in pool if w.lower() in c.title.lower()]
        if group:
            return Picked(group[0], f"命中优先词「{w}」", pool=pool, top=pool[:5], stage="ok")

    hq = [c for c in pool
          if c.from_music and re.search(r"(Hi-?Res|无损)", c.title, re.I)
          and c.score >= pool[0].score * 0.25]
    if hq:
        hq.sort(key=lambda c: c.score, reverse=True)
        return Picked(hq[0], "高音质档置顶", pool=pool, top=pool[:5], stage="ok")

    reason = "加权排序第一"
    if len(pool) > 1 and abs(pool[0].score - pool[1].score) < 1e-9:
        reason = "加权排序第一（与第二名并列）"
    if notes:
        reason += "；" + "；".join(notes)
    return Picked(pool[0], reason, pool=pool, top=pool[:5], stage="ok")


def choose_card(
    pool: Sequence[Cand],
    keyword: str,
    *,
    canonical: str = "",
    ref_dur: int = 0,
    max_rounds: int = 3,
) -> tuple[Cand | None, str, str]:
    """§5 作者判定链：最多 3 轮换候选。"""
    if not pool:
        return None, "", "候选池为空"

    cluster_n = 0
    if ref_dur:
        cluster_n = len([c for c in pool if c.duration and abs(c.duration - ref_dur) <= 5])

    for idx, c in enumerate(pool[:max_rounds]):
        if ref_dur and c.duration and c.duration < ref_dur * _CLIP_RATIO and cluster_n >= 2:
            continue
        title_names = _artists_in_title(c.title)
        if canonical:
            nc = normalize(canonical)
            matched = [n for n in title_names if nc and (normalize(n) in nc or nc in normalize(n))]
            if title_names and not matched:
                continue
            if matched:
                return c, canonical, f"标题作者与曲库一致（第 {idx + 1} 候选）"
        elif title_names:
            return c, title_names[0], f"标题作者：{title_names[0]}"

        if ref_dur:
            cluster = [x for x in pool if x.duration and abs(x.duration - ref_dur) <= 5]
            if len(cluster) >= 2:
                cnt: Counter[str] = Counter()
                for x in cluster:
                    for n in _artists_in_title(x.title):
                        cnt[n] += 1
                if cnt:
                    name, times = cnt.most_common(1)[0]
                    if times >= 2:
                        return c, name, f"时长簇众数作者：{name}（{times} 票）"

        a = artist_from_title(c.title, keyword)
        if a:
            return c, a, f"标题结构兜底：{a}"
        return c, (canonical or c.author.strip()), "无作者线索，用候选本身"

    return None, "", "候选全是片段/翻唱，判不出正片"
