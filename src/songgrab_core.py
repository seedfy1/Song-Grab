"""SongGrab 核心库：搜索、选片、下载、转封装、命名、台账。

设计原则：
- 不依赖浏览器；搜索走 B 站公开 web 接口。
- 下载走 yt-dlp（Python API），转封装走 ffmpeg（子进程，静默）。
- 所有外部进程一律 CREATE_NO_WINDOW，避免弹黑框。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import subprocess
import sys
import tempfile
import threading
import unicodedata
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import platform_compat as _pc

# ---------------------------------------------------------------- 路径与常量

__version__ = "1.1.3"


def app_dir() -> Path:
    """程序所在目录（打包后为 exe 同级目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# 让 HTTPS 校验在打包环境里也能找到 CA 证书
def _init_ssl() -> None:
    try:
        import certifi
        ca = certifi.where()
        if os.path.exists(ca):
            os.environ.setdefault("SSL_CERT_FILE", ca)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
    except Exception:
        pass


_init_ssl()

# 外置热更新时由 core_loader 注入 SG_CORE_DIR（指向 exe 所在目录），
# 保证外置文件与内置文件对 APP_DIR 的认知一致。
APP_DIR = Path(os.environ["SG_CORE_DIR"]).resolve() if os.environ.get("SG_CORE_DIR") else app_dir()
FFMPEG = _pc.ffmpeg_path()
ARTISTS_FILE = _pc.artists_file()
SETTINGS_FILE = _pc.settings_file()

SEARCH_API = "https://api.bilibili.com/x/web-interface/wbi/search/type"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

# 默认不设优先词条：选片只按播放量，词条由用户在界面上自行添加（支持正则）
DEFAULT_PRIORITY: list[str] = []

# 非歌曲类标题黑名单
# FIX(H4): 英文词加词边界。原写法 `cover|anim|chart|MAD` 无边界，会把
# Animal / Discovery / Madonna / Made in Japan 这类正经歌名当非歌曲硬过滤掉。
TITLE_BLOCK = re.compile(
    r"(教程|教学|如何|怎么|手书|攻略|实况|通关|记录|谱面|自制谱|全连|首杀|"
    r"理论值|解说|混剪|歌ってみた|翻唱|reaction|合集|十选|"
    r"排行榜|盘点|\bamv\b|\bmad\b|\bchart\b|\banim\b|\banimation\b|\bcover\b)",
    re.IGNORECASE,
)

# 常见繁体/日文汉字 → 简体
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
    # v7：异体字表补全（run100 实测「叶蒨文≠叶倩文」「陳淑樺≠陈淑桦」导致作者
    # 查不到、被同名异曲顶替）。覆盖常见繁体姓氏与易错异体字。
    "陳": "陈", "蒨": "倩", "樺": "桦", "鍾": "钟", "島": "岛", "嶋": "岛",
    "峯": "峰", "嵐": "岚", "邨": "村", "喆": "哲", "儀": "仪", "瑋": "玮",
    "瑩": "莹", "詠": "咏", "銘": "铭", "鎮": "镇", "銳": "锐", "鈴": "铃",
    "鋼": "钢", "鋒": "锋", "鏡": "镜", "鍵": "键", "韻": "韵", "靈": "灵",
    "離": "离", "霧": "雾", "際": "际", "隊": "队", "隨": "随", "險": "险",
    "難": "难", "館": "馆", "駐": "驻", "驗": "验", "騰": "腾", "驚": "惊",
    "驅": "驱", "鳴": "鸣", "鴻": "鸿", "顧": "顾", "顯": "显", "飄": "飘",
    "順": "顺", "須": "须", "頌": "颂", "頑": "顽", "飲": "饮", "餘": "余",
    "鮮": "鲜", "鄧": "邓", "鄭": "郑", "馮": "冯", "蔣": "蒋", "蕭": "萧",
    "趙": "赵", "劉": "刘", "孫": "孙", "許": "许", "盧": "卢", "吳": "吴",
    "張": "张", "楊": "杨", "羅": "罗", "蘇": "苏", "濤": "涛", "濱": "滨",
}

# ---------------------------------------------------------------- 配置

# 配置版本。写盘时带上这个键，将来做一次性迁移时用。
# 用户已确认把默认最低播放量改回 1（原先 H3 曾抬到 10000，导致热门词的正片
# 只有几百播放、全被门槛剔掉，反而只能选到高播放的鬼畜/片段）。
SETTINGS_VERSION = 3

DEFAULT_SETTINGS: dict[str, Any] = {
    "output_dir": "",            # 空字符串 = 桌面
    "min_play": 1,               # 播放量门槛，0 = 不限；默认 1 = 实质不限
    "min_dur": 50,               # 最短时长（秒），短于它的视频不选
    "max_dur": 720,              # 最长时长（秒），长于它的视频不选；0 = 不限
    "priority_words": list(DEFAULT_PRIORITY),
    "use_cookies": False,
    "last_txt_dir": "",
    "output_format": "m4a",      # m4a / mp3，必须列在这里，否则读回时会被过滤掉
    # T3/T4: 远程更新清单地址（留空则不检查更新）。发布方填一个 http(s) 链接，
    # 客户端会去拉 version.json 并校验 SHA256；不在这里列出来的键会被读回时丢弃。
    "update_url": "",
    # L22: B 站曲库作者源（视频 → bgm 标签 → music_id → origin_artist）。
    # 用户定：它是**最高优先级**作者源；取不到才回退 QQ/Apple/网易云。
    "bili_music": True,
    # L36: 曲库作者「只校验不覆盖」。True = 沿用旧行为（曲库最高优先级）；
    # False = 曲库作者不再覆盖 QQ/Apple/网易云，只在它们都取不到时兜底
    #（对应 TODO 待观察项：曲库给「官方原唱」与实际要的发行版本常不是同一人）。
    "bili_music_override": True,
}


def _cfg_value_ok(key: str, value: Any) -> bool:
    """按默认值的类型校验读回的配置；类型不对就丢弃该键、保留默认值。"""
    default = DEFAULT_SETTINGS.get(key)
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, str):
        if key == "output_format":
            return value in ("m4a", "mp3")
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    return True


def load_settings() -> dict[str, Any]:
    cfg = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                # L41: 手改坏的 settings.json（类型不对）按默认值兜底，别把错误类型带进下游
                cfg.update({k: v for k, v in data.items()
                            if k in DEFAULT_SETTINGS and _cfg_value_ok(k, v)})
    except Exception as exc:
        # L42: settings.json 坏了静默回默认，用户以为设置生效；至少留一行痕迹
        _log_swallowed("load_settings", exc)
    return cfg


def save_settings(cfg: dict[str, Any]) -> bool:
    # FIX(M20): 返回是否写盘成功。原实现吞掉异常返回 None，界面于是照样提示
    # 「已保存」，程序目录不可写时用户以为设置生效了，实际没有。
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        out = dict(cfg)
        # FIX(H3): 记录配置版本，供 load_settings 只做一次性迁移，不覆盖用户手改的值。
        out["_v"] = SETTINGS_VERSION
        SETTINGS_FILE.write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except Exception as exc:
        _log_swallowed("save_settings", exc)
        return False


def desktop_dir() -> Path:
    """默认输出目录（Android：公共音乐目录；桌面：XDG）。"""
    return _pc.default_output_dir()


def resolve_output_dir(cfg: dict[str, Any]) -> Path:
    raw = (cfg.get("output_dir") or "").strip()
    # 统一成 Windows 反斜杠：正斜杠会让 os.startfile / ShellExecute 报 WinError 5
    p = Path(os.path.normpath(raw)) if raw else desktop_dir()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def open_in_explorer(folder: Path, select_file: Path | None = None) -> tuple[bool, str]:
    """打开文件夹（Android 走 am start，桌面走文件管理器）。"""
    return _pc.open_folder(Path(str(folder)), select_file)


def dir_writable_detail(p: Path) -> tuple[bool, str]:
    """多策略写测试：任一种方式成功即视为可写。"""
    p = Path(os.path.normpath(str(p)))
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"无法创建目录：{type(e).__name__}: {e}"

    errs: list[str] = []
    for name in (f".sg_test_{os.getpid()}", f"sg_test_{os.getpid()}.tmp"):
        probe = p / name
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            try:
                probe.unlink()
            except Exception:
                pass
            return True, "可写"
        except Exception as e:
            errs.append(f"{name}: {type(e).__name__}: {e}")

    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(p), prefix="sg_", suffix=".tmp")
        os.close(fd)
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return True, "可写（tempfile）"
    except Exception as e:
        errs.append(f"tempfile: {type(e).__name__}: {e}")

    return False, "；".join(errs[:3])


def dir_writable(p: Path) -> bool:
    return dir_writable_detail(p)[0]



CONFIRM_FILE = _pc.data_dir() / "confirm.json"


def load_confirm() -> dict[str, dict[str, Any]]:
    """界面确认结果（§8.6 状态落盘）。

    raw 行 -> {bvid, artist, title, lock, src_artist, t}
    lock=True 表示用户在界面上改过曲师（artist 可能为空 = 主动标「未命名」）。
    """
    try:
        if CONFIRM_FILE.exists():
            data = json.loads(CONFIRM_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        pass
    return {}


def save_confirm(rows: dict[str, dict[str, Any]], keep: int = 500) -> None:
    """落盘确认状态，只保留最近 keep 条，避免文件无限长大。"""
    try:
        items = list(rows.items())
        if len(items) > keep:
            items.sort(key=lambda kv: float(kv[1].get("t") or 0))
            items = items[-keep:]
        CONFIRM_FILE.write_text(
            json.dumps(dict(items), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def clear_confirm() -> None:
    """删掉确认记录（用户点「忘掉已确认的音源」）。"""
    try:
        if CONFIRM_FILE.exists():
            CONFIRM_FILE.unlink()
    except Exception:
        pass


def load_artists() -> dict[str, str]:
    try:
        if ARTISTS_FILE.exists():
            data = json.loads(ARTISTS_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------- 文本处理

def normalize(s: str) -> str:
    """去标点、繁简归一、小写，只保留字母数字与中日文；part/pt 折叠成 pt。"""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    s = "".join(ch for ch in s if ch.isalnum())
    s = "".join(VARIANT.get(ch, ch) for ch in s)
    return s.replace("part", "pt")


TITLE_SPLIT = re.compile(r"[\s【】\[\]（）()『』「」<>《》/／|｜~～\-—_,，。:：;；+]+")

BOOK_MARK = re.compile(r"《([^《》]*)》")

# 引号里的内容多半是歌词 / 引用：「情歌-梁静茹"白云飞走 苍狗与海鸥"」
_QUOTE_PAIRS = re.compile(r"[“\"「『]([^”\"」』]{1,300})[”\"」』]")

# 歌名后面常见、且不改变「这是不是同一首歌」的尾巴：`8102在线听`、`晴天完整版`、`红莲华 4K`
# 只放行「多出的部分完全由这些词拼成」，所以 `いただきバベル` 仍然进不了 `バベル`。
# 注意这里是 normalize() 之后的形式：小写、无标点、繁简已归一。
_TITLE_TAIL_WORDS = ("在线听", "在线看", "在线播放", "完整版", "完整", "官方版", "修正版",
                     "原版", "高清版", "高清", "超清", "无损版", "无损", "高音质",
                     "附歌词", "带歌词", "含歌词", "中文字幕", "字幕版", "字幕",
                     "试听版", "试听", "纯享版", "mv", "pv", "inst", "伴奏版",
                     "4k", "8k", "1080p", "720p", "60帧", "60fps", "hires")
_TAIL_SORTED = tuple(sorted(_TITLE_TAIL_WORDS, key=len, reverse=True))

# ---------------------------------------------------------------- 选片规则 v3
# 2026-11-05 多角色辩论定稿（Proposer/Critic/Judge 三轮收敛）：
# C1 片段时长基准、C2 片段作者纠正、C3 情感号标题、C4 不叠加、C5 空数据回退、C6 众数簇。

# C6：同名曲时长聚类半径（秒）
_ARTIST_CLUSTER_TOL = 5.0
# C1：候选时长短于「这首歌的主流时长」的比例阈值
# 0.6 → 0.65：实测「我们不一样」的综艺片段 170s / 原曲 272s = 0.625 卡在边界上
# 逃过了判定，放宽到 0.65 仍与 15s 容差、±5s 聚类互不冲突。
_CLIP_RATIO = 0.65

# 用户自己写了「翻唱/live/伴奏/钢琴…」这类词时，说明他要的就是那个版本：
# 内容类硬过滤（正片/二创壳/反应/卡拉OK/器乐）全部跳过，只留时长/播放量/片段闸。
_USER_WANT_RX = re.compile(
    r"(翻唱|翻自|cover|live|现场|演唱会|不插电|concert|伴奏|纯音乐|器乐|演奏|钢琴|吉他"
    r"|指弹|古筝|民乐|remix|混音|mashup|串烧|dj|鬼畜|二创|调教|mad|mmd|片段|剪辑|切片"
    r"|短版|合唱|群星|男声|女声|童声|戏腔|升key|降key|升调|降调|变速|加速|减速|慢速"
    r"|8d|环绕|demo|试听|试唱|清唱|ai\s*翻唱|sovits|diffsinger)",
    re.I)
# C3：去书名号后剩余文案含问句/感叹/你我 且长度不短于该值，判情感号
_EMOTION_MIN_LEN = 7
_EMOTION_RX = re.compile(r"[?？!！]|[你我]")


def _duration_mode(mus: list[tuple[str, str, int]]) -> tuple[float, int]:
    """C6：同名曲时长的众数簇 → (代表时长中位数, 簇大小)。

    把库内所有同名曲时长按 ±5 秒贪心归并，取元素最多的簇；簇大小相同时取
    时长更长的那簇（原唱/完整版通常比翻唱长）。没有可用时长时返回 (0.0, 0)。
    """
    durs = sorted(float(d) for _a, _s, d in mus if float(d or 0) > 0)
    if not durs:
        return 0.0, 0
    clusters: list[list[float]] = []
    for d in durs:
        if clusters and d - clusters[-1][-1] <= _ARTIST_CLUSTER_TOL:
            clusters[-1].append(d)
        else:
            clusters.append([d])
    best = max(clusters, key=lambda c: (len(c), c[-1]))
    return best[len(best) // 2], len(best)


def _mode_cluster(mus: list[tuple[str, str, int]],
                  mode_dur: float) -> list[tuple[str, str, int]]:
    """众数簇内的候选：代表「这首歌真实有多长」的基准集合。"""
    if mode_dur <= 0:
        return []
    return [x for x in mus
            if float(x[2] or 0) > 0 and abs(float(x[2]) - mode_dur) <= _ARTIST_CLUSTER_TOL]


def _clip_factor(duration: int, ref_dur: float) -> float:
    """C1：候选视频时长 < 众数簇时长 × 0.6 → 0.4；否则 1.0。"""
    if ref_dur <= 0 or not duration:
        return 1.0
    return 0.4 if duration < ref_dur * _CLIP_RATIO else 1.0


def _play_floor(c: Card) -> float:
    """v8：播放量不再作分数基数，只作「没人看的搬运」下限防呆。

    用户要求「所有候选初始分统一为 1 再加权」，播放量不能再线性进分数
    （6,490 万 vs 1,579 是 4 万倍，任何系数都压不住）。但完全不看会出现
    「1,579 播放的冷门搬运压过 649 万官方 4K 版」，所以只保留一个很浅的下限档：
    高播放**不再额外奖励**（否则播放量又变回主导），只有极低播放才轻微打折。
    """
    p = int(getattr(c, "play", 0) or 0)
    if p <= 0:
        return 1.0          # 直链/搜索兜底拿不到播放量，不罚
    if p < 1000:
        return 0.45
    if p < 10000:
        return 0.65
    return 1.0


def _emotion_title(title: str) -> bool:
    """C3：去书名号及其内容后，剩余文案含「？/！/我/你」、长度≥7、且不含后缀白名单词。

    `《反方向的钟》你 为什么不爱我` → 剩余「你 为什么不爱我」命中；
    `《晴天》周杰伦 2003 叶惠美专辑` → 剩余无你我问叹，不命中（不误杀正经长标题）。
    """
    # v8：选片基数改成 1 之后，这个 0.6 的降权不再被播放量稀释，会把官方修复版
    # 一棍子打死——「【4K60FPS】周杰伦《告白气球》超甜神曲！祝大家七夕快乐！」
    # 649 万播放被判情感号（分 1.215），输给 1,579 播放的冷门搬运。带正式信号
    # （官方/修复/4K/FPS/MV/高清）的标题一律不算情感号。
    t = str(title or "")
    tl = t.lower()
    if re.search(r"(?<![a-z])(mv|4k|8k|fps|1080p|2160p|hi-?res)(?![a-z])", tl) \
            or any(w in t for w in ("官方", "修复", "超清", "高清", "原版", "完整版", "无损")):
        return False
    rest = re.sub(r"《[^》]*》", " ", t)
    rest = _TAG_RX.sub(" ", rest)
    rest = re.sub(r"\s+", "", rest).strip("-_|·— ")
    if len(rest) < _EMOTION_MIN_LEN or not _EMOTION_RX.search(rest):
        return False
    nr = normalize(rest)
    return not any(w and w in nr for w in _TAIL_SORTED)


def _mode_artist(mode: list[tuple[str, str, int]],
                 song: str) -> tuple[str, str] | None:
    """众数簇里出现次数最多的作者（多源交叉验证），避免簇首恰好是翻唱者。"""
    if not mode:
        return None
    cnt: dict[str, int] = {}
    for a, _s, _d in mode:
        cnt[normalize(a)] = cnt.get(normalize(a), 0) + 1
    best = max(cnt, key=lambda k: cnt[k])
    for a, s, _d in mode:
        if normalize(a) == best and artist_ok(song, a):
            return a, s
    return None


def _clip_base(mus: list[tuple[str, str, int]]) -> float:
    """v5 R4：片段判定的基准时长 = 众数簇内时长**降序第 2 名**。

    用第 2 名而不是最长成员：库里的最长那条经常是 DJ remix / 加长版
    （我们不一样的 304s 是 DJ泪海 remix，原唱大壮只有 270s），拿它当分母
    会把 170s 的综艺切片算成 0.56「合格」。簇内只有 1 条时退回该条时长。
    """
    ref, n = _duration_mode(mus)
    if n <= 0 or ref <= 0:
        return 0.0
    mode = _mode_cluster(mus, ref)
    durs = sorted((float(d) for _a, _s, d in mode if float(d or 0) > 0), reverse=True)
    if not durs:
        return 0.0
    return durs[0] if len(durs) == 1 else durs[1]


def _singer_in_title(title: str) -> str:
    """v5 R3：「听完香菜唱《告白气球》」→「香菜」（剥掉「听完」这类引导动词）。"""
    m = _SING_RX.search(str(title or ""))
    if not m:
        return ""
    who = m.group(1)
    for w in _SING_LEAD:
        if who.startswith(w) and len(who) > len(w):
            who = who[len(w):]
            break
    return who.strip()


def _cover_kind(card: Card, known: list[str] | None = None) -> int:
    """v5 R2/R3：0=正常 1=演奏者（软降权） 2=翻唱（排序靠后）。

    80 首实测里播放量最高的几条错选都卡在这里：Believer 的 UP 主叫
    「儿童合唱团OneVoice」(777万)、告白气球的标题写「听完香菜唱《告白气球》」
    (814万)、only my railgun 的 UP 主是 Animenzzz 钢琴改编(543万)。
    """
    up = normalize(card.author or "")
    ttl = normalize(card.title or "")
    for w in _UP_COVER:
        if w in up:
            return 2
    who = _singer_in_title(card.title)
    if who:
        wn = normalize(who)
        names = [normalize(a) for a in (known or []) if a]
        hit = any(wn and (wn in a or a in wn) for a in names if a)
        if not hit:
            return 2
    if _COVER_VER_RX.search(str(card.title or "")):
        return 2
    for w in _UP_PLAYER:
        if w in up:
            # R2 保护条件：UP 主就是官方/搬运原版时不降权
            if any(g in ttl or g in up for g in _OFFICIAL_HINT):
                break
            return 1
    return 0


# v6 R7：`【X】歌名` 这种二创壳。X 是二创人物名（李云龙/马冬梅/布施明/五五开），
# 不是歌手也不是技术修饰词，播放量往往比原唱高一个数量级，只降权压不住。
_SHELL_TECH = (
    "4k", "8k", "60fps", "1080", "720", "hi-res", "hires", "无损", "高清", "超清",
    "修复", "官方", "原版", "本家", "完整版", "纯享", "现场", "live", "mv", "pv",
    "中字", "字幕", "双语", "钢琴", "古筝", "吉他", "小提琴", "二胡", "演奏",
)


def _shell_of(title: str, song: str, known: list[str] | None = None) -> bool:
    """v6 R7：标题以 `【X】` 开头、X 既不是歌名/技术词/曲库作者名 → 判为二创壳。

    `【李云龙】出山` / `【马冬梅】Havana` / `【五五开】出山` → True；
    `【生僻字】元素周期表`（X 就是歌名）、`【4K60FPS】周杰伦《七里香》`（技术词）、
    `【周杰伦】晴天`（曲库作者名）→ False，不误杀正片。
    """
    m = re.match(r"^[【\[]\s*(?P<x>[^】\]]{1,14})\s*[】\]]", str(title or "").strip())
    if not m:
        return False
    x = normalize(m.group("x"))
    if not x:
        return False
    ks = normalize(song)
    if ks and (x == ks or ks in x):
        return False
    if any(t in x for t in _SHELL_TECH):
        return False
    for a in (known or []):
        an = normalize(a)
        if an and len(an) >= 2 and (an in x or x in an):
            return False
    return True


def _quoted_texts(title: str) -> list[str]:
    return _QUOTE_PAIRS.findall(str(title))


# 【】[]（）() 这类是标签/后缀（`【MV】`、`（完整版）`），不算标题正文
_BRACKET_RX = re.compile(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)|『[^』]*』|<[^>]*>")

# FIX(M1): 判「拉丁歌名是否被标签粘连」时只剥【】[]（）『』。半角 () 常是歌名的一部分
# （`Luv(sic) Part 3`），剥掉会把整标题和歌名改得不一样，导致正常命中被误杀。
_TAG_RX = re.compile(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|『[^』]*』")


def _only_in_quotes(title: str, key: str) -> bool:
    """歌名只出现在引号内、引号外没有 → 是别的歌的歌词，不算命中。"""
    texts = _quoted_texts(title)
    if not texts:
        return False
    # FIX(日文歌名「」引用): 「作者「歌名」标签」是正片标准格式——BUMP OF CHICKEN
    # 「天体観測」PV、米津玄師「Lemon」MV 里，歌名被「」框住是日文歌名的引用写法，
    # 不是歌词引用。旧逻辑把「引号内含 key 且引号外有字」一律判负，这类正片全被挡掉
    # （天体観測 正片 match_rank=0，只剩裸写歌名的翻奏胜出）。只有当引号内是**比歌名
    # 更长的歌词句**（key 只是其中一部分，如「白云飞走 苍狗与海鸥」里的「苍狗与海鸥」）
    # 时才可能判负；引号内恰好等于歌名本身 → 放行。
    if any(normalize(t) == key for t in texts):
        return False
    if not any(key in normalize(t) for t in texts):
        return False
    outside = _BRACKET_RX.sub(" ", _QUOTE_PAIRS.sub(" ", str(title)))
    # FIX(M2): 引号外必须还有「实质正文」才判负。原实现连 `「バベル」【MV】` 这种
    # 「引号内就是歌名本身、引号外只剩标签」的标题也判负，正片被自己的规则挡掉。
    outside_key = normalize(outside)
    if key in outside_key:
        return False
    return len(outside_key) >= 3


def book_matched(title: str, keyword: str) -> bool:
    """标题里出现《歌名》，且书名号内与歌名完全相等。"""
    k = normalize(keyword)
    if not k:
        return False
    for inner in BOOK_MARK.findall(str(title)):
        if normalize(inner) == k:
            return True
    return False


def title_tokens(title: str) -> list[str]:
    return [t for t in TITLE_SPLIT.split(str(title)) if t]


def match_rank(title: str, keyword: str) -> int:
    """0 不匹配；1 普通匹配；2 书名号里整段完全相等（只是更强的匹配证据，不再据此优先）。"""
    k = normalize(keyword)
    if not k:
        return 0
    if book_matched(title, keyword):
        return 2
    if _plain_matched(title, keyword):
        return 1
    return 0


def title_matched(title: str, keyword: str) -> bool:
    """标题是否真的对得上歌名（挡住同名不同曲）。"""
    return match_rank(title, keyword) > 0


def _tail_only(extra: str) -> bool:
    """多出的部分是否完全由 `_TITLE_TAIL_WORDS` 拼成（允许叠加，如 `4k高清`）。

    只放白名单尾巴，不整体放宽字符数：这样 `8102在线听` 能命中 `8102`，
    而 `いただきバベル` 仍然不会命中 `バベル`。
    """
    rest = str(extra or "")
    if not rest:
        return False
    while rest:
        for w in _TAIL_SORTED:
            if rest.startswith(w):
                rest = rest[len(w):]
                break
        else:
            return False
    return True


def _ascii_alnum(ch: str) -> bool:
    """是否是 ASCII 字母/数字（中文不算，中英混排时不该阻断匹配）。"""
    return bool(ch) and ch.isascii() and ch.isalnum()


def _name_in_title(title: str, name: str) -> bool:
    """作者名是否作为「独立段」出现在标题里（前后是分隔符/边界，而非紧贴字）。

    run100 缺陷 1：`by_title` 原来用 `normalize(a) in normalize(title)` 裸子串，
    「泰坦尼克号」里的「尼克」被当成作者命中，而标题里明写的正牌歌手「席琳·迪翁」
    反而被漏掉。这里改成：从标题每个位置起，向右贪心找到 normalize 后等于作者名的
    最短原文段（兼容繁简、·、- 等会被 normalize 去掉的字符），再校验该段的真实
    前后边界——前后若是边界或非字母数字（空格/书名号/【】/破折号等）才算独立成段；
    前后紧贴汉字/假名/字母数字（如「坦-尼克-号」）则判不独立。
    """
    t = str(title or "")
    n = str(name or "").strip()
    if not n:
        return False
    nn = normalize(n)
    if not nn:
        return False
    if nn not in normalize(t):
        return False
    for i in range(len(t)):
        # 跳过会映射成空的前缀（如「·」「-」），从真正命中作者名首字符的位置开始
        if not normalize(t[i]):
            continue
        j = i + 1
        while j <= len(t) and normalize(t[i:j]) != nn:
            j += 1
        if j > len(t):
            continue
        before = t[i - 1] if i > 0 else ""
        after = t[j] if j < len(t) else ""
        # FIX(缺陷:中文作者名前边界): 原用 `.isalnum()`，中文汉字 isalnum()=True，
        # 「大声听孙燕姿《我怀念的》」里「听」被当粘连 → 中文作者名几乎永远匹配不上，
        # by_title 落空后落到 qq/netease 返回的翻唱者（我怀念的→AKA_wen酱、红豆→方大同）。
        # 前边界放宽为「ASCII 字母数字才阻断」：汉字动词/修饰词（听/唱/的/让）不算粘连；
        # 后边界仍用 isalnum（汉字也阻断），拦住「泰坦尼克号」里「尼克」后跟「号」的误嵌。
        if (not before or not _ascii_alnum(before)) and (not after or not after.isalnum()):
            return True
    return False


def _plain_matched(title: str, keyword: str) -> bool:
    k = normalize(keyword)
    if not k:
        return False
    # 歌名只出现在引号里 = 别的歌的歌词，直接判负
    if _only_in_quotes(title, k):
        return False
    # 歌词句拼接（青春的上游/白云飞走/苍狗与海鸥）= 别的歌的歌词，判负
    if lyric_like(title):
        return False
    # 标题里挂了《别的歌名》：说明这是另一首歌的视频（如「白云飞走…《情歌》」）
    marks = BOOK_MARK.findall(str(title))
    if marks and not any(normalize(m) == k for m in marks):
        return False
    # 多词拉丁歌名（含空格）：整标题包含即可，避免被空格切碎
    if re.search(r"\s", str(keyword).strip()):
        # FIX(M1): 只做子串包含不够——`Bad Apples`、`Not Bad Apple` 都会命中
        # `Bad Apple`。要求命中处不与相邻 ASCII 字母数字粘连（中文邻接不影响）。
        # 注意 normalize 会把【】这类标签整块去掉，标签文字粘到歌名上
        # （`【MV】Bad Apple` → `mvbad apple`），所以先把标签块换成空格再归一化。
        # 只剥【】[]（）『』，不剥半角 () —— `Luv(sic) Part 3` 的括号是歌名的一部分。
        nt = normalize(_TAG_RX.sub(" ", str(title)))
        at = nt.find(k)
        while at >= 0:
            before = nt[at - 1] if at > 0 else ""
            after = nt[at + len(k)] if at + len(k) < len(nt) else ""
            if not (_ascii_alnum(before) or _ascii_alnum(after)):
                return True
            at = nt.find(k, at + 1)
    for tok in title_tokens(title):
        n = normalize(tok)
        if not n:
            continue
        if n == k:
            return True
        if k not in n:
            continue
        extra = n.replace(k, "", 1)
        if extra and not any(ch.isalnum() for ch in extra) and len(n) - len(k) <= 2:
            return True
        if _tail_only(extra):
            return True
    return False


def parse_duration(text: str) -> int:
    """'3:10' / '1:02:03' → 秒。"""
    parts = [p for p in str(text).split(":") if p.strip().isdigit()]
    if not parts:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


_FILENAME_MAP = str.maketrans({
    "\\": "＼", "/": "／", ":": "：", "*": "＊", "?": "？",
    '"': "＂", "<": "＜", ">": "＞", "|": "｜",
})


# FIX(L7): Windows 保留设备名。`CON.m4a`、`PRN.m4a` 这类文件名在资源管理器里
# 建不出来，下载到这里会失败，且报错信息极难理解。
_WIN_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)
}


def sanitize_filename(name: str, max_bytes: int = 120) -> str:
    # Windows 非法字符换成全角等价字符，保留可读性（`Re:birth` → `Re：birth`）
    name = "".join(ch for ch in str(name) if ord(ch) >= 32).translate(_FILENAME_MAP)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    raw = name.encode("utf-8")
    if len(raw) > max_bytes:
        # FIX(H8): 截断时先保住 `+`（歌名/作者分隔符）。原实现整体掐断，
        # 长歌名的成品名会丢掉 `+`，而判重正是按 `歌名+` 前缀找的，于是永远找不到已下好的文件。
        if "+" in name:
            head, _, tail = name.partition("+")
            room = max(16, max_bytes - len(tail.encode("utf-8")) - 1)
            head = head.encode("utf-8")[:room].decode("utf-8", "ignore")
            name = f"{head}+{tail}"
            raw = name.encode("utf-8")
            if len(raw) > max_bytes:  # 作者名本身超长，只能整体截断
                name = raw[:max_bytes].decode("utf-8", "ignore").rstrip(". ")
        else:
            name = raw[:max_bytes].decode("utf-8", "ignore").rstrip(". ")
    # FIX(L7): 命中保留设备名时加前导下划线，避免建文件失败
    if name.split(".")[0].strip().lower() in _WIN_RESERVED:
        name = "_" + name
    return name or "unnamed"


def file_stem_ok(stem: str, taken: set[str]) -> bool:
    return stem not in taken


# ---------------------------------------------------------------- 搜索

@dataclass
class Card:
    bvid: str
    title: str
    play: int
    duration: int
    author: str
    from_music: bool = False
    tags: str = ""
    videos: int = 1          # 稿件分P数：整张专辑/合集常是多P，单曲是 1P

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


COOKIE_TXT = APP_DIR / "cookies.txt"
COOKIE_JSON = APP_DIR / "cookies.json"


def parse_cookie_text(text: str) -> dict[str, str]:
    """解析 cookies.txt。

    兼容三种常见导出格式：
    - Netscape 七列（`# Netscape HTTP Cookie File`，浏览器插件标准导出）
    - `#HttpOnly_` 前缀行
    - `name=value; path=/; domain=.bilibili.com` 或纯 `name=value`
    只保留 bilibili 域的键值，返回 {name: value}。
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        if line.lower().startswith("set-cookie:"):
            line = line.split(":", 1)[1].strip()
        parts = line.split("\t")
        if len(parts) >= 7:
            domain, name, value = parts[0].lower(), parts[5].strip(), parts[6].strip()
            if name and "bilibili" in domain:
                out[name] = value
            continue
        seg = line.split(";", 1)[0]
        if "=" not in seg:
            continue
        name, value = seg.split("=", 1)
        name, value = name.strip(), value.strip()
        if name and not name.startswith("#"):
            out[name] = value
    return out


def load_cookies() -> dict[str, str]:
    """读取已导入的 Cookie：优先 cookies.txt，其次兼容旧版 cookies.json。"""
    if COOKIE_TXT.exists():
        try:
            pairs = parse_cookie_text(COOKIE_TXT.read_text(encoding="utf-8-sig", errors="ignore"))
            if pairs:
                return pairs
        except Exception:
            pass
    try:
        if COOKIE_JSON.exists():
            data = json.loads(COOKIE_JSON.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and data:
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def save_cookie_text(text: str) -> tuple[int, bool]:
    """把用户选中的 cookies.txt 解析并规范化保存到程序目录，返回 (条数, 是否含 SESSDATA)。

    写不进去（例如程序放在 Program Files）时抛 OSError，由界面提示以管理员运行。
    """
    pairs = parse_cookie_text(text)
    if not pairs:
        raise ValueError("没有从文件里解析到任何 Cookie，请确认导出的是 Netscape 格式的 cookies.txt")
    lines = ["# Netscape HTTP Cookie File", "# 由肥鱼音乐导入", ""]
    for name, value in pairs.items():
        lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
    # FIX(L31): 走 _DISK_LOCK。扫码成功与「导入 Cookie」都会写这个文件，下载线程
    # 同时可能在读它；不加锁时并发写会出现半截文件。
    with _DISK_LOCK:
        COOKIE_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(pairs), "SESSDATA" in pairs


# ---------------------------------------------------------------- 扫码登录（L30）
QR_GEN_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_COOKIE_KEEP = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3")


def qr_login_start() -> tuple[str, str]:
    """L30：申请「扫码登录」二维码，返回 (手机扫码后打开的网址, qrcode_key)。"""
    data = _http_json(QR_GEN_API, None, timeout=10, retries=1, sign=False,
                      referer="https://www.bilibili.com/")
    if int(data.get("code", -1)) != 0:
        raise RuntimeError(str(data.get("message") or "申请二维码失败"))
    d = data.get("data") or {}
    url, key = str(d.get("url") or ""), str(d.get("qrcode_key") or "")
    if not url or not key:
        raise RuntimeError("接口没有返回二维码内容")
    return url, key


def qr_login_poll(qrcode_key: str) -> tuple[int, str, dict[str, str]]:
    """L30：查询扫码状态，返回 (状态码, 提示, cookies)。

    状态码：0 成功 / 86101 未扫码 / 86090 已扫码待确认 / 86038 已过期 / -1 网络异常。
    成功时 cookies 可直接交给 save_cookie_text 写盘（含 SESSDATA）。
    """
    full = f"{QR_POLL_API}?qrcode_key={urllib.parse.quote(qrcode_key)}"
    req = urllib.request.Request(full, headers={
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    })
    try:
        # FIX(L31): 这里不能用全局 _OPENER —— 它的 CookieJar 同时被搜索线程
        # （_http_json/_warm_up/_expand_short）读写，UI 线程与工作线程并发遍历会
        # 让 jar 抛 "dictionary changed size during iteration"。扫码轮询不需要
        # 登录态，用一次性独立 opener 彻底隔离。
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with opener.open(req, timeout=10) as r:
            raw = r.read().decode("utf-8", "ignore")
            set_cookies = r.headers.get_all("Set-Cookie") or []
    except Exception as exc:
        return -1, f"网络异常：{exc}", {}
    try:
        data = json.loads(raw)
    except Exception:
        return -1, "接口返回了无法解析的内容", {}
    d = data.get("data") or {}
    # FIX(L31): 接口偶尔把 code 返成 null，int(None) 抛 TypeError 会一路冒到 Tk
    # 回调，让 after 轮询链静默中断。取不到有效整数就按 -1（网络异常）处理。
    try:
        code = int(d.get("code", data.get("code", -1)))
    except (TypeError, ValueError):
        code = -1
    msg = str(d.get("message") or data.get("message") or "")
    if code != 0:
        return code, msg, {}
    cookies: dict[str, str] = {}
    login_url = str(d.get("url") or "")
    try:
        for name, value in urllib.parse.parse_qsl(urllib.parse.urlsplit(login_url).query):
            if name in _COOKIE_KEEP or name.startswith("DedeUserID"):
                cookies[name] = value
    except Exception:
        pass
    for sc in set_cookies:
        seg = sc.split(";", 1)[0]
        if "=" not in seg:
            continue
        name, value = seg.split("=", 1)
        name = name.strip()
        if name in _COOKIE_KEEP or name.startswith("DedeUserID"):
            cookies[name] = value.strip()
    return 0, "登录成功", cookies


_COOKIE_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIE_JAR))
_WARMED = False
_MIN_INTERVAL = 1.2
_SEARCH_CACHE: dict[tuple, tuple[float, list]] = {}
_CACHE_TTL = 300

# L41: 三处进程内缓存原来只写不淘汰，长跑时单调增长；给上限、按插入顺序淘汰。
_CACHE_LIMIT_SEARCH = 256
_CACHE_LIMIT_INDEX = 16
_CACHE_LIMIT_MUSIC = 512

# L44: known_bad 降权命中去重（仅用于流水埋点，不参与判定）
_BAD_HIT_SEEN: set[tuple[str, str]] = set()


def _cache_put(cache: dict, key: Any, value: Any, limit: int) -> None:
    """写入并保持不超过 limit 条；dict 保插入序，超了淘汰最早写入的。"""
    cache[key] = value
    if len(cache) > limit:
        for k in list(cache)[: len(cache) - limit]:
            cache.pop(k, None)

# ---- L23 搜索并发：默认 4 路，被限流自动降 2 路、再降 1 路；连续成功且冷却到点后回升 ----
# 每条「路」仍保持 _MIN_INTERVAL 的间隔，只是允许 N 条路同时各发各的。
_LANE_LEVELS = (4, 2, 1)
_LANES = 4
_LANE_COND = threading.Condition(threading.Lock())
_LANE_NEXT: list[float] = [0.0, 0.0, 0.0, 0.0]
_RISK_STREAK = 0
_OK_STREAK = 0
_LAST_RISK_TS = 0.0
_LAST_DOWN_TS = 0.0
_RISK_WINDOW = 60.0        # 秒：两次限流间隔超过这个值，视为新一轮
_RISK_TRIGGER = 2          # 同一轮里累计几次限流就降一级
_OK_TO_UPGRADE = 20        # 连续成功多少次考虑升一级
_UPGRADE_COOLDOWN = 120.0  # 秒：刚降过级，至少稳这么久才回升
_WARM_LOCK = threading.Lock()
_KEY_LOCK = threading.RLock()   # 可重入：取密钥时若再走签名逻辑也不会自锁

# ---- L24 批量并行：catalog.json / compare.json / artists.json 都是「读全量→改→原子写」，
# 多线程同时进来会互相覆盖（后写的把先写的记录整段冲掉）。所有这类写盘统一走这把锁。
_DISK_LOCK = threading.RLock()

# L41: 关键路径（写盘/台账/续传）吞掉的异常留一行痕迹；
# 同一位置每进程只记一次，避免日志刷屏；写日志本身失败绝不再抛。
_LOG_ONCE: set[str] = set()
_LOG_ONCE_LOCK = threading.Lock()


def _log_swallowed(where: str, exc: BaseException | None = None) -> None:
    try:
        key = "%s:%s" % (where, type(exc).__name__ if exc is not None else "-")
        with _LOG_ONCE_LOCK:
            if key in _LOG_ONCE:
                return
            _LOG_ONCE.add(key)
        line = "%s [%s] %s: %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), where,
            type(exc).__name__ if exc is not None else "Exception", exc)
        with open(APP_DIR / "debug.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def search_concurrency() -> int:
    """当前实际并发路数（1/2/4），GUI 或测试可读。"""
    return _LANES


def _reset_search_concurrency() -> None:
    """测试用：恢复默认 4 路并清空统计。"""
    global _LANES, _RISK_STREAK, _OK_STREAK, _LAST_RISK_TS, _LAST_DOWN_TS
    with _LANE_COND:
        _LANES = _LANE_LEVELS[0]
        _RISK_STREAK = 0
        _OK_STREAK = 0
        _LAST_RISK_TS = 0.0
        _LAST_DOWN_TS = 0.0
        _LANE_NEXT[:] = [0.0, 0.0, 0.0, 0.0]
        _LANE_COND.notify_all()


def _note_limited(force: bool = False) -> None:
    """记一次限流；同一窗口内连续两次就降一级（4→2→1）。

    L24：本机到 CDN 的连接被拒（WinError 10061）不是 B 站风控，但同样是
    「并发过高」的明确信号，download_audio 会传 force=True 立刻降一级，
    免得四个 yt-dlp 一直硬撞、每首都退避重试到超时。
    """
    global _LANES, _RISK_STREAK, _OK_STREAK, _LAST_RISK_TS, _LAST_DOWN_TS
    with _LANE_COND:
        now = time.time()
        _OK_STREAK = 0
        if now - _LAST_RISK_TS > _RISK_WINDOW:
            _RISK_STREAK = 1
        else:
            _RISK_STREAK += 1
        _LAST_RISK_TS = now
        if (force or _RISK_STREAK >= _RISK_TRIGGER) and _LANES > _LANE_LEVELS[-1]:
            _LANES = _LANE_LEVELS[min(_LANE_LEVELS.index(_LANES) + 1, len(_LANE_LEVELS) - 1)]
            _RISK_STREAK = 0
            _LAST_DOWN_TS = now
            _LANE_COND.notify_all()


_CONN_ERR_KEYS = (
    "10061", "10054", "10060", "connection refused", "connection reset",
    "connection aborted", "积极拒绝", "远程主机强迫关闭",
)


def _is_conn_error(exc: BaseException) -> bool:
    """L24: 判断是不是「本机连不上 CDN」这类传输层错误（不是 B 站风控）。"""
    msg = str(exc).lower()
    return any(k in msg for k in _CONN_ERR_KEYS)


def _note_ok() -> None:
    """记一次成功；稳定一段时间后允许回升一级。"""
    global _LANES, _OK_STREAK
    with _LANE_COND:
        if _LANES >= _LANE_LEVELS[0]:
            _OK_STREAK = 0
            return
        _OK_STREAK += 1
        if (_OK_STREAK >= _OK_TO_UPGRADE
                and time.time() - _LAST_DOWN_TS >= _UPGRADE_COOLDOWN):
            _LANES = _LANE_LEVELS[max(_LANE_LEVELS.index(_LANES) - 1, 0)]
            _OK_STREAK = 0
            _LANE_COND.notify_all()


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {
        "User-Agent": UA,
        "Referer": "https://search.bilibili.com/",
        "Origin": "https://search.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }
    pairs = load_cookies()
    merged: dict[str, str] = {}
    # FIX(M3): 把 CookieJar 里预热拿到的 cookie（buvid3 等）合并进来。原实现手写
    # Cookie 头会整条覆盖 opener 自动附带的 cookie，`_warm_up` 等于白做，风控照样触发。
    try:
        for c in _COOKIE_JAR:
            if c.name and c.value:
                merged[c.name] = c.value
    except Exception:
        pass
    if pairs:
        merged.update(pairs)
    if merged:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in merged.items())
    if extra:
        h.update(extra)
    return h


# ---------------------------------------------------------------- WBI 签名
# 2026-10-29 补：wbi 搜索不带 w_rid 只会拿到 v_voucher、结果恒为空。
_WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5,
    49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55,
    40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57,
    62, 11, 36, 20, 34, 44, 52,
]
_WBI_KEY_CACHE: tuple[float, str] = (0.0, "")
_WBI_TTL = 6 * 3600
NAV_API = "https://api.bilibili.com/x/web-interface/nav"


def _wbi_mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in _WBI_TAB if i < len(raw))[:32]


def _wbi_key(force: bool = False) -> str:
    """取（并缓存）当前 WBI mixin key；拿不到就抛错，由上层兜底。"""
    global _WBI_KEY_CACHE
    ts, key = _WBI_KEY_CACHE
    if not force and key and time.time() - ts < _WBI_TTL:
        return key
    # L23：多线程并发取密钥会打满 nav 接口，这里串行化并双检
    with _KEY_LOCK:
        ts, key = _WBI_KEY_CACHE
        if not force and key and time.time() - ts < _WBI_TTL:
            return key
        data = _http_json(NAV_API, retries=2, sign=False)
        wbi = (data.get("data") or {}).get("wbi_img") or {}
        img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise RuntimeError("拿不到 WBI 密钥")
        key = _wbi_mixin_key(img, sub)
        _WBI_KEY_CACHE = (time.time(), key)
        return key


_LOGIN_CACHE: dict[str, Any] = {"ts": 0.0, "ok": False, "name": "", "code": -1, "msg": ""}
_LOGIN_TTL = 300


def bili_login_state(force: bool = False) -> dict[str, Any]:
    """L20: 检测 B 站登录态，用于「登录后作者名更准确」提示与风控解释。

    返回 {"ok": bool, "name": str, "code": int, "msg": str}；结果缓存 5 分钟，
    避免每首歌都打一次 nav 接口。未导入 cookie 时 ok=False，属正常情况。
    """
    global _LOGIN_CACHE
    now = time.time()
    if (not force and _LOGIN_CACHE.get("ts")
            and now - float(_LOGIN_CACHE["ts"]) < _LOGIN_TTL):
        return dict(_LOGIN_CACHE)
    res: dict[str, Any] = {"ts": now, "ok": False, "name": "", "code": -1, "msg": "未检测"}
    failed = False
    try:
        data = _http_json(NAV_API, retries=1, sign=False)
        res["code"] = int(data.get("code") or 0)
        if res["code"] == 0:
            d = data.get("data") or {}
            res["ok"] = bool(d.get("isLogin"))
            res["name"] = str(d.get("uname") or "")
            res["msg"] = "已登录" if res["ok"] else "未登录"
        else:
            res["msg"] = str(data.get("message") or "")
    except Exception as exc:
        failed = True
        res["msg"] = f"{type(exc).__name__}: {exc}"
    # FIX(L25/B5): 异常结果不写缓存。nav 抖一次就被缓存 5 分钟，会让界面一直
    # 显示「未登录 B 站」，即使 cookie 完全正常；失败就让下次调用重新探测。
    if not failed:
        _LOGIN_CACHE = res
    return dict(res)


def _wbi_sign(params: dict[str, Any]) -> str:
    """参数排序 + wts + md5(排序查询串 + mixin key) → 带 w_rid 的查询串。"""
    p = {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}
    p["wts"] = str(int(time.time()))
    p = dict(sorted(p.items()))
    query = urllib.parse.urlencode(p, quote_via=urllib.parse.quote)
    p["w_rid"] = hashlib.md5((query + _wbi_key()).encode("utf-8")).hexdigest()
    return urllib.parse.urlencode(p, quote_via=urllib.parse.quote)


def _warm_up() -> None:
    global _WARMED
    if _WARMED:
        return
    # L23：多线程会同时进来，加锁避免 4 条路各发一次预热请求
    with _WARM_LOCK:
        if _WARMED:
            return
        try:
            req = urllib.request.Request("https://www.bilibili.com/", headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            with _OPENER.open(req, timeout=15) as r:
                r.read(2048)
            _WARMED = True
        except Exception:
            pass


def _throttle() -> None:
    """L23：从「全局串行」改成「N 条路各自限速」。

    每条路仍保证两次请求间隔 >= _MIN_INTERVAL，但 N 条路可以同时跑，
    所以 4 首同时搜索的吞吐约为原来的 4 倍；被风控降级后自动变成 2 条、1 条。
    """
    with _LANE_COND:
        lanes = _LANES
        if len(_LANE_NEXT) < lanes:
            _LANE_NEXT.extend([0.0] * (lanes - len(_LANE_NEXT)))
        while True:
            now = time.time()
            idx = min(range(lanes), key=lambda i: _LANE_NEXT[i])
            wait = _LANE_NEXT[idx] - now
            if wait <= 0:
                _LANE_NEXT[idx] = now + _MIN_INTERVAL
                return
            # 超时兜底：并发数变化时立刻重算，不用等满
            _LANE_COND.wait(min(wait, 0.5))


def _http_json(url: str, params: dict[str, Any] | None = None,
               timeout: int = 15, retries: int = 3, sign: bool = True,
               referer: str | None = None) -> dict[str, Any]:
    global _WARMED, _WBI_KEY_CACHE
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            _warm_up()
            _throttle()
            # FIX(L4): WBI 签名里还会发一次 nav 请求，原来这段在 try 之外——
            # nav 超时/412 会直接冒出，不走下面的退避重试。放进 try 统一兜住。
            if params is None:
                full = url
            else:
                query = _wbi_sign(params) if sign else urllib.parse.urlencode(params)
                full = f"{url}?{query}"
            # L22: 曲库接口要求 Referer 是 music.bilibili.com，搜索接口是 search.bilibili.com
            hdr = _headers({"Referer": referer} if referer else None)
            req = urllib.request.Request(full, headers=hdr)
            with _OPENER.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            return data
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (412, 429, 503):
                _note_limited()
                if attempt < retries - 1:
                    _WARMED = False
                    _WBI_KEY_CACHE = (0.0, "")   # 密钥可能过期，下一轮重新取
                    time.sleep(1.5 * (attempt + 1))
                    continue
            raise
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    raise last_err or RuntimeError("请求失败")


def _parse_search(data: dict[str, Any]) -> list[Card]:
    cards: list[Card] = []
    for it in ((data.get("data") or {}).get("result") or []):
        title = re.sub(r"<.*?>", "", str(it.get("title") or ""))
        cards.append(
            Card(
                bvid=str(it.get("bvid") or ""),
                title=title,
                play=int(it.get("play") or 0),
                duration=parse_duration(it.get("duration") or "0:0"),
                author=str(it.get("author") or ""),
                tags=str(it.get("tag") or ""),
                videos=int(it.get("videos") or 1),
            )
        )
    return [c for c in cards if c.bvid]


def search_page(keyword: str, page: int = 1, order: str = "click", tids: int | None = None,
                timeout: float = 15) -> list[Card]:
    ck = (keyword, page, order, tids)
    hit = _SEARCH_CACHE.get(ck)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    params: dict[str, Any] = {
        "search_type": "video",
        "keyword": keyword,
        "order": order,
        "page": str(page),
    }
    if tids:
        params["tids"] = str(tids)
    data = _http_json(SEARCH_API, params, timeout=timeout)
    if data.get("code") != 0:
        raise RuntimeError(f"搜索接口返回 code={data.get('code')} {data.get('message')}")
    out = _parse_search(data)
    if not out and (data.get("data") or {}).get("v_voucher"):
        # 风控给了验证凭证：换一次密钥重签，仍空就走上层兜底
        # L23：空池 + v_voucher 就是被风控了，算一次限流，连续出现会降并发
        _note_limited()
        # L43 阶段 5：风控空池只记录、不改缓存行为（空池本来就不进 _SEARCH_CACHE）
        metrics_event("search_limited", keyword=keyword,
                      code=str(data.get("code") or ""))
        try:
            _wbi_key(force=True)
            retry_data = _http_json(SEARCH_API, params, timeout=timeout)
            # FIX(L6): 重签后如果接口返回非 0 code（限流/风控），不能静默当成
            # 「搜不到这首歌」，否则真实原因被吞，界面只报「搜索无结果」。
            if retry_data.get("code") != 0:
                raise RuntimeError(
                    f"搜索接口返回 code={retry_data.get('code')} {retry_data.get('message')}"
                )
            out = _parse_search(retry_data)
        except RuntimeError:
            raise
        except Exception:
            pass
    if out:
        _cache_put(_SEARCH_CACHE, ck, (time.time(), out), _CACHE_LIMIT_SEARCH)
        # L23：真搜到了东西才算一次「健康」，用于太平时回升并发
        _note_ok()
    else:
        # L43 阶段 5：空池也留痕，否则命中率悄悄掉时看不出是哪类
        metrics_event("search_empty", keyword=keyword)
    return out


def search_both(keyword: str, page: int = 1) -> list[Card]:
    """综合 + 音乐分区，合并去重；音乐分区结果打标。"""
    merged: dict[str, Card] = {}
    errors: list[str] = []
    # FIX(M4/M22): 原实现把两个分区的异常都 `pass` 掉。风控/断网时返回空列表，
    # 上层只会报「搜索无结果」，412/超时/未登录这些真实原因被彻底吞掉。
    # 音乐分区用「综合排序」而不是「最多播放」：
    # 热门词（我们不一样/出山/生僻字/Havana/野狼disco）在播放量榜上被生活、科普、
    # 鬼畜视频占满（千万播放的 vlog 排在前面），正片播放量常常只有几百到几万，
    # 在 click 排序下根本进不了前 20，候选池里没有正片，后面怎么排序都选不对。
    # 综合排序（totalrank）下 B 站把相关性权重放高，官方 MV 能排到第一。
    try:
        for c in search_page(keyword, page=page, order="totalrank", tids=3):
            c.from_music = True
            merged[c.bvid] = c
    except Exception as e:
        errors.append(f"音乐分区：{e}")
    try:
        for c in search_page(keyword, page=page):
            merged.setdefault(c.bvid, c)
    except Exception as e:
        errors.append(f"综合分区：{e}")
    if not merged and errors:
        raise RuntimeError("；".join(errors))
    return list(merged.values())


# ---------------------------------------------------------------- 选片

@dataclass
class PickResult:
    card: Card | None
    reason: str
    candidates: list[Card] = field(default_factory=list)
    # CAND(1): 新增排序后的候选池（已过滤），供界面「先试搜、点选确认」展示
    pool: list["Card"] = field(default_factory=list)
    # v8：候选加权分（bvid → 分数）。选片基数已从播放量改为 1，播放量不再进分数，
    # 界面判断「第一名是否断层」必须用它，不能再用播放量冒充。
    scores: dict[str, float] = field(default_factory=dict)
    # CAND(4): "ok" 表示可以直接下，"pending" 表示过滤链把正片剔光但仍有候选可挑
    # （全翻唱/全是鬼畜壳/只剩切片…），界面据此把它当「待确认」而不是「没有选中」。
    stage: str = "ok"


def search_ytdlp(keyword: str, limit: int = 20) -> list[Card]:
    """兜底搜索：用 yt-dlp 的 bilisearch，绕开接口风控。

    拿不到播放量，Card.play 统一记 0，选片需配合 ignore_play=True。
    """
    try:
        import yt_dlp
    except Exception:
        return []
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
        "socket_timeout": 15,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"bilisearch:{keyword}", download=False)
    except Exception:
        return []
    out: list[Card] = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        bvid = str(e.get("id") or "").strip()
        title = str(e.get("title") or "").strip()
        if not bvid or not title:
            continue
        try:
            dur = int(e.get("duration") or 0)
        except Exception:
            dur = 0
        out.append(Card(
            bvid=bvid,
            title=title,
            play=0,
            duration=dur,
            author=str(e.get("uploader") or e.get("channel") or ""),
            from_music=False,
        ))
    return out


_PRIORITY_RX: dict[str, re.Pattern[str] | None] = {}
# 只有出现这些符号才把词条当正则；`.`、`-` 不算，避免歌名里的 Mr. / No.1 被误判
_PRIORITY_META = set("|()[]{}*+?^$\\")


def _priority_hit(pattern: str, title: str) -> bool:
    """词条：含正则符号按正则（忽略大小写），否则按普通文字包含。"""
    p = str(pattern).strip()
    if not p:
        return False
    if not any(ch in _PRIORITY_META for ch in p):
        return p.lower() in title.lower()
    if p not in _PRIORITY_RX:
        try:
            _PRIORITY_RX[p] = re.compile(p, re.IGNORECASE)
        except re.error:
            _PRIORITY_RX[p] = None
    rx = _PRIORITY_RX[p]
    if rx is None:
        return p.lower() in title.lower()
    return rx.search(title) is not None


_TAG_UP = ("bgm", "ost", "原创音乐", "游戏音乐", "电音", "无损", "hi-res", "hires", "纯音乐", "ep")
_TAG_DOWN = ("鬼畜", "搞笑", "沙雕", "漫展", "vlog", "生活记录", "解说", "实况", "攻略", "直播", "反应", "吐槽", "翻唱")

# 标题词只降权、不排除：一个是避免误杀高音质现场，一个是保持「播放量最高」为主序
_TITLE_DOWN_STRONG = ("歌单", "playlist", "循环", "合集", "原曲：", "原曲:", "翻弹",
                      "纯享", "片段", "试听", "off vocal", "インスト", "伴奏",
                      "动态歌词", "歌词排版", "歌词版", "歌词向", "歌词mv", "歌词视频")
_TITLE_DOWN_MILD = ("remix", "リミックス", "live", "现场", "演唱会", "改编", "翻自")
# 现场活动版：生日会 / 生放 / 音乐节 / 跨年 等。正片是活动录像，编曲、时长、音质都跟原版不同，
# 但仍要留作兜底（只有这些版本时照下），所以只降权、不判否。
_TITLE_EVENT = ("生日会", "生日演唱会", "生放", "周年", "音乐节", "跨年", "巡演", "路演",
                "商演", "见面会", "歌友会", "livehouse", "concert", "tour", "bml",
                "线上演唱会", "online live")
_TITLE_UP_WORDS = ("官方", "完整版", "无损", "hi-res", "原声", "本家")
# 高音质正片：用户明确偏好 Hi-Res / 无损试听，单独给较高加分
_TITLE_HIRES = ("hi-res", "hires", "无损", "高音质", "母带", "lossless")
# 翻唱/重制：正片不是原唱
_TITLE_COVER = ("cover", "翻唱", "翻自", "翻弹")
_FEAT_RX = re.compile(r"feat\.?\s*([^)\]】/,，、]+)")

# 下面四类是「正片不是这首歌」的硬信号，只降权、不判否（判否由 pick_card 统一处理）
_TITLE_EDIT = ("剪辑", "混剪", "卡点", "踩点", "燃向", "回忆向", "amv", "mad", "手书", "mmd", "pv向")
_TITLE_AI = ("ai翻唱", "ai cover", "aicover", "sovits", "rvc", "ai孙燕姿", "ai周杰伦")
_TITLE_FX = ("8d", "环绕", "加速版", "减速版", "变速", "降调", "升调", "dj版", "慢摇", "蹦迪", "电音版")
_TITLE_PAY = ("充电专属", "大会员", "付费", "会员专享")
# 器乐演奏版：标题写着「古筝」「钢琴版」时正片是乐器翻奏，唱的不是这首歌。
# 这类版本播放量常常极高（Animenz 的钢琴改编、古筝 cover），不降权会长期压过
# 原唱正片，是 80 首实测里最多的一类错选。
_TITLE_INSTR = ("古筝", "钢琴", "小提琴", "大提琴", "吉他", "二胡", "笛子", "琵琶",
                "陶笛", "卡林巴", "口琴", "萨克斯", "架子鼓", "演奏", "翻奏", "弹出",
                "乐器", "编曲", "改编", "混音", "八音盒", "钢琴版", "吉他版",
                "纯音乐版", "piano", "instrumental",
                # v7 补：run100 漏掉的乐器/乐谱词——「贝斯翻奏」「动态鼓谱」「太鼓达人」
                # 都因词表缺口压过原唱正片。
                "贝斯", "电吉他", "贝斯翻奏", "鼓谱", "乐谱", "谱", "太鼓", "鼓",
                "打击乐", "唢呐", "手风琴", "长笛", "竖笛", "黑管", "圆号", "小号",
                # v10 补：「双排键」「电子琴」等键盘/管乐/弦乐器，v7_spot2 里「双排键
                # 【天体観測】」157 播放翻奏压过 BUMP OF CHICKEN 正片（词表缺口）。
                "双排键", "电子琴", "电钢琴", "电风琴", "管风琴", "合成器", "口风琴",
                "长号", "中提琴", "单簧管", "双簧管", "大管", "唢呐")
# v5 R1：器乐改编版的正片没有人声，和「翻唱」同档：直接不参与选片。要求出现在
# 【】标签里或写成「XX版/翻奏/演奏」，避免「钢琴」只是标题里的一个普通词。
_TITLE_INSTR_RX = re.compile(
    r"【[^】]*(古筝|钢琴|小提琴|大提琴|二胡|笛子|琵琶|吉他|陶笛|卡林巴|口琴|萨克斯|架子鼓|贝斯|电吉他|太鼓|双排键|电子琴|电钢琴|管风琴|合成器|单簧管|双簧管|中提琴)[^】]*】"
    r"|(古筝|钢琴|小提琴|大提琴|二胡|笛子|琵琶|吉他|陶笛|卡林巴|口琴|萨克斯|架子鼓|贝斯|电吉他|太鼓|双排键|电子琴|电钢琴|管风琴|合成器|单簧管|双簧管|中提琴)版"
    r"|(演奏|翻奏|弹出|乐器|器乐版|编曲|改编|混音|八音盒|纯音乐版|instrumental)"
    # v7：乐谱/鼓谱/贝斯演奏类——「动态鼓谱」「贝斯翻奏」这类标题词不在【】里也不带「版」
    r"|(鼓谱|乐谱|翻谱|扒谱|贝斯|电吉他|太鼓|打击乐|双排键|电子琴|电钢琴|管风琴|合成器|单簧管|双簧管|中提琴)", re.I)

# v5 R2/R3：UP 主名里的演奏者信号与翻唱者信号。
# 「Animenz」这类钢琴改编号的标题常常一个字都不提钢琴（80 首实测里
# 【Animenz】Only My Railgun 543万 压过南条爱乃原唱现场 381万），只能靠 UP 主名认。
_UP_PLAYER = ("animenz", "animenzzz", "演奏", "四重奏", "乐团", "钢琴家", "小提琴家",
              # v7 补：run100「禅贝斯-东旭」贝斯翻奏 UP 主名漏判，压过 BAAD 原唱
              "贝斯", "电吉他", "鼓手", "鼓谱", "扒谱", "翻谱", "指弹", "太鼓",
              # v10 补：双排键/键盘类演奏 UP 主名
              "双排键", "电子琴", "管风琴", "合成器")
_UP_COVER = ("儿童合唱团", "合唱团", "童声", "翻唱", "cover")
# UP 主本身就是官方/搬运原版时不算演奏者（R2 的保护条件）
_OFFICIAL_HINT = ("官方", "原版", "本家", "official", "频道")
# 「…唱《歌名》」里的演唱者：取「唱」前面的名字，剥掉「听完/让/由/当」这类引导动词
_SING_RX = re.compile(
    r"([\u4e00-\u9fffA-Za-z·]{2,6}?)\s*(?:终于|也|又|还|首次|再次|翻唱)?\s*唱[了过出]?"
    r"\s*(?:这首|这一首|这一?首|出?一首|一曲)?\s*[《「]"
)
_SING_LEAD = ("听完", "听了", "听", "看完", "看", "让", "由", "被", "请", "跟", "和", "与", "当")
# v5 R5：循环歌单/整场串烧类标题词
_LONG_RX = re.compile(r"循环|歌单|合集|小时|全曲|串烧")
# 伴奏/卡拉OK/消音版没有原唱人声，且常把「MV+伴奏」拼成一整条（时长翻倍）
_KARAOKE_RX = re.compile(r"伴奏|卡拉\s*ok|karaoke|消音|instrumental version", re.I)
# 语言/声部替换版（日语版《生僻字》、女声版、粤语版）本质是翻唱，同样排后面
_COVER_VER_RX = re.compile(r"(日语|粤语|英文|中文|国语|女声|男声|童声|群星)版|翻唱版|改编版")
# 反应/吐槽/点评/解说类：标题里出现歌名，但视频内容不是这首歌
# v7 补：run100 里「为啥费玉清《一剪梅》突然欧美爆火？」「音乐秘闻EP」这类口语化解说
# 词不在旧表，566 万解说梗视频过关。加入口语变体与知识/揭秘类词。
# 注意：「故事」不能进硬剔表——「光阴的故事」是正经歌名，会被误杀。
_REACTION_RX = re.compile(
    r"反应|reaction|吐槽|点评|解析|解说|盘点|听后感|为啥|为什么|冷知识|知识|"
    r"秘闻|揭秘|科普|冷门|涨知识|爆火|突然火|访谈|采访|杂谈|测评|评测|赏析|乐评|"
    r"速学|教学|教程|开课|逐句|歌词分解")
# v7 R9：鬼畜/二创壳——「用六十部动漫台词唱出一首《稻香》」「用31个替身名唱一首稻香」
# 「用四十一部动漫名拼出一首《稻香》」。这类稿件播放量常年压过正片，标题却连「翻唱」
# 都不写，R3 的「X 唱《歌名》」正则也抓不到演唱者，只能在标题上加一档重罚。
_GHOST_RX = re.compile(
    r"用[^《「」]{0,14}(?:部|个)[^《「」]{0,12}(?:唱|拼|合|演|填)|台词唱|配音唱|拼出一首")
# 合奏/弹奏/器乐演奏版：标题常写「初见合奏」「本人弹」，没有原唱人声
_ENSEMBLE_RX = re.compile(r"合奏|弹奏|演奏版|本人弹|钢琴曲|吉他曲|指弹|尤克里里|竖琴|口琴版|八音盒版")
_TITLE_ALBUM = ("专辑", "album", "全专", "全碟", "完整专辑", "曲目", "收录于", "首播", "试听碟")
# 解说/盘点类：正片是「讲这首歌」而不是「放这首歌」，按用户要求重罚
_TITLE_TALK = ("解说", "盘点", "解析", "杂谈", "科普", "测评", "听后感", "反应",
               "reaction", "第一次听", "冷知识", "排行榜", "访谈", "幕后", "故事",
               "编曲分析", "乐评", "串烧", "对比")
_TALK_RX = re.compile(
    r"(我想|我觉得|我认为|你知道吗|你们知道|为什么|如何|怎么|应该是|其实是|明明是|"
    r"到底|居然|竟然|原来|据说|讲讲|聊聊|说说|盘点)")


def _book_count(text: str) -> int:
    return len(BOOK_MARK.findall(str(text or "")))
# UP 主名里的搬运号特征
_UP_MOVER = ("搬运", "合集", "音乐站", "歌单", "电台", "音乐库", "精选")
# FIX(m11937 项2): 厂牌/群星/合唱/影视原声这类「不是个人演唱者」的名字，
# 命中者不参与 by_title 与作者加分——音阙诗听、supercell 是厂牌不是演唱者，
# 等什么君/司南是翻唱者常被曲库误当原唱返回，标题里出现这些名不能当作者锚点。
_NOT_ARTIST_RX = re.compile(
    r"音阙诗听|supercell|众星|合唱|群星|电影原声|原声带|ost\s*合辑|ost\s*合集|"
    r"唱片公司|官方账号|音乐企划|厂牌|feat\s*\.?", re.IGNORECASE)

_MILD_RX = re.compile(r"\b(?:remix|live)\b", re.IGNORECASE)
_UP_RX = re.compile(r"\b(?:hi-res|ost)\b", re.IGNORECASE)
# 「（原曲：X）」= 本视频是改编/再创作，正片不是 X 本身；而单独出现的「原曲」表示「这是原曲」，不算
_ADAPT_RX = re.compile(r"[（(【\[]\s*原曲\s*[：:]")


def title_score(card: Card, artist: str = "", skip_dur: bool = False) -> float:
    """标题加权（0.25～1.40）：括号里标「原曲：」= 改编，重降；歌单/合集/伴奏次之。

    再叠硬信号：二创剪辑、AI 翻唱、音效处理版、付费内容、专辑合集、
    解说/盘点、多书名号、翻唱(cover)、时长异常（<90s 片段、>12min 整张专辑）、
    现场活动版（生日会/生放/音乐节）；
    正向信号：Hi-Res/无损试听（用户偏好高音质）、官方/本家。
    feat. 后面的艺人不是已知作者时，视为翻唱/客串版本，轻降。
    """
    t = str(card.title or "").lower()
    if not t:
        return 1.0
    adapt = 1 if _ADAPT_RX.search(t) else 0
    strong = sum(1 for k in _TITLE_DOWN_STRONG if k in t and k not in ("原曲：", "原曲:"))
    mild = len(_MILD_RX.findall(t)) + sum(1 for k in _TITLE_DOWN_MILD if k in t and k not in ("remix", "live"))
    up = sum(1 for k in _TITLE_UP_WORDS if k in t) + len(_UP_RX.findall(t))
    edit = sum(1 for k in _TITLE_EDIT if k in t)
    ai = sum(1 for k in _TITLE_AI if k in t)
    fx = sum(1 for k in _TITLE_FX if k in t)
    pay = sum(1 for k in _TITLE_PAY if k in t)
    album = sum(1 for k in _TITLE_ALBUM if k in t)
    talk = sum(1 for k in _TITLE_TALK if k in t)
    # FIX(L13 规格§6 第 2 条)：「口语化梗词 −0.30」原本没有独立项，被并进 talk 一起
    # 吃 −0.50，等于把「解说/盘点」和「我想/你知道吗/为什么」当成同一档罚。
    # 现在拆开：解说盘点 −0.50（talk），口语化梗词 −0.30（oral），各自最多算一次。
    oral = len(_TALK_RX.findall(t))
    hires = 1 if any(k in t for k in _TITLE_HIRES) else 0
    cover = 1 if any(k in t for k in _TITLE_COVER) else 0
    instr = sum(1 for k in _TITLE_INSTR if k in t)
    event = sum(1 for k in _TITLE_EVENT if k in t)
    ghost = 1 if _GHOST_RX.search(t) else 0        # v7 R9：鬼畜/二创壳
    feat = 0
    if artist:
        a = normalize(artist)
        for name in _FEAT_RX.findall(t):
            n = normalize(name)
            if n and n not in a and a not in n:
                feat = 1
                break
    multi_book = 1 if _book_count(t) >= 2 else 0
    # C4：C1（片段时长基准）命中时不再叠加「时长 <90s/>12min」这条异常降权，
    # 否则同类信号被罚两次，会把边缘正片打到 0.25 下限。
    dur_bad = 0 if skip_dur else (
        1 if card.duration and (card.duration < 90 or card.duration > 12 * 60) else 0)
    score = (1.0 - adapt * 0.62 - min(strong, 2) * 0.35
             - min(mild, 2) * 0.12 - min(edit, 2) * 0.35 - min(ai, 1) * 0.50
             - min(fx, 2) * 0.35 - min(pay, 1) * 0.40 - min(album, 1) * 0.45
             - min(talk, 1) * 0.50 - min(oral, 1) * 0.30
             - multi_book * 0.30 - cover * 0.30
             - min(instr, 2) * 0.35
             - min(event, 2) * 0.20 - ghost * 0.45
             - feat * 0.25 - dur_bad * 0.65 + min(up, 2) * 0.05
             + hires * 0.25)
    return max(0.25, min(1.40, score))


def up_score(card: Card, artist: str) -> float:
    """UP 主与作者匹配加分，搬运号降权。"""
    a = normalize(artist)
    up = normalize(card.author)
    if a and up and a in up:
        return 1.10
    low = str(card.author or "").lower()
    if any(k in low for k in _UP_MOVER):
        return 0.85
    return 1.0


def album_like(card: Card) -> bool:
    """整张专辑/合集：超过 25 分钟，或「专辑词 + 超过 8 分钟」，或多P 且带专辑词。"""
    dur = card.duration or 0
    if dur > 25 * 60:
        return True
    low = str(card.title or "").lower()
    has_album = any(k in low for k in _TITLE_ALBUM)
    if has_album and dur > 8 * 60:
        return True
    # FIX(M6): 原来是 `> 3`，2–3P 的整张专辑会绕过 pick_card 的专辑过滤。
    # （注：搜索接口的 videos 字段实测恒为 1，本条当前很少触发，但判据本身要正确）
    if card.videos > 1 and has_album:
        return True
    return False


def tag_score(card: Card) -> float:
    """按标签给播放量加权（0.85～1.15）：音乐类加分，非音乐类降权。

    只做加权、不做过滤：权重幅度有限，永远翻不了「播放量差数倍」的盘。
    """
    raw = (card.tags or "").lower()
    if not raw:
        return 1.0
    tags = [t.strip() for t in raw.split(",") if t.strip()]

    def hit(k: str) -> bool:
        # 短标签（ep / ost / bgm）必须整条相等，避免命中 episode、hosted 之类
        if len(k) <= 3:
            return any(t == k for t in tags)
        return any(k in t for t in tags)

    up = sum(1 for k in _TAG_UP if hit(k))
    down = sum(1 for k in _TAG_DOWN if hit(k))
    score = 1.0 + min(up, 3) * 0.05 - min(down, 3) * 0.05
    return max(0.85, min(1.15, score))


# 「歌名-歌手」结构里抓歌手：放开拉丁字母 / 日文假名 / 汉字，长度 2～12，
# 支持 feat. 双主唱（&）与中点（·）分隔；后面紧跟空白/引号/结尾。
# FIX(m11937 项5): 原来只 2-6 汉字，RADWIMPS/Céline Dion/あいみょん/BAAD 等
# 拉丁/假名作者名全漏判。
_DASH_ARTIST_RX = re.compile(
    r"[-–—]\s*([\u4e00-\u9fffA-Za-z\u3040-\u30ff·&]{2,12})(?=[\s\"“”「』【\[（(]|$)")


_LYRIC_SEPS = re.compile(r"[/|~～]")
_LYRIC_SIGNAL = ("无损", "官方", "完整版", "高音质", "hi-res", "hires",
                 "mv", "cover", "翻唱", "ep", "ost", "bgm", "live", "原声")


def lyric_like(title: str) -> bool:
    """标题像「歌词句拼接」：3 段以上纯中文短句，且没有任何正曲信号。

    例：`青春的上游/白云飞走/苍狗与海鸥` —— 这是《情歌》的歌词，不是《苍狗与海鸥》。
    """
    t = str(title)
    low = t.lower()
    if any(s in low for s in _LYRIC_SIGNAL):
        return False
    segs = [s.strip() for s in _LYRIC_SEPS.split(t) if s.strip()]
    if len(segs) < 3:
        return False

    def plain(s: str) -> bool:
        return not re.search(r"[A-Za-z0-9【】\[\]（）()<>《》]", s)

    return all(plain(s) and 2 <= len(s) <= 10 for s in segs)


def artist_mismatch(title: str, artist: str) -> bool:
    """标题里写了「-某歌手」，而这位与已知作者不一致 → 说明是别人的歌。"""
    a = normalize(artist)
    if not a:
        return False
    for m in _DASH_ARTIST_RX.finditer(str(title)):
        n = normalize(m.group(1))
        if n and n != a:
            return True
    return False


def pick_card(
    cards: list[Card],
    keyword: str,
    min_play: int = 1,
    min_dur: int = 50,
    max_dur: int = 720,
    priority_words: list[str] | None = None,
    strict_music: bool = True,
    ignore_play: bool = False,
    artist: str = "",
    exclude: set[str] | None = None,
    ref_dur: float = 0.0,
    known_artists: list[str] | None = None,
    canonical_artist: str | None = None,
    pool_n: int = 10,
    want: str = "",
) -> PickResult:
    # L14: pool_n 让新流程（试搜）能拿到**完整**排序池算「过滤后条数」；
    # 老调用方不传，行为仍是原来那 10 条。
    words = [
        w for w in (DEFAULT_PRIORITY if priority_words is None else priority_words)
        if str(w).strip()
    ]

    banned = exclude or set()
    # 用户写了「翻唱/live/钢琴」这类版本词时，匹配只看歌名本身：
    # 搜索串带版本词能召回对的内容，但标题里极少同时写「晴天」「翻唱」两个词，
    # 拿整串去 match_rank 会把正确结果全判成「不匹配」。
    _lenient = bool(_USER_WANT_RX.search(want or keyword or ""))
    _kw = keyword
    if _lenient:
        _clean = _USER_WANT_RX.sub(" ", keyword).strip()
        if _clean:
            _kw = _clean
    ranked = [(c, match_rank(c.title, _kw)) for c in cards
              if c.title and c.bvid not in banned]
    ranked = [(c, r) for c, r in ranked if r > 0]
    relevant = [c for c, _ in ranked]
    if not relevant:
        # 标题里没同时出现歌名/作者的全部词（繁体、中英混写、B站标题改写都会这样）：
        # 与其报「没有选中」让用户干瞪眼，不如按热度把前三条端出来让他自己挑。
        if cards:
            _pool = sorted(cards, key=lambda c: c.play, reverse=True)[:pool_n]
            _why = ("标题里没有明确歌名" if _lenient else "标题里没有同时出现歌名和作者")
            return PickResult(None, f"{_why}，已按热度给前三条待选",
                              pool=_pool, candidates=_pool[:3], stage="pending")
        return PickResult(None, "没有标题匹配的视频")
    if _lenient:
        # 用户要的就是翻唱/伴奏/器乐版：跳过五道内容类硬过滤
        musical = list(relevant)
    else:
        musical = [c for c in relevant if not TITLE_BLOCK.search(c.title)]
        # 器乐改编版（【古筝】千本樱、【Animenz】unravel 钢琴版）正片没有人声，
        # 与「翻唱」同档硬剔除：这类稿件的播放量往往比原唱还高，只降权压不住。
        musical = [c for c in musical if not _TITLE_INSTR_RX.search(c.title)]
        # v6 R7：二创壳（【李云龙】出山 1467万、【马冬梅】Havana 433万）硬剔除，
        # 它们的播放量比原唱高一个数量级，降权压不住。全被剔时说明候选池只剩二创。
        de_shell = [c for c in musical if not _shell_of(c.title, keyword, known_artists)]
        if de_shell:
            musical = de_shell
        elif musical:
            # CAND(5): 不给 0 结果，把二创壳当候选交用户点选
            return PickResult(None, "候选池无正片（全是鬼畜/二创壳）；已放宽过滤，给你前三条待选",
                              pool=sorted(musical, key=lambda c: c.play, reverse=True)[:pool_n],
                              candidates=sorted(musical, key=lambda c: c.play, reverse=True)[:3],
                              stage="pending")
        # v6 R9：反应/吐槽/点评/解说类稿件，标题带歌名但内容是「别人听这首歌」，
        # 离人愁实测：「周杰伦听《离人愁》时的各种反应」795 万播放压过所有原唱。
        de_talk = [c for c in musical if not _REACTION_RX.search(c.title)]
        if de_talk:
            musical = de_talk
        # v6 R10：伴奏/消音/卡拉OK 版没有人声。追光者实测选到「岑宁儿-追光者（伴奏）」。
        de_inst = [c for c in musical if not _KARAOKE_RX.search(c.title)]
        if de_inst:
            musical = de_inst
        if not musical:
            _pool = sorted(relevant, key=lambda c: c.play, reverse=True)[:pool_n]
            return PickResult(None, "匹配结果都不是歌曲视频；已放宽过滤，给你前三条待选",
                              pool=_pool, candidates=_pool[:3], stage="pending")

    if min_dur > 0:
        long_enough = [c for c in musical if c.duration == 0 or c.duration >= min_dur]
        if not long_enough:
            return PickResult(None, f"匹配视频都短于 {min_dur}s")
    else:
        long_enough = musical
    if max_dur > 0:
        long_enough = [c for c in long_enough if c.duration == 0 or c.duration <= max_dur]
        if not long_enough:
            return PickResult(None, f"匹配视频都长于 {max_dur}s")

    # 音乐分区只加分、不硬过滤：硬过滤会漏掉综合区里的正片
    pool = list(long_enough)
    _music = (lambda c: 1.5 if c.from_music else 1.0) if strict_music else (lambda c: 1.0)

    if ignore_play:
        above = list(pool)
    else:
        above = [c for c in pool if c.play >= min_play]
        if not above:
            return PickResult(None, f"匹配视频播放量不足 {min_play / 10000:g}w")

    # 整张专辑/合集：多P + 专辑词，或超过 25 分钟 → 正片里没有单曲，直接排除
    without_album = [c for c in above if not album_like(c)]
    if not without_album:
        # CAND(3a): 失败也把候选给界面；此处加权排序键（kinds/_score）尚未生成，按播放量降序兜底
        _pool = sorted(above, key=lambda c: c.play, reverse=True)[:pool_n]
        return PickResult(None, "只搜到专辑/合集，未找到单曲；已放宽过滤，给你前三条待选",
                          pool=_pool, candidates=_pool[:3], stage="pending")
    above = without_album

    # v6 R4b：明显短于基准的片段**硬剔除**，不只是降权。0.4 的降权系数压不住
    # 「蘑菇头 63s 翻唱 4.1 万播放」这类，而原唱官方 MV 只有 893 播放。
    # 剔光说明整个候选池只剩切片（综艺剪辑/副歌剪辑），此时硬选一条必然是错的，
    # 诚实报失败让用户换关键词或走直链，符合「宁可漏判不可误下」。
    if ref_dur > 0:
        keep = [c for c in above
                if not c.duration or c.duration >= ref_dur * _CLIP_RATIO]
        if keep:
            above = keep
        else:
            # CAND(3b): 失败也把候选给界面；加权排序键此时尚未生成，按播放量降序兜底
            _pool = sorted(above, key=lambda c: c.play, reverse=True)[:pool_n]
            return PickResult(None, "候选池无正片（只剩切片/片段）；已放宽过滤，给你前三条待选",
                              pool=_pool, candidates=_pool[:3], stage="pending")

    # v5 R5：超长（循环歌单/整场串烧/1 小时版）剔除。1.6 倍基准之外还看绝对时长，
    # 因为「循环歌单」这类稿件的标题经常一个循环词都不写。
    if ref_dur > 0:
        keep = [c for c in above
                if not (c.duration > ref_dur * 1.6
                        and (c.duration > 900 or _LONG_RX.search(c.title)
                             or _KARAOKE_RX.search(c.title)))]
        if keep:
            above = keep

    # v5 R2/R3：演奏者软降权、翻唱候选排到非翻唱之后
    kinds = {c.bvid: _cover_kind(c, known_artists) for c in above}
    # v5 R6：同组候选**全部**命中翻唱信号时，「排在非翻唱之后」是空操作，
    # 只能诚实报失败，否则会硬选一个翻唱（千本樱实测：全是器乐/主播翻唱/茶理理）。
    if kinds and all(k == 2 for k in kinds.values()):
        # CAND(3c): 失败也把候选给界面；加权排序键（_score）定义在下方，按播放量降序兜底
        _pool = sorted(above, key=lambda c: c.play, reverse=True)[:pool_n]
        return PickResult(None, "候选池无正片（全是翻唱/器乐改编）；已放宽过滤，给你前三条待选",
                          pool=_pool, candidates=_pool[:3], stage="pending")

    def _videos_factor(c: Card) -> float:
        # 多P 但不是专辑（如「XX歌曲合集」）只轻降，不判否
        return 0.8 if c.videos > 1 else 1.0

    def _title_weight(c: Card) -> float:
        """C1/C3：片段与情感号降权，二者取 max 不叠加（C4）。"""
        clip = _clip_factor(c.duration, ref_dur)
        ts = title_score(c, artist, skip_dur=clip < 1.0)
        if clip < 1.0:
            return ts * clip
        if _emotion_title(c.title):
            return ts * 0.6
        return ts

    def _score(c: Card) -> float:
        # v8（用户要求）：所有候选初始分统一为 1，再乘各项权重——播放量不再作基数。
        # 播放量只作同分时的次序（见下面 sort 的第三维），不再能靠「热门片段」压过正片。
        s = (1.0 * _play_floor(c) * tag_score(c) * _title_weight(c) * _videos_factor(c)
             * up_score(c, artist) * _music(c)
             * (0.5 if artist_mismatch(c.title, artist) else 1.0))
        if kinds.get(c.bvid) == 1:
            s *= 0.45          # v5 R2：UP 主本身是演奏者
        # v6 R8：「标题里写着曲库作者名」是正片信号（UP 主自己标注）。
        # 花粥&王胜娚《出山》、大壮——《我们不一样》官方MV、Havana - Camila Cabello
        # 都靠它从播放量只有几百的正片里冒出来。
        # FIX(修2, m11662): 原来 ×1.35 且只判「标题子串含作者名」。翻唱者名往往也在
        # 曲库里（QQ/网易云把翻唱版也收录），标题写翻唱者名也会吃满这 1.35，反而把
        # 翻唱压过原唱。改成 ×1.15 且要「2 项交叉确认」才生效。
        # FIX(m11937 项3): 交叉确认去掉「时长落众数簇」分支（古风样本上众数簇被翻唱
        # 占据，不可作判据），改为【UP主名含同一作者 / from_music=True / 标题无
        # cover·DJ·Live·改编 信号】三选二；canonical_artist 可用时作者加分仅对齐 canonical。
        if known_artists:
            tn = normalize(c.title)
            upn = normalize(c.author or "")
            _neg = bool(re.search(r"cover|翻唱|翻自|翻弹|dj|live|改编",
                                  c.title, re.IGNORECASE))
            for an in (normalize(a) for a in known_artists if a):
                if len(an) < 2 or an not in tn:
                    continue
                # canonical_artist 可用时：标题写的作者必须对齐 canonical 才加分，
                # 否则（标题写翻唱者名）不奖励，让 canonical 正片自然靠前。
                if canonical_artist and normalize(canonical_artist) != an:
                    break
                # 三选二：UP 主名含同一作者 / 音乐分区 / 标题无 cover·DJ·Live·改编
                hits = sum([bool(upn and an in upn), bool(c.from_music), not _neg])
                if hits >= 2:
                    s *= 1.15
                break
        # L43：用户右键否掉的 bvid 只降权、不硬剔；30 天过期、可撤销
        if known_bad_active(keyword, c.bvid):
            # L44：命中降权记一次流水；同歌同 BV 每进程只记一次，避免候选池刷屏
            _bad_key = (normalize(_song_only(keyword)), c.bvid)
            if _bad_key not in _BAD_HIT_SEEN:
                _BAD_HIT_SEEN.add(_bad_key)
                metrics_event("known_bad_hit", song=keyword, bvid=c.bvid)
            s *= 0.5
        return s

    # v8：加权分先算一遍存表，排序与候选池共用（原来 sort 的 key 里反复调 _score）
    _sc = {c.bvid: _score(c) for c in above}
    above.sort(
        # 第一维：翻唱候选（kind=2）永远排在同组非翻唱候选之后。
        # 注意 reverse=True，所以非翻唱要填 0（更小）= 排前面。
        # 第三维播放量只做同分兜底，不再参与分数。
        key=lambda c: (0 if kinds.get(c.bvid) == 2 else 1, _sc.get(c.bvid, 0.0), c.play),
        reverse=True,
    )

    # 同一 UP 的同名稿件只留播放最高的一条，避免刷屏
    seen: set[tuple[str, str]] = set()
    deduped: list[Card] = []
    for c in above:
        k = (normalize(c.author), normalize(c.title))
        if k in seen:
            continue
        seen.add(k)
        deduped.append(c)
    above = deduped

    # 优先级词条：按顺序，命中即取该组「加权排序第一」（不是纯播放量最高）；词条按正则解释
    # FIX(L5): 原注释写「组内取播放量最高」，实际取的是 group[0]（已按加权分排序）。
    for w in words:
        group = [c for c in above if _priority_hit(w, c.title)]
        if group:
            # CAND(2): 命中项置顶，其余仍按同一排序键（above 已排序）去重后补足 10 条
            _pool = [group[0]] + [c for c in above if c is not group[0]]
            return PickResult(group[0], f"命中优先词「{w}」", above[:5],
                              pool=_pool[:pool_n],
                              scores={c.bvid: _sc.get(c.bvid, 0.0) for c in _pool[:pool_n]})

    # 高音质档：Hi-Res / 无损试听（用户长期偏好），播放量达到「排序第一」的 25% 即优先
    # FIX(L5): 原注释写「全池最高」，实际比较对象是 above[0]（加权排序第一，不保证播放最高）。
    hires_pool = [c for c in above if c.from_music and any(k in c.title.lower() for k in _TITLE_HIRES)]
    # v8：门槛从「播放量 ≥ 第一名 25%」改成「加权分 ≥ 第一名 25%」——
    # 选片基数已不含播放量，继续按播放量比会出现「第一名 0 分池」的错判。
    if hires_pool and _sc.get(hires_pool[0].bvid, 0.0) >= _sc.get(above[0].bvid, 0.0) * 0.25:
        # CAND(2): 高音质命中项置顶，其余仍按同一排序键（above 已排序）去重后补足 10 条
        _pool = [hires_pool[0]] + [c for c in above if c is not hires_pool[0]]
        return PickResult(hires_pool[0], "高音质（Hi-Res/无损）", above[:5],
                          pool=_pool[:pool_n],
                          scores={c.bvid: _sc.get(c.bvid, 0.0) for c in _pool[:pool_n]})

    # CAND(2): 正常分支把排序后的前 10 个作为候选池；card/reason/排序规则不变
    return PickResult(above[0], "加权得分最高", above[:5], pool=above[:pool_n],
                      scores={c.bvid: _sc.get(c.bvid, 0.0) for c in above[:pool_n]})


# ---------------------------------------------------------------- 下载与转换

def _cookie_opts(workdir: Path, use_cookies: bool) -> dict[str, Any]:
    """给 yt-dlp 的 Cookie 参数。

    yt-dlp 退出时会把 Cookie 回写到 cookiefile，所以一律复制一份到临时目录
    再交给它，避免它把全局 cookies.txt 回写成旧值；程序目录只读时也走这条路。
    """
    if not use_cookies:
        return {}
    if not COOKIE_TXT.exists():
        return {"cookiesfrombrowser": ("edge",)}
    # FIX(L31): 一律交给 yt-dlp 一份临时副本，不再直接给全局 cookies.txt。
    # yt-dlp 退出时会把 cookiefile 回写（YoutubeDLCookieJar.save），扫码登录刚写入
    # 的新 Cookie 会被下载线程回写成旧值；多个下载并发时彼此也会互相覆盖。
    try:
        tmp = workdir / ".cookies.tmp.txt"
        tmp.write_bytes(COOKIE_TXT.read_bytes())
        return {"cookiefile": str(tmp)}
    except OSError:
        return {"cookiesfrombrowser": ("edge",)}


def _make_progress_hook(cancel, on_progress=None):
    """L42: 音频/视频共用的 yt-dlp 进度钩子（2% 或 0.5 秒节流，100% 必发）。"""
    _last = [0.0, -1]

    def _hook(_d) -> None:
        # yt-dlp 每写一块数据回调一次；在这里抛异常即可立刻中断下载
        if cancel is not None and cancel():
            raise Cancelled("用户停止")
        if on_progress is None:
            return
        try:
            if (_d or {}).get("status") != "downloading":
                return
            total = _d.get("total_bytes") or _d.get("total_bytes_estimate") or 0
            done = _d.get("downloaded_bytes") or 0
            if not total:
                return
            frac = min(1.0, max(0.0, float(done) / float(total)))
            pct = int(frac * 100)
            now = time.time()
            # 100% 必须发出去，否则界面停在 99%
            if pct != 100 and (pct == _last[1] or (now - _last[0] < 0.5 and pct - _last[1] < 2)):
                return
            _last[0] = now
            _last[1] = pct
            on_progress(frac)
        except Exception:
            pass

    return _hook


# ---------------------------------------------------------------- 直链并发下载
# yt-dlp 只认 playurl 返回的第一个 CDN（baseUrl）。B 站同一个音频会给
# 三四个备用域名，实测速度差 10 倍（363 KB/s vs 3636 KB/s），碰上
# mcdn.bilivideo.cn 那种 P2P 节点还会直接 Connection refused。
# 所以这里自己拿 playurl，按实测挑最快节点，再用多线程 Range 分块拉。
# 手机端移植：playurl/VIEW_API 走本文件的 _http_json（params 形式）。

_PROBE_BYTES = 262144        # 每个节点先拉 256KB 测速
_DL_THREADS = 8



# ---------------------------------------------------------------- 手机端 cookie 注入
_USER_COOKIES_LOADED = False


def _load_user_cookies() -> bool:
    """载入用户导入的 cookies.txt（登录态），搜索与下载共用。

    Windows 版由 GUI 把 cookie 文本塞进 _COOKIE_JAR；手机端的 cookies.txt
    在 App 私有目录，由 platform_compat 定位，这里统一读一次。
    """
    global _USER_COOKIES_LOADED
    if _USER_COOKIES_LOADED:
        return True
    _USER_COOKIES_LOADED = True
    cf = _pc.cookies_file()
    if cf is None:
        return False
    try:
        jar = http.cookiejar.MozillaCookieJar(str(cf))
        jar.load(ignore_discard=True, ignore_expires=True)
        n = 0
        for c in jar:
            _COOKIE_JAR.set_cookie(c)
            n += 1
        return n > 0
    except Exception:
        return False


def _stream_opener():
    """给每个下载线程单独的 opener。

    urllib 的 OpenerDirector 不是线程安全的（内部 handler 有共享状态），
    8 个线程共用一个迟早会互相踩；但 cookie jar 可以共享，它自己带锁。
    """
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(_COOKIE_JAR))
    op.addheaders = [("User-Agent", UA)]
    return op



def video_pages(bvid: str) -> list[dict[str, Any]]:
    """取分P列表：[{page, part, cid, duration}]。单P视频也返回一条。

    多P视频的音频直链是按 cid 取的，所以要让用户选「下第几集」，
    必须先知道每一集的 cid。
    """
    try:
        data = _http_json(VIEW_API, {"bvid": str(bvid)}, sign=False,
                          referer="https://www.bilibili.com/")
    except Exception:
        return []
    d = data.get("data") or {}
    pages = d.get("pages") or []
    if pages:
        out = []
        for i, x in enumerate(pages):
            out.append({
                "page": int(x.get("page") or i + 1),
                "part": str(x.get("part") or ""),
                "cid": int(x.get("cid") or 0),
                "duration": int(x.get("duration") or 0),
            })
        return out
    cid = int(d.get("cid") or 0)
    if not cid:
        return []
    return [{"page": 1, "part": str(d.get("title") or ""),
             "cid": cid, "duration": int(d.get("duration") or 0)}]


def dash_audio_urls(bvid: str, cid: int = 0) -> tuple[list[str], int, str]:
    """返回 (候选 URL 列表[最快在前], 音频流 id, 说明)。

    cid 传了就直接用（多P视频选的那一集），否则取第一集。
    """
    if not cid:
        info = _http_json(VIEW_API, {"bvid": bvid}, sign=False)
        if info.get("code") != 0:
            raise RuntimeError(f"取视频信息失败 code={info.get('code')}")
        data = info.get("data") or {}
        cid = data.get("cid") or 0
        if not cid:
            pages = data.get("pages") or []
            cid = pages[0].get("cid", 0) if pages else 0
    if not cid:
        raise RuntimeError("取不到 cid")

    pu = _http_json("https://api.bilibili.com/x/player/playurl",
                    {"bvid": bvid, "cid": str(cid), "fnval": "4048", "fourk": "1"},
                    sign=False, referer="https://www.bilibili.com/")
    if pu.get("code") != 0:
        raise RuntimeError(f"取播放地址失败 code={pu.get('code')}")
    dash = (pu.get("data") or {}).get("dash") or {}
    audios = dash.get("audio") or []
    if not audios:
        raise RuntimeError("这个视频没有 DASH 音频流")
    # id 越大音质越好（30250 Hi-Res > 30280 > 30232），同 id 取码率高者
    audios.sort(key=lambda a: (a.get("id", 0), a.get("bandwidth", 0)), reverse=True)
    best = audios[0]
    urls = [u for u in ([best.get("baseUrl") or best.get("base_url") or ""]
                        + list(best.get("backupUrl") or best.get("backup_url") or []))
            if u]
    if not urls:
        raise RuntimeError("播放地址为空")
    return urls, int(best.get("id") or 0), f"{len(urls)} 个节点"


def _pick_fast_url(urls: list[str]) -> tuple[str, int]:
    """并发测速挑最快的节点，返回 (url, 文件总字节数)。全挂了才报错。"""
    results: dict[str, tuple[float, int]] = {}
    errs: list[str] = []
    lock = threading.Lock()

    def probe(u: str) -> None:
        try:
            req = urllib.request.Request(u, headers={
                "User-Agent": UA, "Referer": "https://www.bilibili.com/",
                "Range": f"bytes=0-{_PROBE_BYTES - 1}",
            })
            t0 = time.time()
            with _stream_opener().open(req, timeout=20) as r:
                r.read(_PROBE_BYTES)
                cr = r.headers.get("Content-Range") or ""
                total = int(cr.rsplit("/", 1)[1]) if "/" in cr else int(
                    r.headers.get("Content-Length") or 0)
            kbps = _PROBE_BYTES / 1024.0 / max(time.time() - t0, 0.05)
            with lock:
                results[u] = (kbps, total)
        except Exception as e:
            with lock:
                errs.append(f"{u.split('/')[2]}:{type(e).__name__}")

    ths = [threading.Thread(target=probe, args=(u,), daemon=True) for u in urls]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    if not results:
        raise RuntimeError("所有 CDN 节点都连不上：" + "；".join(errs))
    best_url = max(results, key=lambda k: results[k][0])
    total = next((t for _, t in results.values() if t > 0), 0)
    return best_url, total


def _fetch_chunk(url: str, start: int, end: int, tries: int = 3) -> bytes:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Referer": "https://www.bilibili.com/",
                "Range": f"bytes={start}-{end}",
            })
            with _stream_opener().open(req, timeout=30) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(0.8 * (i + 1))
    return b""


def _download_dash(bvid: str, dst: Path, progress=None, cid: int = 0,
                   cancel=None) -> Path:
    """多线程 Range 拉 DASH 音频流，返回落盘文件。"""
    say = progress or (lambda *a, **k: None)
    _check(cancel)
    urls, sid, note = dash_audio_urls(bvid, cid=cid)
    say("dl", f"音频流 {sid}，{note}，测速挑节点…")
    url, total = _pick_fast_url(urls)
    host = url.split("/")[2]
    if total <= 0:
        raise RuntimeError("拿不到文件大小")
    say("dl", f"用 {host} 下载 {total / 1048576.0:.1f} MB")

    n = max(1, min(_DL_THREADS, int(total / (512 * 1024)) or 1))
    step = (total + n - 1) // n
    parts: list[bytes | None] = [None] * n
    done = [0]
    lock = threading.Lock()

    def work(i: int) -> None:
        s0 = i * step
        e0 = min(s0 + step - 1, total - 1)
        parts[i] = _fetch_chunk(url, s0, e0)
        with lock:
            done[0] += len(parts[i] or b"")
            say("dl", f"{done[0] * 100 // total}%")

    ths = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    _check(cancel)
    if any(p is None for p in parts):
        raise RuntimeError("有分块没下完")
    tmp = dst.with_suffix(".part")
    tmp.write_bytes(b"".join(p or b"" for p in parts))
    got = tmp.stat().st_size
    if got != total:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"大小对不上：{got} != {total}")
    tmp.replace(dst)
    return dst


def download_audio(bvid: str, workdir: Path, use_cookies: bool = False,
                   cancel=None, page: int = 1, on_progress=None,
                   cid: int = 0) -> Path:
    """只下音频：优先自己走 DASH 直链（挑快节点 + 多线程），失败回退 yt-dlp。

    cid  = 多P视频里选中的那一集的 cid（0 = 第一集）
    page = 第几P，仅用于 yt-dlp 回退时拼 ?p=（DASH 路径用 cid）
    """
    _check(cancel)
    workdir.mkdir(parents=True, exist_ok=True)
    _load_user_cookies()
    dst = workdir / f"{bvid}.m4a"
    try:
        if dst.exists():
            dst.unlink()
        _download_dash(bvid, dst, progress=on_progress, cid=cid, cancel=cancel)
        if dst.exists() and dst.stat().st_size > 100 * 1024:
            return dst
    except Cancelled:
        for junk in (dst, dst.with_suffix(".part")):
            try:
                if junk.exists():
                    junk.unlink()
            except Exception:
                pass
        raise
    except Exception as e:
        # 直链失败不算致命：老视频没有 DASH 流，交给 yt-dlp 兜底。
        # 但要把半成品清掉，否则 yt-dlp 的「新增文件」判定会把它当成本次产出。
        for junk in (dst, dst.with_suffix(".part")):
            try:
                if junk.exists():
                    junk.unlink()
            except Exception:
                pass
        try:
            sys.stderr.write(f"[dash] 回退 yt-dlp：{type(e).__name__}: {e}\n")
        except Exception:
            pass
    return _download_ytdlp(bvid, workdir, page=page)


def _download_ytdlp(bvid: str, workdir: Path, use_cookies: bool = True,
                    cancel=None, page: int = 1, on_progress=None) -> Path:
    """yt-dlp 兜底下载（原名 download_audio 的主体）。"""
    import yt_dlp  # 延迟导入，便于单测不依赖

    _check(cancel)
    workdir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in workdir.iterdir() if p.is_file()}
    # L42: 同名旧文件（上次进程被强杀留下的）也可能是本次覆盖写的；
    # 只看名字会把它漏掉，fresh 为空 → 误报「未产出文件」。
    _started = time.time() - 2.0
    _hook = _make_progress_hook(cancel, on_progress)

    opts: dict[str, Any] = {
        "format": "bestaudio[acodec!=flac]/bestaudio/best",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        "progress_hooks": [_hook],
    }
    if use_cookies:
        # 任何人导入的 cookies.txt 优先；没有时再回退到 Edge 登录态（本机自用）
        opts.update(_cookie_opts(workdir, use_cookies))

    url = f"https://www.bilibili.com/video/{bvid}"
    if page > 1:
        url += f"?p={int(page)}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Cancelled:
        # 清掉这一首的半成品，避免下次判重/续传时误认
        for p in workdir.iterdir():
            if p.is_file() and (p.name.startswith(bvid)
                                or p.name.endswith((".part", ".ytdl"))):
                try:
                    p.unlink()
                except Exception:
                    pass
        raise
    except Exception as exc:
        # L24: 多路并行下载时最先崩的往往是本机到 CDN 的连接（WinError 10061）。
        # 立刻降一级并发，让后面的波少撞几次，而不是四个 yt-dlp 一起退避到超时。
        if _is_conn_error(exc):
            _note_limited(force=True)
        raise

    fresh = []
    for p in workdir.iterdir():
        try:
            if not p.is_file() or p.name.startswith(".cookies"):
                continue
            if p.name.endswith((".part", ".ytdl")):
                continue
            # L42: 名字在 before 里但 mtime 更新 → 本次覆盖写的，仍算产出
            if p.name in before and p.stat().st_mtime < _started:
                continue
            fresh.append(p)
        except OSError:
            continue
    if not fresh:
        raise RuntimeError("yt-dlp 未产出文件")
    fresh.sort(key=lambda p: p.stat().st_size, reverse=True)
    return fresh[0]


AUDIO_EXTS = (".m4a", ".mp3", ".mp4")


def _tmp_out(dst: Path) -> Path:
    """给 ffmpeg 输出用的临时文件名（同目录，便于 os.replace 原子替换）。"""
    return dst.with_name(f".{dst.stem}.{os.getpid()}.{int(time.time() * 1000)}.tmp{dst.suffix}")


def _run_ffmpeg(cmd: list[str], dst: Path, cancel=None, timeout: float = 900.0) -> None:
    """跑一次 ffmpeg：轮询判断停止，超时或失败都抛异常。"""
    _check(cancel)
    # FIX(M12): 原来 stdout/stderr 都用 PIPE，却只在进程结束后才 communicate()。
    # ffmpeg 输出一多就会写满管道缓冲区，进程卡死直到 900 秒超时。改成 stderr 落
    # 临时文件（轮询期间不阻塞），失败时再读回尾部错误信息。
    err_file = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=err_file,
            creationflags=NO_WINDOW,
        )
        # 轮询而不是 wait()：停止按钮能在 0.15 秒内生效
        waited = 0.0
        while proc.poll() is None:
            if cancel is not None and cancel():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
                    # FIX(L25/B8): kill 之后也要 wait，否则 Windows 上句柄延迟释放
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
                dst.unlink(missing_ok=True)
                raise Cancelled("用户停止")
            time.sleep(0.15)
            waited += 0.15
            if waited > timeout:
                proc.kill()
                # FIX(L25/B8): kill 后 wait 一下，确保进程真的被回收
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
                dst.unlink(missing_ok=True)
                raise RuntimeError("ffmpeg 超时")
        if proc.returncode != 0:
            err_file.seek(0)
            text = err_file.read()[-300:].decode("utf-8", "ignore")
            raise RuntimeError(f"ffmpeg 失败: {text}")
    finally:
        err_file.close()


def convert_audio(src: Path, dst: Path, title: str, artist: str, fmt: str = "m4a",
                  cancel=None) -> None:
    """m4a 无损转封装；mp3 用 libmp3lame 320k 转码。cancel 命中时杀进程并抛 Cancelled。"""
    fmt = (fmt or "m4a").lower()
    cmd = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-vn", "-map", "0:a:0",
    ]
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", "320k"]
    elif probe_acodec(src) in _MP4_BAD_ACODEC:
        # B 站部分高音质视频的音轨是 FLAC，mp4/m4a 容器装不下（ffmpeg 会报
        # "Could not find tag for codec flac"），只能转成 AAC 320k
        cmd += ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"]
    else:
        cmd += ["-c:a", "copy", "-movflags", "+faststart"]
    cmd += [
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", f"album_artist={artist}",
    ]
    _check(cancel)
    # FIX(M13): 先写临时文件、成功后再原子替换。原实现用 -y 直接写目标路径：
    # 一旦 find_existing 漏判、目标其实是用户原有成品，覆盖后失败会把它一起删掉。
    tmp_dst = _tmp_out(dst)
    try:
        _run_ffmpeg(cmd + [str(tmp_dst)], tmp_dst, cancel)
        os.replace(tmp_dst, dst)
    finally:
        tmp_dst.unlink(missing_ok=True)


def convert_to_m4a(src: Path, dst: Path, title: str, artist: str) -> None:
    convert_audio(src, dst, title, artist, "m4a")


def tag_media(src: Path, dst: Path, title: str, artist: str, cancel=None) -> None:
    """视频不转码，只重封装并写标签（-c copy，几秒钟完成）；音轨是 FLAC 时转 AAC。"""
    if probe_acodec(src) in _MP4_BAD_ACODEC:
        acmd = ["-c:a", "aac", "-b:a", "320k"]
    else:
        acmd = ["-c:a", "copy"]
    cmd = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-map", "0", "-c:v", "copy", *acmd,
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", f"album_artist={artist}",
        "-movflags", "+faststart",
    ]
    # FIX(M13): 与 convert_audio 同样先写临时文件再替换，避免覆盖失败删掉用户文件
    tmp_dst = _tmp_out(dst)
    try:
        _run_ffmpeg(cmd + [str(tmp_dst)], tmp_dst, cancel)
        os.replace(tmp_dst, dst)
    finally:
        tmp_dst.unlink(missing_ok=True)


def download_video(bvid: str, workdir: Path, page: int = 1,
                   use_cookies: bool = False, cancel=None,
                   on_progress=None) -> Path:
    """用 yt-dlp 下画面+声音并合并成 mp4，返回落盘文件路径。"""
    import yt_dlp

    _check(cancel)
    workdir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in workdir.iterdir() if p.is_file()}
    _started = time.time() - 2.0
    _hook = _make_progress_hook(cancel, on_progress)

    opts: dict[str, Any] = {
        "format": "bv*+ba[acodec!=flac]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        "progress_hooks": [_hook],
        # 合并音视频必须告诉 yt-dlp 我们的 ffmpeg 在哪（打包后不在 PATH 里）
        "ffmpeg_location": str(FFMPEG.parent),
    }
    if use_cookies:
        opts.update(_cookie_opts(workdir, use_cookies))

    url = f"https://www.bilibili.com/video/{bvid}"
    if page > 1:
        url += f"?p={int(page)}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Cancelled:
        for p in workdir.iterdir():
            if p.is_file() and (p.name.startswith(bvid)
                                or p.name.endswith((".part", ".ytdl"))):
                try:
                    p.unlink()
                except Exception:
                    pass
        raise

    fresh = []
    for p in workdir.iterdir():
        try:
            if not p.is_file() or p.name.startswith(".cookies"):
                continue
            if p.name.endswith((".part", ".ytdl")):
                continue
            # L42: 名字在 before 里但 mtime 更新 → 本次覆盖写的，仍算产出
            if p.name in before and p.stat().st_mtime < _started:
                continue
            fresh.append(p)
        except OSError:
            continue
    if not fresh:
        raise RuntimeError("yt-dlp 未产出文件")
    fresh.sort(key=lambda p: p.stat().st_size, reverse=True)
    return fresh[0]


def probe_streams(path: Path) -> tuple[float, bool, bool]:
    """返回 (时长秒, 有画面, 有声音)。"""
    # FIX(L21): 与 probe_acodec 一致——ffmpeg 缺失、超时、文件读不了时不能抛，
    # 否则 video_ok 的 (False, reason) 契约被破坏，整个任务直接崩掉。
    try:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-i", str(path)],
            capture_output=True, creationflags=NO_WINDOW, timeout=120,
        )
    except Exception:
        return 0.0, False, False
    text = (proc.stderr or b"").decode("utf-8", "ignore")
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return dur, ("Video:" in text), ("Audio:" in text)


def video_ok(path: Path, expect_dur: float = 0.0) -> tuple[bool, str]:
    """视频完整性：必须同时有画面和声音，且时长与源相差 ≤5s（不做体积校验）。"""
    dur, has_v, has_a = probe_streams(path)
    if dur <= 0:
        return False, "读不到时长"
    if not has_v:
        return False, "没有画面（该视频可能只有音频）"
    if not has_a:
        return False, "没有声音"
    if expect_dur > 0 and abs(dur - expect_dur) > 5:
        return False, f"时长差 {abs(dur - expect_dur):.1f}s 超过 5s"
    return True, ""


def duration_ok(path: Path, expect_dur: float) -> tuple[bool, str]:
    """直链的音频成品：校验时长 + 能完整解码（容器元数据正确但流损坏也能查出来）。"""
    dur, _ = probe_media(path)
    if dur <= 0:
        return False, "读不到时长"
    if expect_dur > 0 and abs(dur - expect_dur) > 5:
        return False, f"时长差 {abs(dur - expect_dur):.1f}s 超过 5s"
    # FIX(M24): 原来只解析容器头。AAC/OPUS 部分帧损坏时容器时长依旧正确，
    # `-c:a copy` 也不解码，坏文件会被记成功。这里补一次完整解码（不写盘）。
    try:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-loglevel", "error",
             "-i", str(path), "-f", "null", "-"],
            capture_output=True, creationflags=NO_WINDOW,
            timeout=max(30.0, dur * 2),
        )
    except Exception as e:
        return False, f"解码校验失败：{e}"
    if proc.returncode != 0 or (proc.stderr or b"").strip():
        return False, "音频流损坏（解码有错误）"
    return True, ""


def probe_media(path: Path) -> tuple[float, int]:
    """返回 (时长秒, 容器码率 kbps)；解析失败时对应值为 0。"""
    # FIX(L21): 同 probe_streams，ffmpeg 异常/超时不能往外抛
    try:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-i", str(path)],
            capture_output=True, creationflags=NO_WINDOW, timeout=60,
        )
    except Exception:
        return 0.0, 0
    text = (proc.stderr or b"").decode("utf-8", "ignore")
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    kb = 0
    mb = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    if mb:
        kb = int(mb.group(1))
    return dur, kb


_MP4_BAD_ACODEC = {"flac", "opus", "vorbis", "pcm_s16le", "pcm_s24le", "pcm_f32le"}


def probe_acodec(path: Path) -> str:
    """取第一个音轨的编码名（flac / aac / opus …），解析失败返回空串。"""
    try:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-i", str(path)],
            capture_output=True, creationflags=NO_WINDOW, timeout=60,
        )
    except Exception:
        return ""
    text = (proc.stderr or b"").decode("utf-8", "ignore")
    m = re.search(r"Audio:\s*([A-Za-z0-9_]+)", text)
    return m.group(1).lower() if m else ""


def audio_ok(path: Path, expect_dur: float = 0.0) -> tuple[bool, str]:
    """完整性：体积须落在「码率 × 时长」的 0.7~1.2 倍；时长差 ≤5s。"""
    try:
        size = path.stat().st_size
    except Exception:
        return False, "文件不存在"
    if size <= 0:
        return False, "文件为空"
    dur, kb = probe_media(path)
    if dur <= 0:
        # FIX(M25): 原来「读不到时长但 ≥100KiB」直接 return True，坏文件能混过去。
        # 正常的 m4a/mp3 由 ffmpeg 解析时长不会失败，读不到就只能是文件坏了。
        return False, "读不到时长（文件可能已损坏）"
    if expect_dur > 0 and abs(dur - expect_dur) > 5:
        return False, f"时长差 {abs(dur - expect_dur):.1f}s 超过 5s"
    if kb > 0:
        expect = kb * 1000 / 8 * dur
        lo, hi = 0.7 * expect, 1.2 * expect
        if not (lo <= size <= hi):
            return False, (f"体积 {size / 1048576:.2f}MB 不在 "
                           f"{lo / 1048576:.2f}~{hi / 1048576:.2f}MB 之间")
    else:
        # FIX(M25): 读不到码率时原来完全不做体积校验，空壳文件也能过。
        # 退化成「至少要有 32KB」的底线检查（32kbps × 30s 的粗下限）。
        if size < 32 * 1024:
            return False, f"体积过小（{size} 字节）且读不到码率"
    return True, ""


# ---------------------------------------------------------------- 台账
# catalog.json：历史台账，只追加；文件被删后给对应记录标 deleted，不丢历史
# compare.json：现状索引，随目录变化（惰性对账），用于判重

_INDEX_CACHE: dict[str, tuple[float, int, dict[str, dict[str, Any]]]] = {}


def clear_index_cache() -> None:
    """清掉 compare.json 的解析缓存。

    用户可能在资源管理器里删/改名成品，而 compare.json 的 mtime+size 没变时
    缓存会继续返回旧内容。试搜与下载开始前调用一次，强制以磁盘为准。
    """
    _INDEX_CACHE.clear()


def catalog_path(out_dir: Path) -> Path:
    return out_dir / "catalog.json"


def compare_path(out_dir: Path) -> Path:
    return out_dir / "compare.json"


_WRITE_LOCK = threading.Lock()


def _atomic_write(path: Path, text: str) -> None:
    # FIX(L17): 固定 `.tmp` 名在并发/多开同一输出目录时会互踩（后写的覆盖先写的，
    # 甚至 replace 到半截内容）。临时名改成 pid + 纳秒时间戳，并用进程内锁串行化。
    with _WRITE_LOCK:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def load_catalog(out_dir: Path) -> list[dict[str, Any]]:
    p = catalog_path(out_dir)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def append_catalog(out_dir: Path, entry: dict[str, Any]) -> None:
    # L24: 并行下载时会同时追加，必须串行化，否则后写的把先写的整段冲掉
    with _DISK_LOCK:
        rows = [r for r in load_catalog(out_dir) if r.get("file") != entry.get("file")]
        rows.append(entry)
        try:
            _atomic_write(catalog_path(out_dir),
                          json.dumps(rows, ensure_ascii=False, indent=2))
        except Exception as exc:
            _log_swallowed("append_catalog", exc)


def load_compare(out_dir: Path) -> dict[str, dict[str, Any]]:
    """读现状索引：{文件名: {bv, bytes, at}}。带 mtime+size 缓存，批量时不重复解析。"""
    p = compare_path(out_dir)
    try:
        if not p.exists():
            return {}
        st = p.stat()
        key = str(p)
        hit = _INDEX_CACHE.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        elif isinstance(raw, list):  # 兼容只存文件名列表的旧格式
            data = {str(k): {} for k in raw}
        else:
            data = {}
        _cache_put(_INDEX_CACHE, key, (st.st_mtime, st.st_size, data), _CACHE_LIMIT_INDEX)
        return data
    except Exception as exc:
        _log_swallowed("load_compare", exc)
        return {}


def save_compare(out_dir: Path, data: dict[str, dict[str, Any]]) -> None:
    # L24: 并行时 compare.json 也会被多个线程整份覆盖
    with _DISK_LOCK:
        p = compare_path(out_dir)
        try:
            _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
            try:
                st = p.stat()
                _cache_put(_INDEX_CACHE, str(p), (st.st_mtime, st.st_size, dict(data)), _CACHE_LIMIT_INDEX)
            except OSError:
                _INDEX_CACHE.pop(str(p), None)
        except Exception as exc:
            _log_swallowed("save_compare", exc)
            _INDEX_CACHE.pop(str(p), None)


def ensure_index_files(out_dir: Path) -> None:
    """输出目录首次使用时生成 catalog.json 与 compare.json。"""
    try:
        if not catalog_path(out_dir).exists():
            _atomic_write(catalog_path(out_dir), "[]")
        if not compare_path(out_dir).exists():
            _atomic_write(compare_path(out_dir), "{}")
    except Exception:
        pass


def scan_audio(out_dir: Path) -> dict[str, int]:
    """目录里的音频：{文件名: 字节数}。目录本身枚举不了时抛 OSError，交给调用方决定。"""
    found: dict[str, int] = {}
    # FIX(M35): 原来外层 `except Exception: pass`。网络盘/可移动盘瞬断或权限变化时
    # 会返回一份不完整的列表，reconcile_compare 拿它当「这些文件真的没了」，于是
    # 删掉 compare 里的 BV/时间记录、给 catalog 误打 deleted。枚举失败必须上抛。
    for p in out_dir.iterdir():
        if p.suffix.lower() not in AUDIO_EXTS:
            continue
        try:
            if not p.is_file():   # 跳过名字像音频的目录
                continue
            found[p.name] = p.stat().st_size
        except OSError:
            continue
    return found


def _reconcile_catalog(out_dir: Path, files: dict[str, int]) -> None:
    """catalog 只标记不删除：文件没了标 deleted，回来了再撤掉标记。"""
    # L24: 与 append_catalog 共用一把锁，避免对账时把刚追加的记录冲掉
    with _DISK_LOCK:
        rows = load_catalog(out_dir)
        if not rows:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        changed = False
        for r in rows:
            name = str(r.get("file") or "")
            if not name:
                continue
            alive = name in files
            if not alive and not r.get("deleted"):
                r["deleted"] = True
                r["deleted_at"] = stamp
                changed = True
            elif alive and r.get("deleted"):
                r.pop("deleted", None)
                r.pop("deleted_at", None)
                changed = True
        if changed:
            try:
                _atomic_write(catalog_path(out_dir),
                              json.dumps(rows, ensure_ascii=False, indent=2))
            except Exception:
                pass


def reconcile_compare(out_dir: Path) -> dict[str, dict[str, Any]]:
    """惰性对账：以目录为准。删掉已不存在的，收编新出现的，再同步 catalog 的标记。"""
    # L37: 目录扫描移出锁。scan_audio 是大目录里最慢的一步（只读目录、不碰索引），
    # 以前整段放进 _DISK_LOCK，主线程对账时下载线程写台账要排队、反之亦然。
    # 现在扫描在锁外，锁内只做「读索引 → 合并 → 写索引」；并发两次对账最多多扫一次，
    # 结果以锁内最后一次写为准，下次对账自动修正。
    try:
        files: dict[str, int] | None = scan_audio(out_dir)
    except Exception:
        files = None
    with _DISK_LOCK:
        return _reconcile_compare_locked(out_dir, files)


def _reconcile_compare_locked(out_dir: Path,
                              files: dict[str, int] | None = None) -> dict[str, dict[str, Any]]:
    ensure_index_files(out_dir)
    data = {k: dict(v) for k, v in load_compare(out_dir).items()}
    if files is None:
        # FIX(M35): 目录枚举失败时直接返回现有索引，不做任何删除/标记。
        try:
            files = scan_audio(out_dir)
        except Exception:
            return data
    changed = False
    # L38: files 是锁外扫出来的快照，可能早于「刚下载完成、索引已写」的那一条。
    # 删除前按磁盘复核一次；快照里没有、磁盘上确实在的名字记为存活，
    # 并把这份存活名单交给 _reconcile_catalog，避免同一份旧快照把它标成 deleted。
    alive: dict[str, int] = dict(files)
    for name in [n for n in data if n not in files]:
        path = out_dir / name
        try:
            ok = path.is_file()
            size = path.stat().st_size if ok else 0
        except OSError:
            ok = False
            size = 0
        if ok:
            alive[name] = size
        else:
            del data[name]
            changed = True
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    for name, size in files.items():
        if name not in data:
            data[name] = {"bv": "", "bytes": size, "at": stamp}
            changed = True
    if changed:
        save_compare(out_dir, data)
    _reconcile_catalog(out_dir, alive)
    return data


def record_download(out_dir: Path, entry: dict[str, Any]) -> None:
    """下载成功：写历史台账，同时更新现状索引。"""
    # L24: 台账与索引必须当成一个事务，否则并行时会丢记录
    with _DISK_LOCK:
        append_catalog(out_dir, entry)
        data = {k: dict(v) for k, v in load_compare(out_dir).items()}
        data[str(entry.get("file", ""))] = {
            "bv": str(entry.get("bv", "")),
            "bytes": int(entry.get("bytes", 0) or 0),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            # FIX(H8): 长歌名的成品名会被 120 字节截断，靠文件名还原不出原歌名，
            # 判重就永远失配。把歌名/作者原样存进索引，find_existing 才能准确比对
            # （同时避免同名不同作者被误判，见 H1）。
            "song": str(entry.get("song", "") or ""),
            "artist": str(entry.get("artist", "") or ""),
        }
        save_compare(out_dir, data)


def _rename_in_catalog(out_dir: Path, old: str, new: str) -> None:
    """FIX(M29): 成品被改名后 catalog 里还是旧文件名，历史台账与目录对不上。"""
    try:
        with _DISK_LOCK:   # L24: 与追加/对账互斥
            rows = load_catalog(out_dir)
            hit = False
            for r in rows:
                if str(r.get("file") or "") == old:
                    r["file"] = new
                    r["renamed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    hit = True
            if hit:
                _atomic_write(catalog_path(out_dir),
                              json.dumps(rows, ensure_ascii=False, indent=2))
    except Exception as exc:
        _log_swallowed("rename_in_catalog", exc)


def _strip_copy_suffix(stem: str) -> str:
    """FIX(L35/B8)：去掉撞名时加上的「 (2)」序号，还原成歌名（或 歌名+作者）。

    只剥纯数字序号——歌名本身带括号的（如「七里香（现场）」）不会被误伤。
    """
    return re.sub(r"\s*\(\d{1,3}\)$", "", str(stem or ""))


def find_existing(out_dir: Path,
                  song: str,
                  index: dict[str, Any] | None = None,
                  artist: str = "") -> Path | None:
    """幂等：同名（且同作者）的成品已存在则返回它。走 compare 索引，命中后确认文件真的在。"""
    want_song = normalize(song)
    if not want_song:
        return None
    want_artist = normalize(sanitize_filename(artist)) if artist else ""
    if index is not None:
        data = {k: dict(v) if isinstance(v, dict) else {} for k, v in index.items()}
    else:
        # FIX(M27): 原来 compare 非空就不对账，手工放进输出目录的新文件永远收编不进来。
        # 每首歌最多调一两次，这里统一走一次惰性对账（1000 文件约 60ms）。
        data = reconcile_compare(out_dir)
    for name, rec in data.items():
        if Path(name).suffix.lower() not in AUDIO_EXTS:
            continue
        stem = Path(name).stem
        # FIX(H1): 以前只按「歌名+」前缀匹配、完全不看作者，同名不同曲会被当成已下载而跳过。
        # 现在优先用索引里记录的歌名/作者比对；老索引没有这些字段时退回文件名比较。
        rec_song = str(rec.get("song") or "")
        rec_artist = str(rec.get("artist") or "")
        if rec_song:
            same_song = normalize(rec_song) == want_song
        else:
            # 文件名形如「歌名+作者」。从最后一个 `+` 切，歌名里本身带 `+` 也不会错切；
            # 也顺带修了 L18：查歌名 `A` 时不再把 `A+B+C.m4a` 误判成 A。
            # L33：新文件名的 stem 就是歌名；老文件仍是「歌名+作者」，先比全名再退回切分
            # 注意必须用 rsplit 取 head（`A+B+C.m4a` → `A+B`），否则查 A 会误判成 A+B+C。
            # FIX(L35/B8): 索引被重建（对账收编）后这些记录没有 song 字段，只能靠文件名；
            # 而撞名时下载会落成「歌名 (2).m4a」，不剥掉序号就永远匹配不上，导致重复下载。
            bare = _strip_copy_suffix(stem)
            same_song = (normalize(stem) == want_song
                         or normalize(bare) == want_song
                         or normalize(stem.rsplit("+", 1)[0]) == want_song
                         or normalize(bare.rsplit("+", 1)[0]) == want_song)
        if not same_song:
            continue
        if want_artist:
            if not rec_artist and "+" in stem:
                rec_artist = stem.rsplit("+", 1)[1]
            if rec_artist and normalize(sanitize_filename(rec_artist)) != want_artist:
                continue  # 同名不同作者：不是同一个成品，不能算已存在
        p = out_dir / name
        try:
            # FIX(M28): 原来只看 exists()，恰好同名的目录也会被当成成品返回。
            if p.is_file():
                return p
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- 主流程

@dataclass
class SongResult:
    song: str
    status: str          # ok / skip / fail / preview
    reason: str = ""
    file: str = ""
    bvid: str = ""
    artist: str = ""
    pending: bool = False  # 曲师未查到，暂用 UP 主名
    video_title: str = ""  # 选中的视频标题，写 error.log 用
    # CAND(7): 试搜返回的候选池（界面「先试搜、点选确认」用），只作展示不参与下载
    candidates: list[dict] = field(default_factory=list)
    # L20: 失败细化。kind 是机器码（见 FAIL_KINDS），detail 是原始错误一行；
    # 界面「说明」列显示中文标签，完整详情写 debug.log，便于用户自助排查。
    fail_kind: str = ""
    fail_detail: str = ""


# L20: 失败分类 → 中文标签
FAIL_KINDS: dict[str, str] = {
    "no_result": "无搜索结果",
    "risk_control": "被风控降级",
    "http_error": "接口异常",
    "no_bvid": "无法确定 BV",
    "no_audio": "无音频流",
    "forbidden": "403 拒绝",
    "link_expired": "直链失效",
    "converter": "转封装失败",
    "disk": "磁盘写入失败",
    "duration_mismatch": "时长校验不过",
    "download_other": "下载失败",
    "user_skip": "用户跳过",
}


def fail_label(kind: str) -> str:
    """把 fail_kind 翻成给人看的中文标签；未知码原样返回。"""
    return FAIL_KINDS.get(str(kind or ""), str(kind or ""))


# L32：可自动重试一次的失败类型。只覆盖「网络/直链抖动」这类重试有可能好的情况；
# 磁盘满、转封装坏、时长不符、无音频流重试没有意义，不在此列。
RETRY_FAIL_KINDS = frozenset({"link_expired", "forbidden", "http_error",
                              "download_other"})


def _classify_search_reason(reason: str) -> str:
    """L20: 搜索失败原因归类（风控 / 接口异常 / 真无结果）。"""
    t = str(reason or "")
    if "v_voucher" in t or "风控" in t or "-1200" in t or "-352" in t:
        return "risk_control"
    if any(k in t for k in ("412", "429", "503", "HTTP", "Timeout", "timed out")):
        return "http_error"
    return "no_result"


def _classify_download_error(exc: BaseException) -> str:
    """L20: 下载/转换异常归类。"""
    msg = f"{type(exc).__name__}: {exc}".lower()
    if "403" in msg or "forbidden" in msg:
        return "forbidden"
    if ("disk" in msg or "no space" in msg or "permission" in msg
            or "winerror 5" in msg or "winerror 112" in msg):
        return "disk"
    if "duration" in msg or "时长" in msg:
        return "duration_mismatch"
    if "convert" in msg or "ffmpeg" in msg or "转封装" in msg:
        return "converter"
    if "audio" in msg or "音频" in msg:
        return "no_audio"
    if "404" in msg or "410" in msg or "expire" in msg or "直链" in msg:
        return "link_expired"
    return "download_other"


class Cancelled(Exception):
    """用户点了停止。调用方捕获后把当前首标为「已停止」，不算失败。"""


def _check(cancel) -> None:
    """cancel 是 0 参可调用（通常是 stop_flag.is_set）；命中即抛 Cancelled。"""
    if cancel is not None:
        try:
            hit = bool(cancel())
        except Exception:
            hit = False
        if hit:
            raise Cancelled("用户停止")


# ---------------------------------------------------------------- 联网查曲师

NETEASE_SEARCH = "https://music.163.com/api/search/get"
QQ_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp"
ITUNES_SEARCH = "https://itunes.apple.com/search"

# 源优先级：用户写的 > B 站曲库（L22，用户指定最高）> QQ > 网易云 > Apple 台/港/日
_ARTIST_SOURCES = ("bili_music", "qq", "netease", "apple_tw", "apple_hk", "apple_jp")
_ARTIST_DUR_TOL = 15  # 秒：同名不同曲判定阈值


def _split_multi_artist(name: str) -> str:
    """「A/B/C」「A, B & C」这类多艺人串，只取第一个。"""
    s = re.split(r"[/,;&、]|feat\.|ft\.", str(name or ""), maxsplit=1)[0]
    return s.strip()


def _title_ok(na: str, nb: str) -> bool:
    # FIX(M32): 原来是双向裸子串判定，`lemon` 与 `lemonade` 会互判为同一首，
    # 音乐库结果被错误采信。短串必须是长串里独立的一段：前后不能紧贴 ASCII 字母数字。
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    start = long_.find(short)
    while start >= 0:
        before = long_[start - 1] if start > 0 else ""
        after = long_[start + len(short)] if start + len(short) < len(long_) else ""
        if not (_ascii_alnum(before) or _ascii_alnum(after)):
            return True
        start = long_.find(short, start + 1)
    return False


def _from_netease(song: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    body = urllib.parse.urlencode({"s": song, "type": 1, "limit": 5, "offset": 0}).encode()
    req = urllib.request.Request(
        NETEASE_SEARCH, data=body,
        headers={"User-Agent": UA, "Referer": "https://music.163.com/"},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    nb = normalize(song)
    for s in ((data.get("result") or {}).get("songs") or [])[:5]:
        if not _title_ok(normalize(str(s.get("name") or "")), nb):
            continue
        arts = [str(a.get("name") or "").strip() for a in (s.get("artists") or [])]
        arts = [a for a in arts if a]
        if not arts:
            continue
        pick = arts[0]
        if is_vocaloid_or_inst(song):
            for a in arts:
                if VOCALOID_RE.search(a):
                    pick = a
                    break
        out.append((_split_multi_artist(pick), int((s.get("duration") or 0) // 1000)))
    return out


def _from_qq(song: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    q = urllib.parse.urlencode({"w": song, "p": 1, "n": 5, "format": "json"})
    req = urllib.request.Request(
        QQ_SEARCH + "?" + q,
        headers={"User-Agent": UA, "Referer": "https://y.qq.com/"},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    nb = normalize(song)
    rows = ((data.get("data") or {}).get("song") or {}).get("list") or []
    for s in rows[:5]:
        if not _title_ok(normalize(str(s.get("songname") or "")), nb):
            continue
        names = [_split_multi_artist(x.get("name") or "") for x in (s.get("singer") or [])]
        names = [x for x in names if x]
        if not names:
            continue
        pick = names[0]
        if is_vocaloid_or_inst(song):
            for x in names:
                if VOCALOID_RE.search(x):
                    pick = x
                    break
        out.append((pick, int(s.get("interval") or 0)))
    return out


def _from_itunes(song: str, country: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    q = urllib.parse.urlencode({"term": song, "entity": "song", "country": country, "limit": 5})
    req = urllib.request.Request(ITUNES_SEARCH + "?" + q, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    nb = normalize(song)
    for x in (data.get("results") or [])[:5]:
        if not _title_ok(normalize(str(x.get("trackName") or "")), nb):
            continue
        a = _split_multi_artist(x.get("artistName") or "")
        if not a:
            continue
        out.append((a, int((x.get("trackTimeMillis") or 0) // 1000)))
    return out


# ---------------------------------------------------------------- L22 曲库作者源
# 用户 2026-11-09 定：B 站曲库是**最高优先级**作者源，取不到才回退 QQ/Apple/网易云。
# 链路（本机实测可用）：视频 → view/detail/tag 里 tag_type=bgm 的那项拿 music_id
# → bgm/detail 拿 origin_artist（真人）/ album。注意 bgm/detail **没有 singer 键**，
# singer 只出现在榜单 toplist/music_list 里，两处别混。
MUSIC_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
MUSIC_TAG_API = "https://api.bilibili.com/x/web-interface/view/detail/tag"
MUSIC_DETAIL_API = "https://api.bilibili.com/x/copyright-music-publicity/bgm/detail"
_MUSIC_CACHE: dict[str, tuple[float, list[tuple[str, int]]]] = {}
_MUSIC_CACHE_TTL = 1800
_MUSIC_BUDGET = 8.0      # 秒：这个源的总预算，超了直接放弃，不拖慢其它源
_MUSIC_MAX_CARDS = 2     # 最多探测前几个候选视频（每个约 3 次请求、3.6 秒）


def bili_music_info(bvid: str, deadline: float | None = None) -> dict[str, str] | None:
    """按视频取曲库信息：`{music_id, title, artist, album}`，取不到返回 None。

    `deadline` 是 time.time() 的绝对时刻，用于给整个调用封顶：三个接口串联时
    每次请求前按剩余时间收紧 timeout，避免把上层预算（`_MUSIC_BUDGET`）冲垮。

    实测注意：曲库元数据不能盲信——「关山酒」曲库登记原唱「等什么君」，而同曲的
    小魂版 MV 也挂在同一 music_id 下（`mv_bvid` 指向小魂版），曲库自身就自相矛盾。
    所以这里的作者只当高可信建议，按用户指令作为最高优先级作者源，但不覆盖用户手写作者。
    """
    bv = str(bvid or "").strip()
    if not bv:
        return None

    def _left() -> float:
        """本次请求可用的超时秒数：默认 15，有 deadline 时按剩余时间收紧。"""
        if deadline is None:
            return 15.0
        return max(1.0, min(15.0, deadline - time.time()))

    def _out_of_time() -> bool:
        return deadline is not None and time.time() >= deadline

    if _out_of_time():
        return None
    # FIX(L22): 只传 bvid 调 view/detail/tag 时，返回的是另一套标签（topic/old_channel），
    # **不含 bgm 项**；必须先 view 拿 aid+cid，再用 aid+cid 查才会带「发现《xx》」。
    try:
        view = _http_json(MUSIC_VIEW_API, {"bvid": bv}, retries=1, sign=False,
                          timeout=_left())
    except Exception:
        return None
    vd = view.get("data") or {}
    aid, cid = vd.get("aid"), vd.get("cid")
    if not aid or not cid:
        return None
    if _out_of_time():
        return None
    try:
        tag = _http_json(MUSIC_TAG_API, {"aid": aid, "cid": cid}, retries=1, sign=False,
                         timeout=_left())
    except Exception:
        return None
    mid = ""
    for t in (tag.get("data") or []):
        if str(t.get("tag_type") or "") == "bgm" and t.get("music_id"):
            mid = str(t["music_id"])
            break
    if not mid:
        return None
    if _out_of_time():
        return None
    try:
        det = _http_json(MUSIC_DETAIL_API, {"music_id": mid}, retries=1, sign=False,
                         timeout=_left(), referer="https://music.bilibili.com/")
    except Exception:
        return None
    data = det.get("data") or {}
    title = str(data.get("music_title") or "").strip()
    artist = str(data.get("origin_artist") or "").strip()
    if not artist:
        # 真人歌手在 artists_list，厂牌常出现在 artists，两个都兜一遍
        for key in ("artists_list", "artists"):
            for it in (data.get(key) or []):
                nm = str((it or {}).get("name") or "").strip()
                if nm:
                    artist = nm
                    break
            if artist:
                break
    if not (title or artist):
        return None
    return {"music_id": mid, "title": title, "artist": artist,
            "album": str(data.get("album") or "").strip()}


def _from_bili_music(song: str) -> list[tuple[str, int]]:
    """曲库作者源：搜前几个候选视频，取它挂载曲目登记的原唱。"""
    key = normalize(song)
    if not key:
        return []
    try:
        if not load_settings().get("bili_music", True):
            return []
    except Exception:
        pass
    hit = _MUSIC_CACHE.get(key)
    if hit:
        # 成功的缓存半小时；失败的只缓存 1 分钟——一次网络抖动不该让这首歌整轮都没曲库作者
        ttl = _MUSIC_CACHE_TTL if hit[1] else 60
        if time.time() - hit[0] < ttl:
            return list(hit[1])
    out: list[tuple[str, int]] = []
    t0 = time.time()
    deadline = t0 + _MUSIC_BUDGET
    try:
        # FIX(L25): 给曲库源的搜索单独收紧超时，否则默认 15s×3 次重试能吃掉 45s，
        # 上层 12s 的 as_completed 预算根本兜不住。
        cards = search_page(song, 1, "click", timeout=5)[:_MUSIC_MAX_CARDS]
    except Exception:
        cards = []
    for c in cards:
        if time.time() >= deadline:
            break
        # 标题里没有歌名的候选不可能是这首歌，别浪费请求
        if not c.bvid or key not in normalize(c.title):
            continue
        info = bili_music_info(c.bvid, deadline=deadline)
        if not info:
            continue
        # 曲库歌名也得对得上，防止标签挂到同名的另一首作品
        if info["title"] and not _title_ok(normalize(info["title"]), key):
            continue
        a = info["artist"]
        if a and artist_ok(song, a):
            # 时长给 0：视频可能是剪辑版，别让它污染「同名曲主流时长」的众数簇
            out.append((_split_multi_artist(a), 0))
            break
    _cache_put(_MUSIC_CACHE, key, (time.time(), out), _CACHE_LIMIT_MUSIC)
    return out


def lookup_artist_candidates(song: str, duration: int = 0) -> list[tuple[str, str, int]]:
    """并行查多源作者，返回 [(作者, 来源, 时长)]。

    duration>0 时，与视频时长差超过 15 秒的候选判为同名不同曲，直接丢弃。
    """
    jobs = [
        # L22：曲库源（用户指定最高优先级）。它内部自己搜一次，取不到就返回空，
        # 不会影响下面几个源——真正的取舍在 _ARTIST_SOURCES / artist_from_mus。
        ("bili_music", lambda: _from_bili_music(song)),
        ("qq", lambda: _from_qq(song)),
        ("netease", lambda: _from_netease(song)),
        ("apple_tw", lambda: _from_itunes(song, "tw")),
        ("apple_hk", lambda: _from_itunes(song, "hk")),
        ("apple_jp", lambda: _from_itunes(song, "jp")),
    ]
    found: dict[str, list[tuple[str, int]]] = {}
    ex = None
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ex = ThreadPoolExecutor(max_workers=len(jobs))
        futs = {ex.submit(fn): name for name, fn in jobs}
        try:
            # 曲库源有独立预算（_MUSIC_BUDGET），这里放宽到 12s 等它跑完
            for f in as_completed(futs, timeout=12):
                # 单个源失败不能影响其它源：QQ 接口下线时正是这里踩过坑
                try:
                    found[futs[f]] = f.result() or []
                except Exception:
                    found[futs[f]] = []
        except Exception:
            pass
    except Exception:
        pass
    finally:
        # FIX(L25): 这里绝不能用 `with` —— 退出时 shutdown(wait=True) 会把超时的
        # 慢源全部等完，as_completed 的 timeout 形同虚设（实测 12s 预算被 15s 的
        # 慢源拖成 15.02s，用户看到的是一整段无响应的等待）。
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:      # Python < 3.9 没有 cancel_futures
                try:
                    ex.shutdown(wait=False)
                except Exception:
                    pass
            except Exception:
                pass

    out: list[tuple[str, str, int]] = []
    for name in _ARTIST_SOURCES:
        for artist, dur in found.get(name, []):
            if duration > 0 and dur > 0 and abs(dur - duration) > _ARTIST_DUR_TOL:
                continue
            out.append((artist, name, dur))
    return out


def lookup_artist(song: str) -> tuple[str, str]:
    """兼容旧接口：返回第一个候选。"""
    for a, s, _d in lookup_artist_candidates(song):
        return a, s
    return "", ""


# ---------------------------------------------------------------- L14 试搜流程
_KANA_RX = re.compile(r"[\u3040-\u30ff]")
# 白名单 UP 主：名字必须完全一致（不做包含匹配），优先级高于「完全命中」。
WL_UPS = ("JLRS日落fm", "JLRS-LeoFM", "JLRS-jayfm")


def is_whitelist_up(author: str) -> bool:
    """白名单 UP 主：完全一致才算，多一个空格都不算。"""
    return str(author or "").strip() in WL_UPS


# L15：白名单视频标题里出现这些词 → 多半是合唱/翻唱/改编/器乐/鬼畜，
# 取消该视频的白名单优先，回到正常筛选顺序。
_WL_BLOCK_TERMS = (
    "合唱", "合唱版", "三人合唱", "多人合唱", "大合唱", "万人合唱", "接力合唱", "接龙",
    "全员合唱", "群星", "分声部", "多声部", "三声部", "混声合唱", "轮唱", "和声",
    "伴唱", "阿卡贝拉", "无伴奏", "纯人声", "单人阿卡贝拉",
    "翻唱", "翻唱版", "男声版", "女声版", "童声版", "戏腔版", "升key", "降key",
    "变速", "加速", "清唱", "试唱", "纯享版",
    "现场版", "舞台版", "导师互唱", "改编", "重编曲", "混音", "填词", "中文填词",
    "翻填", "空耳", "串烧", "组曲", "重置版",
    "伴奏", "消音", "纯音乐", "无人声", "卡拉OK",
    "调教", "声库", "AI翻唱", "AI音乐", "鬼畜调教", "音MAD", "人力VOCALOID",
    "演奏", "钢琴版", "指弹", "民乐版", "古筝", "纯器乐",
)
_WL_BLOCK_ASCII = ("cover", "demo", "mashup", "ace", "diffsinger", "mmd")

_WL_CJK_RX = "|".join(re.escape(w) for w in
                      sorted(_WL_BLOCK_TERMS, key=len, reverse=True))
_WL_ASCII_RX = "|".join(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])"
                        for w in _WL_BLOCK_ASCII)
# CJK 词条直接包含匹配；英文词条加词边界，避免 Space 里的 ace 之类误伤。
WL_BLOCK_RX = re.compile(f"{_WL_CJK_RX}|{_WL_ASCII_RX}", re.IGNORECASE)


def wl_blocked(title: str) -> bool:
    """标题命中合唱/翻唱/改编等词条 → 不享受白名单优先。"""
    return bool(WL_BLOCK_RX.search(str(title or "")))


def _apple_countries(song: str) -> list[str]:
    """Apple 区域优先序：日文名→日本区优先；含繁体字→台/港同级；其余台/港/日。"""
    s = str(song or "")
    if _KANA_RX.search(s):
        return ["jp", "tw", "hk"]
    if any(ch in VARIANT for ch in s):
        return ["tw", "hk", "jp"]
    return ["tw", "hk", "jp"]


def artist_from_mus(mus_all: list[tuple[str, str, int]],
                    song: str) -> tuple[str, str]:
    """从多源结果里按优先级取作者：曲库（L22）→ QQ → Apple → 网易云，各源取第一条。"""
    def first(src: str) -> str:
        for a, s, _d in mus_all:
            if s == src and a and artist_ok(song, a):
                return a
        return ""

    # L22：用户指定 B 站曲库为最高优先级；它没返回结果时才落到下面的旧链路
    # L36：可关成「只兜底」——曲库给的是 B 站登记的官方原唱，未必是用户要的版本。
    # 关掉后仍参与校验（first() 里的 artist_ok 已做歌名过滤），但不再覆盖其它源。
    try:
        bm_override = bool(load_settings().get("bili_music_override", True))
    except Exception:
        bm_override = True
    if bm_override:
        a = first("bili_music")
        if a:
            return a, "bili_music"
    a = first("qq")
    if a:
        return a, "qq"
    for c in _apple_countries(song):
        a = first("apple_" + c)
        if a:
            return a, "apple_" + c
    a = first("netease")
    if a:
        return a, "netease"
    if not bm_override:          # L36：只兜底模式，其它源都没结果才用曲库
        a = first("bili_music")
        if a:
            return a, "bili_music"
    return "", ""


def lookup_artist_priority(song: str, duration: int = 0) -> tuple[str, str]:
    """L14 新流程的作者取法：固定优先级、每源只取第一条，命中即停。"""
    try:
        mus = lookup_artist_candidates(song, duration)
    except Exception:
        mus = []
    return artist_from_mus(mus, song)


_TITLE_BRACKET = re.compile(r"[【\[]([^】\]]{2,20})[】\]]")
_TITLE_ARTIST_BLOCK = ("官方", "高清", "无损", "4k", "mv", "动态歌词", "歌词", "完整版",
                       "修复", "中字", "字幕", "搬运", "合集", "精选", "hi-res", "pv",
                       "ep", "bgm", "ost", "纯音乐", "伴奏", "翻唱", "cover", "live", "现场")


def artist_from_title(title: str) -> str:
    """从视频标题里提取艺人：「【战双帕弥什】…EP - Revelation」→ 战双帕弥什。"""
    t = str(title or "")
    for m in _TITLE_BRACKET.finditer(t):
        seg = m.group(1).strip()
        low = seg.lower()
        if any(k in low for k in _TITLE_ARTIST_BLOCK):
            continue
        return seg
    for m in _DASH_ARTIST_RX.finditer(t):
        return m.group(1).strip()
    return ""


def save_artist(song: str, artist: str, duration: int = 0) -> bool:
    """把查到的曲师写回 artists.json。"""
    try:
        # L24: 并行批量时多首会同时写 artists.json，读-改-写必须串行
        with _DISK_LOCK:
            data = load_artists()
            key = str(song)
            dur = int(duration or 0)
            old = data.get(key, "")
            # FIX(M31): artists.json 只以歌名为 key，同名不同曲会共用一条缓存，第二首直接
            # 拿到错误作者。命中同名但作者不同时另开一条带时长的 key，后续读取只会命中
            # 「歌名」那条，不会再串用（代价是第二首每次重查，可接受）。
            if old and normalize(old) != normalize(artist) and dur > 0:
                key = f"{song}\x1f{dur}"
            data[key] = artist
            # FIX(L17): 原来直接 write_text，崩溃会留下半截 JSON，下次整份读不到。
            _atomic_write(ARTISTS_FILE, json.dumps(data, ensure_ascii=False, indent=2))
            return True
    except Exception:
        return False


VOCALOID_RE = re.compile(
    r"(初音|ミク|miku|镜音|鏡音|リン|レン|巡音|ルカ|meiko|kaito|gumi|"
    r"洛天依|乐正绫|樂正綾|言和|心华|星尘|赤羽|ボカロ|vocaloid)",
    re.IGNORECASE,
)


def is_vocaloid_or_inst(song: str) -> bool:
    """术力口 / 纯音乐：这类没有人类演唱者，作者取曲师或虚拟歌手名。"""
    s = str(song)
    if VOCALOID_RE.search(s):
        return True
    return bool(re.search(r"(inst|off\s*vocal|纯音乐|純音樂|伴奏|instrumental)", s, re.IGNORECASE))


# ---------------------------------------------------------------- 直链
# 用户主动给 B 站视频链接 / BV 号 / av 号时，不搜索、不选片，直接下这一条。
# 歌名和作者由用户填（程序不猜标题），这里只负责「认出是哪一条」。

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
_LIVE_RX = re.compile(r"(?:https?://)?live\.bilibili\.com", re.IGNORECASE)
# BV 号真实字符集：1-9A-HJ-NP-Za-km-z（不含 0、I、O、l），固定 BV1 + 9 位。
# 注意不能加 re.IGNORECASE：大小写折叠会把 I/l/O 又放回来，等于没限。
_BV_STRICT = r"BV1[1-9A-HJ-NP-Za-km-z]{9}"
_BV_LOOSE = r"BV[0-9A-Za-z]{10}"
_VIDEO_RX = re.compile(
    # 链接里的 BV：上下文已经说明意图，字符集放宽，将来 B 站换规则也不至于失效
    r"(?:https?://)?(?i:(?:www\.|m\.)?bilibili\.com)/video/(?P<bv1>" + _BV_LOOSE + r")"
    r"|(?:https?://)?(?i:(?:www\.)?b23\.tv)/(?P<short>[0-9A-Za-z]+)"
    # 裸写在文字里的 BV：用严格字符集，避免把普通歌名误判成视频
    r"|(?<![0-9A-Za-z])(?P<bv2>" + _BV_STRICT + r")(?![0-9A-Za-z])"
    r"|(?<![0-9A-Za-z])(?P<av>[Aa][Vv]\d{1,12})(?![0-9A-Za-z])"
)
_PAGE_RX = re.compile(r"[?&]p=(\d+)")


def video_ref(line: str) -> dict[str, Any] | None:
    """从一行里认出 B 站视频：返回 {kind, ref, page, span}；直播间 kind=live。"""
    s = str(line)
    if _LIVE_RX.search(s):
        return {"kind": "live", "ref": "", "page": 1, "span": (0, 0), "raw": s}
    m = _VIDEO_RX.search(s)
    if not m:
        # 整行就是一个 BV 号、但字符集不合严格规则（多半手打错一位）：
        # 仍按直链走，让用户看到「取不到视频信息」，而不是一路搜成「搜索无结果」
        t = s.strip()
        if re.fullmatch(_BV_LOOSE, t) and not re.fullmatch(_BV_STRICT, t):
            return {"kind": "bv", "ref": t, "page": 1,
                    "span": (0, len(s)), "raw": t}
        return None
    gd = m.groupdict()
    if gd.get("bv1") or gd.get("bv2"):
        kind, ref = "bv", str(gd.get("bv1") or gd.get("bv2"))
    elif gd.get("short"):
        kind, ref = "short", str(gd["short"])
    else:
        kind, ref = "av", str(gd.get("av") or "").lower()
    page = 1
    pm = _PAGE_RX.search(s[m.end():]) or _PAGE_RX.search(s)
    if pm:
        page = max(1, int(pm.group(1)))
    return {"kind": kind, "ref": ref, "page": page,
            "span": (m.start(), m.end()), "raw": m.group(0)}


def strip_video_ref(line: str, info: dict[str, Any]) -> str:
    """去掉行里的链接本体，剩下的就是用户写的「歌名+作者」。"""
    s = str(line)
    a, b = info.get("span") or (0, 0)
    if info.get("kind") == "av":
        # FIX(H9): av 链接的 match 只覆盖「av123」本身，前面的
        # `https://www.bilibili.com/video/` 会原样残留进歌名。
        m = re.search(r"https?://\S*$", s[:a])
        if m:
            a = m.start()
    if b > a:
        rest = s[b:]
        if rest[:1] in ("/", "?", "&", "#"):
            j = 0
            while j < len(rest) and not rest[j].isspace():
                j += 1
            rest = rest[j:]
        name = (s[:a] + rest).strip()
    else:
        name = ""
    name = re.sub(r"^[+＋\s]+", "", name)
    name = re.sub(r"[+＋\s]+$", "", name)
    return name.strip()


def _expand_short(short: str) -> str:
    """b23.tv 短链跟随跳转，还原出 BV / av 号（偶发慢，重试一次）。"""
    url = short if short.startswith("http") else f"https://b23.tv/{short}"
    last: Exception | None = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers=_headers())
            with _OPENER.open(req, timeout=8) as r:
                final = r.geturl()
            m = (re.search(r"/(BV[0-9A-Za-z]{10})", final)
                 or re.search(r"/(av\d+)", final, re.I))
            if m:
                return m.group(1)
            raise RuntimeError("短链无法解析出 BV 号")
        except Exception as e:
            last = e
    raise RuntimeError(f"短链解析失败：{last}")


def resolve_video(info: dict[str, Any]) -> dict[str, Any]:
    """调 view 接口拿标题/时长/UP 主；多P 时取对应分P 的标题与时长。"""
    kind = info.get("kind")
    if kind == "live":
        raise RuntimeError("这是直播间地址，程序不支持直播录制；请等下播后用视频链接或 BV 号")
    ref = str(info.get("ref") or "")
    if kind == "short":
        ref = _expand_short(ref)
        kind = "av" if ref.lower().startswith("av") else "bv"
    params: dict[str, Any]
    if kind == "av":
        params = {"aid": int(ref[2:] or 0)}
    else:
        params = {"bvid": ref}
    data = _http_json(VIEW_API, params, sign=False)
    if data.get("code") != 0:
        raise RuntimeError(f"取不到视频信息（{data.get('message') or data.get('code')}）")
    d = data.get("data") or {}
    page = int(info.get("page") or 1)
    pages = d.get("pages") or []
    title = str(d.get("title") or "")
    duration = int(d.get("duration") or 0)
    if page > 1:
        # FIX(L20): 原来的越界校验只在 `len(pages) > 1` 分支里。单P 视频写 ?p=3 时
        # 会静默按第 1 P 下载，界面却显示「第 3 P」。
        if not pages:
            raise RuntimeError(f"取不到分P 信息，无法定位第 {page} P")
        if page > len(pages):
            raise RuntimeError(f"该视频只有 {len(pages)} P，没有第 {page} P")
    if len(pages) > 1:
        pg = pages[page - 1] or {}
        title = str(pg.get("part") or title)
        duration = int(pg.get("duration") or duration)
    bvid = str(d.get("bvid") or (ref if kind == "bv" else ""))
    return {
        "bvid": bvid,
        "aid": int(d.get("aid") or 0),
        "title": title,
        "duration": duration,
        "author": str((d.get("owner") or {}).get("name") or ""),
        "play": int((d.get("stat") or {}).get("view") or 0),
        "pages": max(1, len(pages)),
        "page": page,
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
    }


def split_author_choices(line: str) -> list[tuple[str, str]]:
    """把「歌名+作者」按 + 从右往左切成若干种解释，最后一种是整行当歌名。

    `A+B+C` → [("A+B","C"), ("A","B+C"), ("A+B+C","")]
    """
    s = str(line).strip()
    parts = s.split("+")
    out: list[tuple[str, str]] = []
    for i in range(len(parts) - 1, 0, -1):
        left, right = parts[:i], parts[i:]
        # FIX(L18): 「a++b」「+a+b」这类含空段的行，切出来的歌名/作者会带着 `+`，
        # 拿去搜索必然无结果、还白耗一次限流配额。空段一律不产生切法，只留整行兜底。
        if not all(p.strip() for p in left) or not all(p.strip() for p in right):
            continue
        song = "+".join(left).strip()
        artist = "+".join(right).strip()
        if song and artist:
            out.append((song, artist))
    # FIX(L19): 「Song+」「+Song」这种只写了一个分隔符的行，原来整行当歌名（带着 +），
    # 搜索必然无结果。落单的分隔符直接剥掉。
    # 但 `C++` 这类歌名本身就带 `+`，只有「末尾/开头恰好一个 +」才算落单分隔符。
    clean = s
    if clean.endswith("+") and not clean.endswith("++"):
        clean = clean[:-1]
    if clean.startswith("+") and not clean.startswith("++"):
        clean = clean[1:]
    clean = clean.strip()
    out.append((clean or s, ""))
    return out


def choose_card(
    line: str,
    cfg: dict[str, Any],
    say=None,
    exclude: set[str] | None = None,
    ref_dur: float = 0.0,
    known_artists: list[str] | None = None,
    canonical_artist: str | None = None,
) -> tuple[PickResult, list[Card], str, str]:
    """按「歌名+作者」规则选片：先带作者搜，结果少且播放量低再退。

    返回 (选片结果, 候选列表, 实际歌名, 作者)。
    """
    say = say or (lambda *a, **k: None)
    min_play = int(cfg.get("min_play", 1))
    min_dur = int(cfg.get("min_dur", 50))
    max_dur = int(cfg.get("max_dur", 720))
    words = cfg.get("priority_words", DEFAULT_PRIORITY)

    first_ok = None
    last: tuple[PickResult, list[Card], str, str] = (
        PickResult(None, "搜索无结果"), [], str(line).strip(), "")
    for song, artist in split_author_choices(line):
        keyword = f"{song} {artist}".strip()
        say("search", f"搜索「{keyword}」")
        try:
            cards = search_both(keyword)
        except Exception as e:
            cards = []
            say("search", f"搜索失败：{e}")

        fit = [c for c in cards if title_matched(c.title, song)]
        top = max((c.play for c in fit), default=0)
        strong = len(fit) >= 3 and top >= 10000

        # v5 R6：有效音乐候选不足 2 条（我们不一样实测：40 条候选里音乐区被生活
        # 视频淹没）→ 补搜一次「歌名 原版」，仍找不到正片时 pick_card 会报失败。
        def _valid(cs: list[Card], _song: str = song) -> list[Card]:
            return [c for c in cs
                    if c.from_music and match_rank(c.title, _song) > 0
                    and (min_dur <= 0 or c.duration == 0 or c.duration >= min_dur)
                    and (max_dur <= 0 or c.duration == 0 or c.duration <= max_dur)]

        if len(_valid(cards)) < 2:
            try:
                extra = search_both(f"{song} 原版")
            except Exception:
                extra = []
            if extra:
                seen_bv = {c.bvid for c in cards}
                add = [c for c in extra if c.bvid and c.bvid not in seen_bv]
                if add:
                    cards = cards + add
                    say("search", f"候选不足，补搜「{song} 原版」+{len(add)} 条")

        pick = pick_card(cards, song, min_play=min_play, min_dur=min_dur,
                         max_dur=max_dur, priority_words=words, artist=artist,
                         exclude=exclude, ref_dur=ref_dur,
                         known_artists=known_artists,
                         canonical_artist=canonical_artist,
                         want=f"{line} {song} {artist}".strip())
        last = (pick, cards, song, artist)
        if pick.card and strong:
            return pick, cards, song, artist
        if pick.card and first_ok is None:
            first_ok = (pick, cards, song, artist)
        if not artist:
            break
    return first_ok or last


def artist_ok(song: str, artist: str) -> bool:
    """作者是否可用：不能为空，也不能就是歌名本身（网易云会把关键词当艺人返回）。"""
    a, s = normalize(artist), normalize(song)
    return bool(a) and a != s


# ===== L43 命中率基建：known_good / known_bad / 纠正事件 / metrics =====
# 三个文件互不读取；均本地生成，不进 §8.8 热更白名单。
# 评测 / 回放模式下读路径与写路径**双禁**（不是过滤条目）：见 _hit_read/_hit_write/_hit_append。
EVAL_MODE = bool(os.environ.get("SG_EVAL_MODE") or os.environ.get("SG_REPLAY"))
KNOWN_GOOD_FILE = APP_DIR / "known_good.json"
KNOWN_BAD_FILE = APP_DIR / "known_bad.json"
CORRECTIONS_FILE = APP_DIR / "corrections.jsonl"
METRICS_FILE = APP_DIR / "metrics.jsonl"
_KNOWN_TTL_DAYS = 180
_BAD_TTL_DAYS = 30
_HIT_LOCK = threading.RLock()


def _hit_read(path: Path, default):
    """读本地命中率文件；评测/回放模式直接返回默认值（读路径硬禁）。"""
    if EVAL_MODE:
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def _hit_write(path: Path, data) -> bool:
    """原子写本地命中率文件；评测/回放模式直接失败（写路径硬禁）。"""
    if EVAL_MODE:
        return False
    try:
        with _HIT_LOCK:
            _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1))
        return True
    except Exception as exc:
        _log_swallowed("hit_write", exc)
        return False


def _hit_append(path: Path, rec: dict) -> bool:
    """追加一行 JSONL；评测/回放模式直接失败（写路径硬禁）。"""
    if EVAL_MODE:
        return False
    try:
        line = json.dumps(rec, ensure_ascii=False)
        with _HIT_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception as exc:
        _log_swallowed("hit_append", exc)
        return False


def _song_only(text: str) -> str:
    """从「歌名+作者」里取歌名部分（只看最后一个 +）。"""
    try:
        return (split_author_choices(str(text or ""))[0][0] or "").strip()
    except Exception:
        return str(text or "").split("+")[0].strip()


def known_good_lookup(line: str) -> dict[str, Any] | None:
    """歌名(+作者) → 用户确认过的 bvid。命中即直接取流，不过搜索/选片/打分。"""
    if EVAL_MODE:
        return None
    try:
        song, hint = split_author_choices(str(line or ""))[0]
    except Exception:
        return None
    song = (song or "").strip()
    if not song:
        return None
    data = _hit_read(KNOWN_GOOD_FILE, {})
    now = time.time()
    same: list[dict[str, Any]] = []
    for rec in data.values():
        if not isinstance(rec, dict) or not rec.get("bvid"):
            continue
        if rec.get("missing"):
            continue
        if normalize(str(rec.get("song") or "")) != normalize(song):
            continue
        if hint and normalize(str(rec.get("artist") or "")) != normalize(hint):
            continue
        ts = float(rec.get("last_verified") or 0)
        # 过期未复核的记录不直接取流，退回正常选片（防止「一次误点永久生效」）
        if ts and now - ts > _KNOWN_TTL_DAYS * 86400:
            continue
        same.append(rec)
    if not same:
        return None
    if not hint:
        same.sort(key=lambda r: float(r.get("last_verified") or 0), reverse=True)
    return dict(same[0])


def known_good_put(song: str, artist: str, bvid: str, *, title: str = "",
                   author: str = "", source: str = "download") -> bool:
    """沉淀用户确认过的 bvid（下载成功 / 手动改选）。评测模式不写。"""
    song, bvid = (song or "").strip(), (bvid or "").strip()
    if not song or not bvid:
        return False
    data = _hit_read(KNOWN_GOOD_FILE, {})
    key = "%s||%s" % (normalize(song), normalize(artist or ""))
    old = data.get(key) if isinstance(data.get(key), dict) else {}
    data[key] = {
        "song": song, "artist": (artist or "").strip(), "bvid": bvid,
        "title": title or old.get("title", ""),
        "author": author or old.get("author", ""),
        "source": source, "last_verified": time.time(),
        "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hits": int(old.get("hits") or 0) + 1, "missing": False,
    }
    return _hit_write(KNOWN_GOOD_FILE, data)


def known_good_mark_missing(song: str, bvid: str) -> bool:
    """复核发现 bvid 失效：标 missing，下次自动退回正常选片。"""
    data = _hit_read(KNOWN_GOOD_FILE, {})
    hit = False
    for rec in data.values():
        if (isinstance(rec, dict) and rec.get("bvid") == bvid
                and normalize(str(rec.get("song") or "")) == normalize(song or "")):
            rec["missing"] = True
            rec["last_verified"] = 0.0
            hit = True
    return bool(_hit_write(KNOWN_GOOD_FILE, data) and hit)


def known_bad_put(song: str, bvid: str, reason: str = "") -> bool:
    """右键取消 / 用户否掉的 bvid：只降权，不硬剔；可撤销、30 天过期。"""
    song, bvid = (song or "").strip(), (bvid or "").strip()
    if not song or not bvid:
        return False
    data = _hit_read(KNOWN_BAD_FILE, {})
    data["%s||%s" % (normalize(song), bvid)] = {
        "song": song, "bvid": bvid, "reason": reason or "",
        "ts": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S"), "revoked": False,
    }
    return _hit_write(KNOWN_BAD_FILE, data)


def known_bad_active(song: str, bvid: str) -> bool:
    """该 bvid 当前是否仍在降权期内（未撤销、未过期）。"""
    if EVAL_MODE:
        return False
    data = _hit_read(KNOWN_BAD_FILE, {})
    rec = data.get("%s||%s" % (normalize(_song_only(song)), (bvid or "").strip()))
    if not isinstance(rec, dict) or rec.get("revoked"):
        return False
    ts = float(rec.get("ts") or 0)
    return bool(ts) and (time.time() - ts <= _BAD_TTL_DAYS * 86400)


def known_bad_undo(song: str, bvid: str) -> bool:
    """撤销一条 known_bad（误点可恢复）。"""
    data = _hit_read(KNOWN_BAD_FILE, {})
    key = "%s||%s" % (normalize(_song_only(song)), (bvid or "").strip())
    rec = data.get(key)
    if not isinstance(rec, dict):
        return False
    rec["revoked"] = True
    rec["revoked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ok = _hit_write(KNOWN_BAD_FILE, data)
    if ok:
        # L44：撤销成功记一次流水（undone=true）
        metrics_event("known_bad_undo", song=song or "", bvid=bvid or "", undone=True)
    return ok


def record_correction(song: str, old_bvid: str, new_bvid: str, *, tier: str = "",
                      fail_kind: str = "", kind: str = "repick") -> bool:
    """记录一次用户纠正动作（事件，不记作者结论，不碰 artists.json）。"""
    return _hit_append(CORRECTIONS_FILE, {
        "ts": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "song": song or "", "old_bvid": old_bvid or "", "new_bvid": new_bvid or "",
        "tier": tier or "", "fail_kind": fail_kind or "", "kind": kind,
    })


def metrics_event(kind: str, **fields: Any) -> bool:
    """阶段 5：按 fail_kind × 时间 × 歌单聚合的原始事件（只观测）。"""
    rec: dict[str, Any] = {"ts": time.time(),
                           "at": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind}
    rec.update({k: v for k, v in fields.items() if v not in (None, "")})
    return _hit_append(METRICS_FILE, rec)


def metrics_read(days: int = 30) -> list[dict[str, Any]]:
    if EVAL_MODE:
        return []
    try:
        cut = time.time() - days * 86400
        out: list[dict[str, Any]] = []
        with open(METRICS_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and float(rec.get("ts") or 0) >= cut:
                    out.append(rec)
        return out
    except Exception:
        return []


def metrics_aggregate(days: int = 30) -> list[dict[str, Any]]:
    """fail_kind（12 类）× 日期 × 歌单 的计数表。"""
    rows = metrics_read(days)
    agg: dict[tuple[str, str, str], int] = {}
    for r in rows:
        kind = str(r.get("fail_kind") or r.get("kind") or "?")
        day = str(r.get("at") or "")[:10]
        plist = str(r.get("playlist") or "")
        agg[(kind, day, plist)] = agg.get((kind, day, plist), 0) + 1
    return [{"fail_kind": k, "date": d, "playlist": p, "count": n}
            for (k, d, p), n in sorted(agg.items())]


def metrics_export_csv(path: Path, days: int = 30) -> int:
    rows = metrics_aggregate(days)
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("fail_kind,date,playlist,count\n")
            for r in rows:
                f.write("%s,%s,%s,%d\n" % (r["fail_kind"], r["date"],
                                           r["playlist"], r["count"]))
        return len(rows)
    except Exception as exc:
        _log_swallowed("metrics_export", exc)
        return 0


def export_need_author(rows: Any, path: Path) -> int:
    """阶段 4：把「无作者 / 待确认 / 失败」导出成待补 `+作者` 的 TSV。"""
    if EVAL_MODE:
        return 0
    n = 0
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("song\tstage\tartist\treason\tbvid\tsuggest\n")
            for r in rows or []:
                stage = str(r.get("stage") or "")
                if stage == "success":
                    continue
                song = str(r.get("song") or "").strip()
                if not song:
                    continue
                art = str(r.get("artist") or "")
                f.write("%s\t%s\t%s\t%s\t%s\t%s\n" % (
                    song, stage, art, str(r.get("reason") or ""),
                    str(r.get("bvid") or ""), (song + "+" + art) if art else song))
                n += 1
    except Exception as exc:
        _log_swallowed("export_need_author", exc)
        return 0
    return n


def author_cov_stats(days: int = 30) -> dict[str, Any]:
    """阶段 4 埋点：`+作者` 覆盖率、所写作者与命中条目作者的一致率（写法准确率代理）。"""
    trials = [r for r in metrics_read(days) if r.get("kind") == "trial"]
    with_hint = [r for r in trials if r.get("hint")]
    hit = [r for r in with_hint if r.get("bvid")]
    agree = [r for r in hit if r.get("artist_ok")]
    return {
        "trials": len(trials),
        "with_hint": len(with_hint),
        "coverage": round(len(with_hint) / len(trials), 4) if trials else 0.0,
        "hit": len(hit),
        "accuracy": round(len(agree) / len(hit), 4) if hit else 0.0,
    }


def verify_known_good(limit: int = 20) -> dict[str, int]:
    """用 view?bvid 复核 known_good；失效标 missing、静默退回正常选片。"""
    if EVAL_MODE:
        return {"checked": 0, "missing": 0}
    data = _hit_read(KNOWN_GOOD_FILE, {})
    checked = missing = 0
    for rec in list(data.values()):
        if checked >= limit:
            break
        if not isinstance(rec, dict) or not rec.get("bvid") or rec.get("missing"):
            continue
        try:
            info = _http_json("https://api.bilibili.com/x/web-interface/view",
                              {"bvid": rec["bvid"]}, timeout=8, retries=1)
        except Exception:
            continue                      # 网络失败不算失效
        checked += 1
        if not info or int(info.get("code", -1)) != 0:
            rec["missing"] = True
            rec["last_verified"] = 0.0
            missing += 1
        else:
            rec["last_verified"] = time.time()
            rec["verified_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if checked:
        _hit_write(KNOWN_GOOD_FILE, data)
    return {"checked": checked, "missing": missing}


def describe_song(line: str, cfg: dict[str, Any], say=None,
                  fallback: bool = False, cancel=None,
                  bvid_override: str | None = None,
                  canonical_artist: str | None = None) -> dict[str, Any]:
    """选片 + 查作者 + 算文件名：试搜与下载共用同一条路径。

    试搜与「开始下载」的规则、信息必须完全一致，只差最后是否真的下载，
    所以两者都从这里取结果，避免文案、作者、文件名出现分叉。
    bvid_override 非空时（用户从候选池点选确认），跳过搜索与选片，直接按指定 BV 取信息。
    """
    say = say or (lambda *a, **k: None)
    _check(cancel)
    fmt = str(cfg.get("output_format", "m4a")).lower()
    if fmt not in ("m4a", "mp3", "mp4"):
        fmt = "m4a"
    out_dir = resolve_output_dir(cfg)

    # CAND(4): 指定 BV 与直链共用同一条「解析 → 定作者 → 算文件名」路径：
    # 伪造一个 video_ref 结果，让下面的直链分支原样复用，不搜索、不选片。
    forced_bv = str(bvid_override or "").strip()
    # L43：用户确认过的 bvid 直接取流——不过 search_both / pick_card / _score
    _kg_hit: dict[str, Any] | None = None
    if not forced_bv:
        _kg_hit = known_good_lookup(line)
        if _kg_hit:
            forced_bv = str(_kg_hit.get("bvid") or "").strip()
    elif known_good_lookup(line) is not None:
        # trial_song 把 known_good 的 bvid 当 forced 传进来：识别来源，失效时才能回退
        _rec = known_good_lookup(line)
        if _rec and str(_rec.get("bvid") or "") == forced_bv:
            _kg_hit = _rec
    # 直链：行里出现 B 站链接 / BV 号 / av 号时，不搜索、不选片
    vref = video_ref(str(line))
    if forced_bv:
        # 手机端：bvid_override 允许带 ?p=N（多P 视频里用户选中的那一集）
        _pg = 1
        _pm = re.search(r"[?&]p=(\d+)", forced_bv)
        if _pm:
            _pg = max(1, int(_pm.group(1)))
            forced_bv = re.split(r"[?&]", forced_bv)[0].strip()
        vref = {"kind": "bv", "ref": forced_bv, "page": _pg,
                "span": (0, 0), "raw": forced_bv}
    direct: dict[str, Any] | None = None
    if forced_bv:
        # CAND(4): 指定 BV 时行里没有链接可剥离，整行都按「歌名+作者」解释
        song, artist_hint = split_author_choices(line)[0]
    elif vref is not None:
        name_part = strip_video_ref(str(line), vref)
        if name_part:
            song, artist_hint = split_author_choices(name_part)[0]
        else:
            song, artist_hint = "", ""
    else:
        # 「歌名+作者」：按最后一个 + 切出歌名；作者只用于搜索精度与命名
        song, artist_hint = split_author_choices(line)[0]
    # CAND(4): 指定 BV 在下载校验上按**非直链**处理（走 audio_ok/base_dur 那套完整性校验），
    # 所以 mp4 与普通歌名一样回落 m4a（mp4 只对真正的直链生效）。
    if (vref is None or forced_bv) and fmt == "mp4":
        fmt = "m4a"          # mp4 只对直链生效，普通歌名仍然下音频

    artists = load_artists()
    artist = artist_hint or artists.get(song, "").strip()
    # 旧版本会把「查不到」写成 Unknown 存档，这里当没查到，重新查一次
    if artist.strip().lower() in ("unknown", "未知", "-"):
        artist = ""
    source = "hint" if artist_hint else ("file" if artist else "")
    if artist and not artist_ok(song, artist):
        artist, source = "", ""
    # 规格§7 作者锚点：只认用户用「+」写明的作者，且必须通过 artist_ok 校验。
    # 用户没写作者名时不加锚点——曲库五源推出来的「原唱」噪声很大（会把翻唱者
    # 当原唱返回），拿它当锚点会把本来正确的视频换走：固定快照 100 首实测
    # bvid 79%→73%（硬锚点）/ 76%（软锚点），已否决该来源。
    if canonical_artist is None and source == "hint" and artist:
        canonical_artist = artist

    res: dict[str, Any] = {
        "card": None, "pick": None, "song": song, "artist": artist,
        "artist_source": source, "pending": False, "fname": "", "reason": "",
        "fmt": fmt, "existing": None, "fail": None,
        # CAND(4): 指定 BV 走直链的解析分支，但 direct=False 让下载校验走非直链那套
        # （audio_ok + base_dur 比对）；forced 标记便于界面/日志区分来源。
        "direct": vref is not None and not forced_bv,
        "forced": bool(forced_bv),
        "video": None, "need_name": False, "known_good": bool(_kg_hit),
        # CAND(5): 候选池与头部两名的分数先占位，保证任何 return 都有这三个键
        "candidates": [], "top_score": 0.0, "second_score": 0.0,
    }
    # CAND(4): 直链 / 指定 BV 的提示文案分开写，普通直链文案保持原样
    _label = "指定视频" if forced_bv else "直链"

    # FIX(M30): 直链原来是「先 resolve 再查本地」。源视频被删/私享/失效时，明明是已经
    # 下过的歌也会被判成「取不到视频信息」。本地已有成品就直接算已存在。
    if vref is not None and song.strip():
        early = find_existing(out_dir, song.strip(), artist=artist_hint)
        if early is not None:
            want = out_fname(song.strip(), fmt)
            res["existing"] = early
            if want and early.name != want:
                res["reason"] = f"已存在（当前 {early.name}，正式下载会改名为 {want}）"
                res["fname"] = want
            else:
                res["reason"] = "已存在"
                res["fname"] = early.name
            return res

    if vref is not None:
        _check(cancel)
        try:
            direct = resolve_video(vref)
        except Cancelled:
            raise
        except Exception as e:
            if _kg_hit is not None:
                # L43：known_good 指向的 bvid 已失效 → 标记 missing 并静默退回正常选片
                known_good_mark_missing(str(_kg_hit.get("song") or song), forced_bv)
                return describe_song(line, cfg, say=say, fallback=fallback,
                                     cancel=cancel, bvid_override=None,
                                     canonical_artist=canonical_artist)
            res["fail"] = SongResult(song or str(line), "fail", str(e),
                                     fail_kind=_classify_download_error(e),
                                     fail_detail=f"{type(e).__name__}: {e}")
            return res
        res["video"] = direct
        res["card"] = Card(bvid=direct["bvid"], title=direct["title"],
                           play=direct["play"], duration=direct["duration"],
                           author=direct["author"])
        if not song.strip():
            # 只有链接没有歌名：不猜标题，交给界面弹窗让用户自己填
            res["need_name"] = True
            res["reason"] = f"{_label}《{direct['title'][:40]}》，待命名"
            return res
        if not direct["bvid"]:
            res["fail"] = SongResult(song, "fail", "无法确定 BV 号",
                                     fail_kind="no_bvid", fail_detail="解析视频页后没有拿到 bvid")
            return res
        from_title = artist_from_title(direct["title"])
        got = artist_hint or from_title or direct["author"]
        if got and not artist_ok(song, got):
            got = ""
        if got:
            artist = got
            # FIX(M34): 最后这个兜底拿的是 UP 主名，原来也标成 "title"，于是
            # process_song 会把它写进 artists.json，错误作者被长期复用。
            source = ("hint" if artist_hint
                      else ("title" if from_title and got == from_title else "up"))
        else:
            artist, source, res["pending"] = "Unknown", "unknown", True
        song = song.strip()
        res.update({
            "song": song, "artist": artist, "artist_source": source,
            "fname": out_fname(song, fmt),
            "reason": f"{_label}《{direct['title'][:40]}》",
        })
        if _kg_hit is not None:
            # L44：known_good 直取成功记一次流水（只观测，不影响选片）
            metrics_event("known_good_hit",
                          song=str(_kg_hit.get("song") or song),
                          bvid=str(res["card"].bvid))

    # 已有成品：两边都先判「已存在」，并给出会改成的目标名
    existing = find_existing(out_dir, song, artist=artist)
    if existing:
        want = out_fname(song, fmt)
        if want and existing.name != want:
            res["reason"] = f"已存在（当前 {existing.name}，正式下载会改名为 {want}）"
            res["fname"] = want
        else:
            res["reason"] = "已存在"
            res["fname"] = existing.name
        res["existing"] = existing
        return res

    # 直链到此为止：不搜索、不选片、不联网查作者
    if vref is not None:
        return res

    # 选片 + 作者查询互相校验：若视频时长和音乐库里所有同名曲都对不上，
    # 说明选错了视频（片段/翻唱/别的歌），排除后重选，最多 3 轮。
    # C1/C2/C6：先把库内同名曲的时长分布查出来，作为「这首歌应该多长」的基准。
    # 查询失败/为空时 ref_dur=0，C1/C3 自动不启用，选片回退到纯加权逻辑（C5）。
    mus_all: list[tuple[str, str, int]] = []
    try:
        mus_all = lookup_artist_candidates(song, 0)
    except Exception:
        mus_all = []
    ref_dur, _mode_n = _duration_mode(mus_all)
    # v5 R4：片段判定基准取众数簇内时长降序第 2 名（避开 DJ remix 这种最长成员）
    clip_base = _clip_base(mus_all)
    if ref_dur:
        say("artist", f"同名曲主流时长 {ref_dur:g}s（{_mode_n} 条），片段基准 {clip_base:g}s")
    # 曲库里的作者名，供 pick_card 判「…唱《歌名》」里的演唱者在不在库里
    known_names = [a for a, _s, _d in mus_all if a]

    excluded: set[str] = set()
    # 只有 ≥2 条同长才算可信参照；单条孤证不用于选片降权（C5 精神：宁可漏判）。
    pick, cards, picked_song, picked_artist = choose_card(
        line, cfg, say, ref_dur=clip_base if _mode_n >= 2 else 0.0,
        known_artists=known_names, canonical_artist=canonical_artist)
    _check(cancel)
    song = picked_song or song
    if picked_artist:
        artist = picked_artist
        res["artist_source"] = "hint"

    if not artist and pick.card:
        for _round in range(3):
            say("artist", f"联网查演唱者「{song}」…")
            if _round > 0:
                try:
                    mus_all = lookup_artist_candidates(song, 0)
                except Exception:
                    mus_all = []
                ref_dur, _mode_n = _duration_mode(mus_all)
                clip_base = _clip_base(mus_all)
                known_names = [a for a, _s, _d in mus_all if a]
            # C2：只在「众数簇」（这首歌的真实时长）里找时长对得上的版本。原来拿全部
            # 同名曲按 15s 容差匹配，114s 的切片会撞上同样 114s 的小众翻唱，把作者错
            # 标成翻唱者。
            # C5：众数簇为空说明库里没有这首歌的时长参照，C1/C2 不启用。
            mode = _mode_cluster(mus_all, ref_dur)
            vdur = float(pick.card.duration or 0)
            # 0) 片段判定优先（C1）：视频明显比这首歌短时，先换候选再谈作者。
            #    综艺剪辑、副歌切片常常在标题里写着原唱歌手名，若先按标题取作者，
            #    就会把一个 170s 的片段当成「大壮《我们不一样》」的正片留下。
            #    比库长多半是加了片头/4K 修复版的正片，视频本身是对的，不换。
            #    簇里只有 1 条时参照不可信，不做片段判定，只拿它兜底作者。
            is_clip = (vdur > 0 and clip_base > 0 and vdur < clip_base * _CLIP_RATIO
                       and _mode_n >= 2)
            if is_clip and mode and _round < 2:
                say("artist", "候选视频比原曲短很多（疑似片段），换下一个候选…")
                excluded.add(pick.card.bvid)
                _prev_pick = pick
                pick, cards, picked_song, picked_artist = choose_card(
                    line, cfg, say, exclude=excluded, ref_dur=clip_base,
                    known_artists=known_names, canonical_artist=canonical_artist)
                _check(cancel)
                if not pick.card:
                    # 换无可换：留用原片段（宁可下个片段，也别整首不下），作者按众数兜底
                    pick = _prev_pick
                    hit = _mode_artist(mode, song)
                    if hit:
                        artist, source = hit
                        say("artist", f"按同名曲主流时长判定：{artist}（{source}）")
                    break
                song = picked_song or song
                continue
            # 1) 标题里直接写着作者名（「【4K60FPS】周杰伦《七里香》」「大声听柳爽
            #    《漠河舞厅》」）时以它为准：这是 UP 主自己标的，比统计众数可靠——
            #    库里翻唱版本常常比原唱还多，众数会把翻唱者当成原唱。
            # FIX(缺陷1): by_title 用 _name_in_title 做词边界判定，替换原来
            # `normalize(a) in tnorm` 裸子串（「泰坦尼克号」的「尼克」被误当作者）。
            # FIX(m11937 项2): 厂牌/群星/合唱/影视原声（音阙诗听、supercell、众星）
            # 不是个人演唱者，不参与 by_title 与作者加分。
            by_title = [(a, s, d) for a, s, d in mus_all
                        if a and artist_ok(song, a)
                        and not _NOT_ARTIST_RX.search(a)
                        and _name_in_title(pick.card.title, a)]
            if by_title:
                by_title.sort(
                    key=lambda x: (len(normalize(x[0])),
                                   -abs(float(x[2] or 0) - vdur)),
                    reverse=True,
                )
                raw = str(by_title[0][0]).strip(" -—–|/、·:：")
                # FIX(翻唱换候选): 标题里写的作者若其曲库时长偏离「众数簇」（这首歌
                # 主流版本的时长），说明标题标的是翻唱者（梦醒时分→伍佰 226s vs 原唱
                # 陈淑桦 247s；白狐→小阿枫 238s vs 原唱陈瑞 306s）。此时**不能**直接改
                # 作者——那会把「伍佰翻唱视频」标成陈淑桦，制造视频-作者错位（修1 教训）。
                # 正确做法是排除这个翻唱候选、换让原唱版（时长落在众数簇内）胜出，
                # 视频与作者一起换，保持自洽。4K 修复/演唱会版的原唱标题作者时长本就
                # 在众数簇内，不会误触发。
                _tdur = float(by_title[0][2] or 0)
                # FIX(m11937 项4): _author_conflict 判据由「∉ 众数簇作者集合」改为
                # 「标题作者 ≠ canonical_artist 或 ∈ 厂牌/群星名单」。众数簇作者集合
                # 在古风样本上被翻唱者占据，不可作判据（离人愁 227s 簇全是曲肖冰、
                # 说散就散 230/231s 原唱翻唱同簇），最多作弱参考，不作触发。
                _title_name = normalize(raw)
                _author_conflict = False
                if canonical_artist and normalize(canonical_artist) != _title_name:
                    _author_conflict = True
                elif _NOT_ARTIST_RX.search(raw):
                    _author_conflict = True
                # 时长偏离众数簇仅作弱参考：只有 canonical_artist 未提供时才沿用
                if canonical_artist:
                    _dur_conflict = False
                else:
                    _dur_conflict = bool(mode and _tdur > 0 and ref_dur > 0
                                         and abs(_tdur - ref_dur) > _ARTIST_CLUSTER_TOL)
                if (_dur_conflict or _author_conflict) and _round < 2:
                    say("artist", f"标题作者 {raw} 与原唱不符（疑似翻唱/厂牌），换候选…")
                    excluded.add(pick.card.bvid)
                    _prev_pick = pick
                    pick, cards, picked_song, picked_artist = choose_card(
                        line, cfg, say, exclude=excluded, ref_dur=clip_base,
                        known_artists=known_names, canonical_artist=canonical_artist)
                    _check(cancel)
                    if not pick.card:
                        # 换无可换：留用当前翻唱版，作者跟随视频（自洽，宁下翻唱不落空）
                        pick = _prev_pick
                        if raw and artist_ok(song, raw):
                            artist, source = raw, "title"
                            say("artist", f"标题里写着：{artist}")
                        break
                    song = picked_song or song
                    continue
                if raw and artist_ok(song, raw):
                    artist, source = raw, "title"
                    say("artist", f"标题里写着：{artist}")
                    break
            # 2) 时长几乎相等（±5s）的库候选。「15s 容差」会把 4s 之差的翻唱
            #    也当成原唱（「搁浅」266s 的翻唱盖过 263s 的原曲），这里收紧。
            near = [x for x in mode
                    if vdur > 0 and abs(float(x[2] or 0) - vdur) <= 5]
            hit = next(((a, s) for a, s, _d in near if artist_ok(song, a)), None)
            if hit:
                artist, source = hit
                say("artist", f"查到：{artist}（{source}）")
                break
            # 兜底取众数簇作者：多源（QQ/网易云/Apple）里出现次数最多的那个，
            # 避免簇里恰好排第一的翻唱者被当成原唱（如「七里香」被标成翻唱 UP 主）。
            hit = _mode_artist(mode, song)
            if hit:
                artist, source = hit
                say("artist", f"按同名曲主流时长判定：{artist}（{source}）")
            break
        if not artist and pick.card:
            from_title = artist_from_title(pick.card.title)
            if from_title and artist_ok(song, from_title):
                artist, source = from_title, "title"
                say("artist", f"从视频标题取：{artist}")

    if fallback and not pick.card:
        say("search", f"接口无合适结果（{pick.reason}），改用 yt-dlp 兜底搜索…")
        try:
            alt = search_ytdlp(song)
        except Exception:
            alt = []
        if alt:
            alt_pick = pick_card(
                alt, song,
                min_play=0,
                min_dur=int(cfg.get("min_dur", 50)),
                max_dur=int(cfg.get("max_dur", 720)),
                priority_words=cfg.get("priority_words", DEFAULT_PRIORITY),
                ignore_play=True,
                artist=artist,
            )
            if alt_pick.card:
                alt_pick.reason = f"兜底命中（{alt_pick.reason}）"
                pick = alt_pick
                say("search", f"兜底选中 {alt_pick.card.title[:30]}")

    res["song"], res["artist"] = song, artist
    # CAND(6): 失败/成功都带上候选池：pick.pool 已由 pick_card 按同一排序键排好，
    # 界面失败时也能让用户从候选里手点一个 BV 再下载。
    # 排序键是 pick_card 内的闭包（依赖 kinds/artist/known_artists），外部复现不了，
    # 所以按任务约定用播放量兜底当分数（真实加权分见 pick_card 内的 _score）。
    res["pick"] = pick
    _pl = list(getattr(pick, "pool", None) or [])
    _pick_sc = dict(getattr(pick, "scores", None) or {})
    res["candidates"] = [
        {"bvid": c.bvid, "title": c.title, "play": c.play,
         "duration": c.duration, "author": c.author,
         "score": round(float(_pick_sc.get(c.bvid, 0.0)), 4)}
        for c in _pl
    ]
    # v8：断层判定改用加权分。基数已是 1，用播放量冒充分数会让「热门片段」自动通过 certain。
    res["top_score"] = float(_pick_sc.get(_pl[0].bvid, 0.0)) if len(_pl) >= 1 else 0.0
    res["second_score"] = float(_pick_sc.get(_pl[1].bvid, 0.0)) if len(_pl) >= 2 else 0.0
    if not pick.card:
        reason = f"搜索无结果（{pick.reason}）" if not cards else pick.reason
        res["fail"] = SongResult(song, "fail", reason,
                                 fail_kind=_classify_search_reason(reason),
                                 fail_detail=str(pick.reason or reason))
        return res

    pending = False
    if not artist:
        # 用户要求：多源都查不到时统一标 Unknown，照常下载
        artist = "Unknown"
        pending = True
        source = "unknown"
        res["artist_source"] = "unknown"
        say("artist", "未查到演唱者，文件名用 Unknown（歌单里写「歌名+作者」可覆盖）")

    res.update({
        "card": pick.card, "pick": pick, "artist": artist, "pending": pending,
        "artist_source": source,
        "fname": out_fname(song, fmt),
        "reason": f"{pick.reason}｜{pick.card.title[:40]}（{pick.card.play} 播放）",
    })
    if pending:
        res["reason"] += "；演唱者未查到（Unknown）"
    return res


# FIX(L35/B2): 并行下载（GUI 默认 2 路）时只靠 exists() 会让两路拿到同一个目标名——
# 实测 8 线程抢同一个文件名，5 轮全部返回同一个名字，后完成的直接覆盖先完成的。
# 这里用一把进程内锁 + 名字预留表，把「算名字」到「落盘结束」这段窗口占住。
_NAME_LOCK = threading.Lock()
_NAME_RESERVED: set[str] = set()


def unique_dst(out_dir: Path, fname: str) -> tuple[Path, str]:
    """L33：同名不同曲撞名时依次试「歌名 (2).m4a」，绝不覆盖已有文件。

    L35：进程内预留目标名，并行下载不会选中同一个文件名。用完必须调 release_dst。
    """
    stem, ext = Path(fname).stem, Path(fname).suffix
    with _NAME_LOCK:
        for i in range(1, 100):
            name = fname if i == 1 else f"{stem} ({i}){ext}"
            if name in _NAME_RESERVED:
                continue
            if (out_dir / name).exists():
                continue
            _NAME_RESERVED.add(name)
            return out_dir / name, name
    # 100 个都占满：退回原名（极端情况，至少不让函数崩）
    return out_dir / fname, fname


def release_dst(fname: str) -> None:
    """FIX(L35/B2)：下载收尾释放 unique_dst 预留的名字。"""
    if not fname:
        return
    with _NAME_LOCK:
        _NAME_RESERVED.discard(fname)


def out_fname(song: str, fmt: str) -> str:
    """L33（用户 2026-11-11 定）：文件名只用歌名，作者只写进音频标签。

    同名不同曲靠 `find_existing` 的「歌名+作者」比对区分；真要落到同一个文件名时
    由下载收尾处的防覆盖逻辑加 ` (2)` 序号，绝不静默覆盖已有文件。
    """
    s = sanitize_filename(str(song or "").strip()) if str(song or "").strip() else ""
    return (s or "Unknown") + "." + fmt


def _trial_fname(song: str, artist: str, fmt: str) -> str:
    """试搜阶段先给个建议文件名；L33 起只写歌名，作者进标签。"""
    s = str(song or "").strip()
    if not s:
        return ""
    return out_fname(s, fmt)


def _trial_search(keyword: str, say) -> list[Card]:
    say("trial", f"搜索「{keyword}」…")
    try:
        return search_both(keyword)
    except Exception as e:
        say("trial", f"搜索失败：{e}")
        return []


def _trial_rank(cards: list[Card], song: str, artist: str, cfg: dict[str, Any],
                ref_dur: float, known: list[str],
                canonical: str | None = None) -> PickResult:
    """按选片规则跑一遍完整过滤 + 加权排序，池子取全量（算「过滤后条数」用）。"""
    return pick_card(
        cards, song,
        min_play=int(cfg.get("min_play", 1) or 1),
        min_dur=int(cfg.get("min_dur", 50) or 0),
        max_dur=int(cfg.get("max_dur", 720) or 0),
        priority_words=cfg.get("priority_words", DEFAULT_PRIORITY),
        artist=artist, ref_dur=ref_dur, known_artists=known,
        canonical_artist=canonical, pool_n=200,
        want=f"{song} {artist}".strip(),
    )


def _trial_cands(pick: PickResult, n: int = 3) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in (pick.pool or [])[:n]:
        out.append({"bvid": c.bvid, "title": c.title, "play": c.play,
                    "duration": c.duration, "author": c.author,
                    "score": pick.scores.get(c.bvid, 0.0)})
    return out


def trial_song(line: str, cfg: dict[str, Any] | None = None, say=None,
               cancel=None, bvid_override: str | None = None) -> dict[str, Any]:
    """L14「试搜」：查作者 → 带作者搜 → 分档。不下载、不写 artists.json。

    stage: success / pending / failed
    match_kind: whitelist / exact / candidate / direct / none
    branch: 1（带作者结果够）/ 2（回退裸歌名并集）/ 0（直链）
    分支判定口径（用户 L14 定）：计数点 = 最终真正参与排序的池子（软过滤回退后为准）；
    分支1 需 >5 条且 ≥2 条播放量 >1w，否则走分支2 并集。
    """
    cfg = cfg or {}
    say = say or (lambda *a, **k: None)
    res: dict[str, Any] = {
        "line": line, "song": "", "artist": "", "artist_source": "",
        "keyword": "", "branch": 0, "stage": "failed", "match_kind": "none",
        "bvid": "", "title": "", "author": "", "duration": 0, "score": 0.0,
        "candidates": [], "pool_count": 0, "over_1w": 0,
        "reason": "", "need_name": False, "fname": "",
    }
    raw = str(line or "").strip()
    if not raw:
        res["reason"] = "空行"
        return res

    fmt = str(cfg.get("output_format", "m4a") or "m4a").lower()
    if fmt not in ("m4a", "mp3", "mp4"):
        fmt = "m4a"
    min_dur = int(cfg.get("min_dur", 50) or 0)
    max_dur = int(cfg.get("max_dur", 720) or 0)

    # 直链 / 指定 BV：不搜索，直接复用 describe_song 的解析路径
    forced = str(bvid_override or "").strip()
    if not forced:
        _kg_t = known_good_lookup(raw)
        if _kg_t:
            forced = str(_kg_t.get("bvid") or "").strip()
    if forced or video_ref(raw) is not None:
        say("trial", "识别为直链/指定 BV，跳过搜索…")
        d = describe_song(raw, cfg, say=say, cancel=cancel,
                          bvid_override=forced or None)
        card = d.get("card")
        if card is None:
            # L43：known_good 失效回退后池子为空等情况，别把失败写成 success
            _fail = d.get("fail")
            res.update({
                "song": d.get("song") or raw,
                "stage": "failed",
                "reason": (_fail.reason if _fail is not None
                           else (d.get("reason") or "未取到候选")),
            })
            return res
        art = str(d.get("artist") or "")
        res.update({
            "song": d.get("song") or raw, "artist": art,
            "artist_source": d.get("artist_source") or "",
            "branch": 0, "stage": "success", "match_kind": "direct",
            "bvid": (card.bvid if card else ""),
            "title": (card.title if card else ""),
            "author": (card.author if card else ""),
            "duration": (card.duration if card else 0),
            "reason": d.get("reason") or "",
            "fname": d.get("fname") or "",
            "need_name": bool(d.get("need_name")) or not art
            or art.strip().lower() == "unknown",
        })
        if not res["fname"]:
            res["fname"] = _trial_fname(res["song"], res["artist"], fmt)
        return res

    choices = split_author_choices(raw)
    song0, hint0 = choices[0]
    base_song = song0 or raw
    res["song"] = base_song

    # 曲库五源一次拿全：既按优先级取作者，也给选片当 known_artists 与时长基准
    say("artist", f"查同名曲与作者「{base_song}」…")
    try:
        mus_all = lookup_artist_candidates(base_song, 0)
    except Exception as e:
        mus_all = []
        say("artist", f"查询失败：{e}")

    artist, source = "", ""
    if hint0 and artist_ok(base_song, hint0):
        artist, source = hint0, "hint"
        say("artist", f"用你写的作者：{artist}")
    if not artist:
        artist, source = artist_from_mus(mus_all, base_song)
        if artist:
            say("artist", f"按优先级取到作者：{artist}（{source}）")
        else:
            say("artist", "没查到作者，只按歌名搜；命名时再补")
    res["artist"] = artist
    res["artist_source"] = source
    res["need_name"] = not artist

    ref_dur, _mode_n = _duration_mode(mus_all)
    clip_base = _clip_base(mus_all)
    known = [a for a, _s, _d in mus_all if a]

    cards_all: list[Card] = []
    seen_bv: set[str] = set()

    def _absorb(cs: list[Card]) -> None:
        for c in cs:
            if c.bvid and c.bvid not in seen_bv:
                seen_bv.add(c.bvid)
                cards_all.append(c)

    # 有作者：从右往左逐个 + 切法试，池子 >5 才算数（用户定的回退规则）
    if artist:
        if hint0 and artist == hint0:
            tries = [(s_, a_) for s_, a_ in choices if a_]
        else:
            tries = [(base_song, artist)]
    else:
        tries = []

    chosen = None
    best = None          # 池子最大的那次尝试，分支2 用它决定「裸歌名」取哪个
    for s_, a_ in tries:
        _check(cancel)
        kw = f"{s_} {a_}".strip()
        cards = _trial_search(kw, say)
        _absorb(cards)
        pr = _trial_rank(cards, s_, a_, cfg,
                         clip_base if _mode_n >= 2 else 0.0, known, None)
        cnt = len(pr.pool)
        over = sum(1 for c in pr.pool if c.play > 10000)
        say("trial", f"「{kw}」过滤后 {cnt} 条，其中播放量 >1w 的 {over} 条")
        if best is None or cnt > best["count"]:
            best = {"song": s_, "artist": a_, "keyword": kw,
                    "pick": pr, "count": cnt, "over": over}
        if cnt > 5:
            chosen = best
            break

    branch = 1 if (chosen is not None and chosen["over"] >= 2) else 2
    if branch == 1:
        pick = chosen["pick"]
        res["song"] = chosen["song"]
        res["keyword"] = chosen["keyword"]
    else:
        bare_song = best["song"] if best else base_song
        say("trial", f"带作者结果不足，回退按「{bare_song}」搜，并与前面结果取并集…")
        _absorb(_trial_search(bare_song, say))
        pick = _trial_rank(cards_all, bare_song, artist, cfg,
                           clip_base if _mode_n >= 2 else 0.0, known, None)
        res["song"] = bare_song
        res["keyword"] = bare_song
    res["branch"] = branch

    # 白名单最优先：UP 主名完全一致 + 标题完整带歌名（简介带不算）+ 过时长窗口
    # L15：标题命中合唱/翻唱/改编等词条的视频不享受白名单优先，回到正常筛选
    nbase = normalize(res["song"])
    wl: list[Card] = []
    wl_hit = 0
    for c in cards_all:
        if not is_whitelist_up(c.author):
            continue
        if not nbase or nbase not in normalize(c.title):
            continue
        if c.duration and min_dur > 0 and c.duration < min_dur:
            continue
        if c.duration and max_dur > 0 and c.duration > max_dur:
            continue
        if wl_blocked(c.title):
            wl_hit += 1
            continue
        wl.append(c)
    if wl_hit:
        say("trial", f"白名单 UP 有 {wl_hit} 条视频标题含合唱/翻唱/改编等词，"
                     f"已取消其优先，改走正常筛选。")
    if wl:
        wl.sort(key=lambda c: c.play, reverse=True)
        c = wl[0]
        res.update({"stage": "success", "match_kind": "whitelist",
                    "bvid": c.bvid, "title": c.title, "author": c.author,
                    "duration": c.duration,
                    "reason": f"白名单 UP「{c.author}」命中（{c.play} 播放）"})
        res["fname"] = _trial_fname(res["song"], res["artist"], fmt)
        return res

    res["candidates"] = _trial_cands(pick, 3)
    res["pool_count"] = len(pick.pool)
    res["over_1w"] = sum(1 for c in pick.pool if c.play > 10000)
    if pick.card:
        card = pick.card
        res.update({"bvid": card.bvid, "title": card.title,
                    "author": card.author, "duration": card.duration,
                    "score": pick.scores.get(card.bvid, 0.0)})

    if branch == 1 and pick.card and artist:
        _s = pick.scores
        s0 = _s.get(pick.pool[0].bvid, 0.0) if pick.pool else 0.0
        s1 = _s.get(pick.pool[1].bvid, 0.0) if len(pick.pool) > 1 else 0.0
        tie = len(pick.pool) > 1 and abs(s0 - s1) < 1e-6
        title_full = bool(nbase) and nbase in normalize(pick.card.title)
        artist_in = bool(normalize(artist)) and (
            _name_in_title(pick.card.title, artist)
            or normalize(artist) in normalize(pick.card.title))
        if title_full and artist_in and not tie:
            res.update({"stage": "success", "match_kind": "exact",
                        "reason": "完全命中（标题含完整歌名+作者，且无并列第一）"})

    if res["stage"] != "success":
        if pick.card:
            res["stage"] = "pending"
            res["match_kind"] = "candidate"
            base_reason = pick.reason or "需要人工确认"
            res["reason"] = (f"分支2·带作者结果不足 → {base_reason}"
                             if branch == 2 else base_reason)
        elif getattr(pick, "stage", "ok") == "pending" and pick.candidates:
            # CAND(6): 过滤链把正片剔光（全翻唱/全是二创壳/只剩切片），但池子非空：
            # 不报 0 结果，改成「待确认」并把前三给用户点选，默认用第一条。
            res["stage"] = "pending"
            res["match_kind"] = "candidate"
            res["reason"] = pick.reason or "待确认：候选池全是翻唱/器乐改编"
            c0 = pick.candidates[0]
            res.update({"bvid": c0.bvid, "title": c0.title, "author": c0.author,
                        "duration": c0.duration,
                        "score": pick.scores.get(c0.bvid, 0.0)})
        else:
            res["stage"] = "failed"
            res["reason"] = pick.reason or "无搜索结果"
    if not res["fname"]:
        res["fname"] = _trial_fname(res["song"], res["artist"], fmt)
    return res


def preview_song(line: str, cfg: dict[str, Any], progress=None,
                 fallback: bool = False, cancel=None,
                 bvid_override: str | None = None) -> SongResult:
    """试搜：规则与「开始下载」完全一致，只是不下载文件。"""
    say = progress or (lambda *a, **k: None)
    # CAND(8): 把用户点选的 BV 透传给 describe_song，跳过搜索与选片
    d = describe_song(line, cfg, say, fallback=fallback, cancel=cancel,
                      bvid_override=bvid_override)
    # CAND(9): 各个 return 点都带上候选池，界面据此展示「其它候选」
    cands = d.get("candidates") or []
    if d.get("need_name") and d.get("video"):
        v = d["video"]
        _lab = "指定视频" if d.get("forced") else "直链"
        return SongResult(d["song"] or f"（{_lab}）", "preview",
                          f"{_lab}《{v['title'][:40]}》，开始下载时会让你填歌名",
                          "", v["bvid"], video_title=v["title"],
                          candidates=cands)
    if d["existing"] is not None:
        return SongResult(d["song"], "skip", d["reason"], d["fname"],
                          artist=d["artist"], candidates=cands)
    if d["fail"] is not None:
        fail = d["fail"]
        fail.candidates = cands
        return fail
    card = d["card"]
    return SongResult(d["song"], "preview", d["reason"], d["fname"], card.bvid,
                      artist=d["artist"], pending=d["pending"],
                      video_title=card.title, candidates=cands)


def process_song(
    song_line: str,
    cfg: dict[str, Any],
    workdir: Path,
    progress=None,
    fallback: bool = False,
    cancel=None,
    bvid_override: str | None = None,
    on_progress=None,
    no_artist: bool = False,
) -> SongResult:
    """处理单首歌。曲师查不到时也下载，用 UP 主名占位并标记 pending。

    no_artist=True 是手机端「未命名」：用户明确要求不要自动查作者，
    标签写 Unknown，也不写 artists.json。
    """
    say = progress or (lambda *a, **k: None)
    out_dir = resolve_output_dir(cfg)
    try:
        # CAND(10): 用户点选的 BV 透传给 describe_song（跳过搜索与选片）
        d = describe_song(song_line, cfg, say, fallback=fallback, cancel=cancel,
                          bvid_override=bvid_override)
    except Cancelled:
        return SongResult(song_line, "stopped", "用户停止")
    # 手机端：记忆里（confirm.json）的音源可能已被删除/私享，取不到时别直接判失败，
    # 退回正常「搜索 + 选片」，把决定权交回用户。
    if bvid_override and d.get("fail") is not None:
        say("pick", "已确认的音源取不到了，重新搜索")
        try:
            d = describe_song(song_line, cfg, say, fallback=fallback, cancel=cancel,
                              bvid_override=None)
        except Cancelled:
            return SongResult(song_line, "stopped", "用户停止")
    song, artist, pending = d["song"], d["artist"], d["pending"]
    if no_artist:
        artist, pending = "Unknown", True
    fmt = d["fmt"]

    # FIX(L20): 只把「用户给的作者」写回 artists.json。
    # 网络源（qq/netease/apple_*）自动查到的作者一律不落盘——查错一次就会永久错且不自愈
    # （稻香 = Lucky小爱）。file/up 也不写；用户手动改的作者由 GUI 走 source="manual" 写入。
    if (artist and artist != "Unknown" and not no_artist
            and d["artist_source"] in ("hint", "user", "manual")):
        # FIX(M31): 带上视频时长再存，同名不同曲才能另开一条记录而不是串用作者。
        _card = d.get("card")
        save_artist(song, artist, int((_card.duration if _card else 0) or 0))

    # 已有成品：作者变了就顺手改名，避免补表后留下旧名字
    existing = d["existing"]
    if existing is not None:
        reason = "已存在"
        if artist and existing.suffix.lower() == "." + fmt:
            want = d["fname"]
            if existing.name != want:
                try:
                    old_name = existing.name
                    # FIX(L35/B1): Windows 上 rename 到已存在的目标会抛
                    # FileExistsError（WinError 183），原来被 except 吞掉后只把文案
                    # 退回原样——界面每次都承诺「会改名」，实际永远不改。
                    # 目标被别的同名曲占住时，改用「歌名 (2).m4a」。
                    target, tname = unique_dst(out_dir, want)
                    try:
                        existing = existing.rename(target)
                    finally:
                        release_dst(tname)
                    # 改名后索引里还是旧名，不同步的话下次会当成「不存在」重复下载
                    reconcile_compare(out_dir)
                    # FIX(M29): catalog 里也还是旧文件名，删歌判定与追溯会错位。
                    _rename_in_catalog(out_dir, old_name, existing.name)
                    reason = f"已存在（已改名为 {existing.name}）"
                except Exception:
                    reason = d["reason"]
        return SongResult(song, "skip", reason, existing.name, artist=artist)

    if d["fail"] is not None:
        return d["fail"]

    card = d["card"]
    direct = bool(d.get("direct"))
    want_video = direct and fmt == "mp4"
    expect = float((d.get("video") or {}).get("duration") or 0)
    if direct:
        say("pick", f"直链 {card.bvid} · {card.title[:30]}")
    else:
        say("pick", f"选中 {card.play} 播放 · {card.title[:30]}")

    raw = None
    dst = None
    fname = ""
    # FIX(M33): yt-dlp 中途失败时 raw 还是 None，已经写出的分片会永久留在临时目录。
    # 记下开工前的文件快照，收尾时把本次新出现的临时文件一并清掉。
    before: set[str] = set()
    try:
        before = {p.name for p in workdir.iterdir() if p.is_file()}
    except Exception:
        pass
    try:
        _check(cancel)
        fname = d["fname"]
        dst, fname = unique_dst(out_dir, fname)
        if want_video:
            say("download", "下载视频（画面+声音，体积较大）…")
            raw = download_video(
                card.bvid, workdir, page=int((d["video"] or {}).get("page") or 1),
                use_cookies=bool(cfg.get("use_cookies")), cancel=cancel,
                on_progress=on_progress)
            ok, why = video_ok(raw, expect)
            if not ok:
                raise RuntimeError(f"下载文件不完整：{why}")
            _check(cancel)
            say("convert", "写入标签…")
            tag_media(raw, dst, title=song, artist=artist, cancel=cancel)
            ok2, why2 = video_ok(dst, expect)
        else:
            say("download", "下载音频…")
            raw = download_audio(card.bvid, workdir, use_cookies=bool(cfg.get("use_cookies")),
                                 cancel=cancel,
                                 page=int((d.get("video") or {}).get("page") or 1),
                                 on_progress=on_progress)
            if direct:
                # 直链：源时长来自 view 接口（权威值），只比对时长，不做体积倍数校验
                src_dur, _ = probe_media(raw)
                if src_dur <= 0:
                    raise RuntimeError("下载文件读不到时长")
                if expect > 0 and abs(src_dur - expect) > 5:
                    raise RuntimeError(f"下载时长与视频不符（{src_dur:.0f}s / {expect:.0f}s）")
            else:
                ok, why = audio_ok(raw)
                if not ok:
                    raise RuntimeError(f"下载文件不完整：{why}")
                src_dur, _ = probe_media(raw)
            _check(cancel)
            say("convert", "转封装…" if fmt in ("m4a", "mp4") else "转码 mp3…")
            convert_audio(raw, dst, title=song, artist=artist, fmt=fmt, cancel=cancel)
            # FIX(H10): 非直链原来拿 raw（刚下载的文件）自己的时长当基准去比成品，
            # 等于自己跟自己比，永远通过。基准应该是选中视频的时长。
            base_dur = float(card.duration or 0) or src_dur
            ok2, why2 = (duration_ok(dst, expect) if direct
                         else audio_ok(dst, expect_dur=base_dur))
        if not ok2:
            dst.unlink(missing_ok=True)
            raise RuntimeError(f"成品不完整：{why2}")

        dur, _ = probe_media(dst)
        if dur <= 0:
            dst.unlink(missing_ok=True)
            raise RuntimeError("成品无法读取时长")

        record_download(out_dir, {
            "song": song, "file": fname, "title": song, "artist": artist,
            "bytes": dst.stat().st_size, "dur": round(dur, 2), "bv": card.bvid,
            "format": fmt, "pending_artist": pending,
        })
        reason = d["reason"]
        # L43：下载成功 = 用户接受，沉淀 known_good（评测/回放模式不写）
        known_good_put(song, artist, card.bvid, title=card.title, author=card.author)
        return SongResult(song, "ok", reason, fname, card.bvid, artist=artist,
                          pending=pending, video_title=card.title)
    except Cancelled:
        # 用户停止：半成品由 convert_audio / download_audio 自己清掉；
        # 状态记为 stopped，调用方不把它算进失败清单。
        if dst is not None:
            try:
                dst.unlink(missing_ok=True)
            except Exception:
                pass
        return SongResult(song, "stopped", "用户停止", bvid=card.bvid,
                          artist=artist, video_title=card.title)
    except Exception as e:
        if dst is not None:
            try:
                dst.unlink(missing_ok=True)
            except Exception:
                pass
        metrics_event("download_fail", song=song, bvid=card.bvid,
                      fail_kind=_classify_download_error(e))
        return SongResult(song, "fail", f"下载/转换失败：{e}", bvid=card.bvid,
                          video_title=card.title,
                          fail_kind=_classify_download_error(e),
                          fail_detail=f"{type(e).__name__}: {e}")
    finally:
        # FIX(L35/B2)：无论成功失败都要释放 unique_dst 预留的文件名，否则
        # 下一次同名下载会白白跳到「 (2)」，序号越积越多。
        release_dst(fname)
        if raw is not None:
            try:
                raw.unlink(missing_ok=True)
            except Exception:
                pass
        # FIX(M33): 补清本次新出现、但没被 download_* 自己回收的临时文件。
        try:
            for p in workdir.iterdir():
                if not p.is_file() or p.name in before:
                    continue
                if p.name.startswith(".cookies"):
                    continue
                p.unlink(missing_ok=True)
        except Exception:
            pass


def parse_playlist_text(text: str) -> list[str]:
    """把歌单文本解析成歌名列表。"""
    out: list[str] = []
    for line in str(text).splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("#", "//", ";")):
            continue
        s = re.sub(r"^[\-\*\u2022]\s*", "", s)
        s = re.sub(r"^\d+\s*[.、)]\s*", "", s)
        s = re.sub(r"[✅✔]\s*$", "", s).strip()
        if s:
            out.append(s)
    return out


def read_text_file(path: Path) -> str:
    """按 BOM → utf-8-sig → utf-8 → gbk → utf-16 依次尝试解码。"""
    raw = path.read_bytes()
    # FIX(L16): 无 BOM 的 UTF-16 会被 gbk 先「解码成功」，得到带 \x00 的乱码，
    # 歌单里就全是空字符。先按 BOM / NUL 分布判定 UTF-16，再走普通编码表；
    # 并且把「解出来还带 NUL」一律当解码失败。
    encs: list[str] = []
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encs.append("utf-16")
    elif raw and raw.count(b"\x00") > len(raw) // 8:
        le = raw[1::2].count(b"\x00")
        be = raw[0::2].count(b"\x00")
        encs.append("utf-16-le" if le >= be else "utf-16-be")
    encs.extend(["utf-8-sig", "utf-8", "gbk", "utf-16"])
    for enc in encs:
        try:
            text = raw.decode(enc)
            if "\ufffd" not in text and "\x00" not in text:
                return text
        except Exception:
            continue
    raise RuntimeError("无法识别文件编码，请另存为 UTF-8")


# ---------------------------------------------------------------- 停止后续传

RESUME_FILE = _pc.data_dir() / "resume.json"


def save_resume(songs: list[str], index: int, note: str = "",
                output_dir: str = "") -> bool:
    """记下「停在第几首」，下次点开始下载可从这一首接着走。返回是否写盘成功。"""
    try:
        _atomic_write(RESUME_FILE, json.dumps({
            "songs": list(songs),
            "index": int(index),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
            # FIX(M16): 续传点原来不带输出目录，换了目录还会接着往下跑，
            # 结果把剩下的歌续进了另一个文件夹。
            "output_dir": str(output_dir or ""),
        }, ensure_ascii=False, indent=2))
        # FIX(M20): 吞异常后界面还在提示「可续传」，实际没落盘。
        return True
    except Exception as exc:
        _log_swallowed("save_resume", exc)
        return False


def load_resume() -> dict[str, Any] | None:
    """读续传点；歌单对不上或索引越界时返回 None。"""
    try:
        if not RESUME_FILE.exists():
            return None
        data = json.loads(RESUME_FILE.read_text(encoding="utf-8-sig"))
        songs = data.get("songs")
        if not isinstance(songs, list) or not songs:
            return None
        idx = int(data.get("index", 0))
        if not 0 <= idx < len(songs):
            return None
        data["index"] = idx
        return data
    except Exception:
        return None


def clear_resume() -> bool:
    """清掉续传点。返回是否真的清干净（界面据此提示）。"""
    try:
        RESUME_FILE.unlink(missing_ok=True)
        # FIX(M20): 原来吞异常不返回，界面无法知道清理失败。
        return True
    except Exception:
        return False


# ================================================================ 手机端兼容层
# 下面这些是 Android 版 UI（app_main.py / controller.py）用到的入口。
# Windows 版把同样的能力拆成了 video_ref / resolve_video / split_author_choices，
# 这里做一层薄封装，让 Kivy 侧不用改调用点。

UNNAMED = "Unknown"

_TAG_RX = re.compile(r"[【\[][^】\]]{0,20}[】\]]")
_TAIL_NOISE_RX = re.compile(
    r"(?i)\s*[-—–_|｜·]?\s*(官方\s*(?:MV|mv|视频|音频|完整版|live)?|高清|超清|"
    r"无损|音质|hi-?res|4k|2160p?|1080p?|720p?|60fps|修复版?|重制版?|"
    r"完整版|现场版?|live版?|歌词版?|动态歌词|附歌词|字幕版?|"
    r"mv|official|full\s*version|lyrics?)\s*$"
)


def scan_bvid(text: str) -> str:
    """从一串文字里认出 B 站视频号（BV / av / 链接 / b23.tv 短链），认不出返回空串。"""
    # 整行就是视频号、但前缀写成了小写 bv（手机上手动输入很常见）：
    # video_ref 的裸号规则故意区分大小写（避免把 I/l/O 当合法字符），这里单独兜一下。
    _m = re.fullmatch(r"[Bb][Vv]([0-9A-Za-z]{10})", (text or "").strip())
    if _m:
        return "BV" + _m.group(1)
    info = video_ref(text or "")
    if not info or info.get("kind") == "live":
        return ""
    if info.get("kind") == "bv":
        return str(info.get("ref") or "")
    try:
        return str(resolve_video(info).get("bvid") or "")
    except Exception:
        return ""


def video_info(bvid: str) -> Card | None:
    """按 bvid 取单条视频信息（已确认音源，下载阶段不再搜索）。"""
    try:
        v = resolve_video({"kind": "bv", "ref": str(bvid), "page": 1,
                           "span": (0, 0), "raw": str(bvid)})
    except Exception:
        return None
    if not v.get("bvid"):
        return None
    return Card(
        bvid=str(v["bvid"]), title=str(v.get("title") or ""),
        play=int(v.get("play") or 0), duration=int(v.get("duration") or 0),
        author=str(v.get("author") or ""), videos=int(v.get("pages") or 1),
    )


def clean_video_title(title: str) -> str:
    """把 B 站视频标题洗成一个像样的歌名（直下时用）。"""
    t = unicodedata.normalize("NFKC", title or "").strip()
    t = re.sub(r"\s+", " ", t)
    if not t:
        return ""
    m = re.search(r"《([^》]{1,40})》", t)
    if m and m.group(1).strip():
        return m.group(1).strip()
    t = _TAG_RX.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" -—_|｜·")
    prev = None
    while prev != t:
        prev = t
        t = _TAIL_NOISE_RX.sub("", t).strip(" -—_|｜·")
    return (t or title or "")[:40].strip()


def parse_song_entry(text: str) -> tuple[str, str]:
    """把输入拆成 (歌名, 歌手)：只认加号，且从最后一个加号切。"""
    t = (text or "").strip()
    if not t:
        return "", ""
    for sep in ("＋", "+"):
        if sep in t:
            a, b = t.rsplit(sep, 1)
            if a.strip() and b.strip():
                return a.strip(), b.strip()
    return t, ""


def qr_login_generate() -> tuple[str, str, str]:
    """生成扫码登录二维码。返回 (扫码链接, qrcode_key, 错误说明)。"""
    try:
        url, key = qr_login_start()
        return url, key, ""
    except Exception as e:
        return "", "", f"{type(e).__name__}: {e}"


def network_diag() -> str:
    """设置页「网络诊断」用：逐个探接口，把真实错误暴露出来。"""
    lines = []
    targets = [
        ("B站首页", "https://www.bilibili.com/"),
        ("扫码接口", f"{QR_GEN_API}?source=main-fe-header"),
        ("搜索接口", SEARCH_API + "?search_type=video&keyword=test&page=1"),
        ("播放地址", "https://api.bilibili.com/x/player/playurl?bvid=BV1dL411Z7uG&cid=0&fnval=4048&fourk=1"),
    ]
    for name, u in targets:
        t0 = time.time()
        try:
            req = urllib.request.Request(u, headers={
                "User-Agent": UA, "Referer": "https://www.bilibili.com/"})
            with _OPENER.open(req, timeout=20) as r:
                n = len(r.read())
            lines.append(f"{name}: OK {getattr(r, 'status', 0)} {n}B {time.time()-t0:.1f}s")
        except Exception as e:
            lines.append(f"{name}: {type(e).__name__}: {e}")
    import ssl
    lines.append(f"SSL: {ssl.OPENSSL_VERSION}")
    cf = _pc.cookies_file()
    lines.append(f"cookie: {cf.name if cf else '无'}")
    return "\n".join(lines)

