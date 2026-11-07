"""SongGrab 核心库：搜索、选片、下载、转封装、命名、台账。

设计原则：
- 不依赖浏览器；搜索走 B 站公开 web 接口。
- 下载走 yt-dlp（Python API），转封装走 ffmpeg（子进程，静默）。
- 所有外部进程一律 CREATE_NO_WINDOW，避免弹黑框。
"""

from __future__ import annotations

import http.cookiejar
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import platform_compat as _pc
import metasrc
import picker as _sel

# ---------------------------------------------------------------- 路径与常量

def app_dir() -> Path:
    """程序所在目录（打包后为 exe 同级目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# 让 HTTPS 校验在打包环境里也能找到 CA 证书
def _init_ssl() -> None:
    """把 CA 证书包路径写进环境变量（给 yt-dlp / requests 用）。"""
    try:
        ca = _pc.ca_bundle()
        if ca:
            for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
                os.environ[k] = str(ca)
    except Exception:
        pass


_init_ssl()

APP_DIR = app_dir()
FFMPEG = _pc.ffmpeg_path()
ARTISTS_FILE = _pc.artists_file()
SETTINGS_FILE = _pc.settings_file()

SEARCH_API = "https://api.bilibili.com/x/web-interface/wbi/search/type"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

# ---------------------------------------------------------------- B 站会话
# 搜索接口裸请求第 2 次就 412。带上首页发的 buvid3 cookie 并加 wbi 签名后
# 连续请求稳定通过（实测 5/5 → 10/10）。
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

_COOKIE_JAR = None
_MIXIN_KEY = ""
_MIXIN_AT = 0.0
_OPENER = None


def _jar():
    """CookieJar 懒加载：http.cookiejar 导入要 ~330ms，首次用时再导入。"""
    global _COOKIE_JAR
    if _COOKIE_JAR is None:
        import http.cookiejar
        _COOKIE_JAR = http.cookiejar.CookieJar()
    return _COOKIE_JAR


def _ssl_context():
    """显式指定 CA 包。Android 上 ssl 默认找不到证书，会 CERTIFICATE_VERIFY_FAILED。"""
    import ssl
    ca = _pc.ca_bundle()
    try:
        if ca:
            return ssl.create_default_context(cafile=str(ca))
    except Exception:
        pass
    return ssl.create_default_context()


def _opener():
    global _OPENER
    if _OPENER is None:
        handlers = [urllib.request.HTTPCookieProcessor(_jar())]
        try:
            handlers.insert(0, urllib.request.HTTPSHandler(context=_ssl_context()))
        except Exception:
            pass
        _OPENER = urllib.request.build_opener(*handlers)
    return _OPENER


_USER_COOKIES_LOADED = False


def _load_user_cookies() -> bool:
    """载入用户导入的 cookies.txt（登录态），搜索与下载共用。"""
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
            _jar().set_cookie(c)
            n += 1
        return n > 0
    except Exception:
        return False


def _boot_cookie() -> None:
    """先载入用户登录 cookie；没有就访问首页拿 buvid3。"""
    _load_user_cookies()
    if any(c.name == "buvid3" for c in _jar()):
        return
    try:
        req = urllib.request.Request(
            "https://www.bilibili.com/",
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        _opener().open(req, timeout=15).close()
    except Exception:
        pass


def _get_mixin_key() -> str:
    """wbi 密钥由 nav 接口的 img_url/sub_url 拼出来，按固定表打乱取前 32 位。"""
    global _MIXIN_KEY, _MIXIN_AT
    if _MIXIN_KEY and time.time() - _MIXIN_AT < 3600:
        return _MIXIN_KEY
    _boot_cookie()
    try:
        req = urllib.request.Request(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        )
        with _opener().open(req, timeout=15) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
        wbi = (j.get("data") or {}).get("wbi_img") or {}
        img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if img and sub:
            _MIXIN_KEY = "".join((img + sub)[i] for i in _MIXIN_TAB)[:32]
            _MIXIN_AT = time.time()
    except Exception:
        pass
    return _MIXIN_KEY


def _wbi_sign(params: dict[str, Any]) -> dict[str, Any]:
    key = _get_mixin_key()
    p = dict(params)
    p["wts"] = int(time.time())
    p = dict(sorted(p.items()))
    p = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in p.items()}
    q = urllib.parse.urlencode(p)
    if key:
        p["w_rid"] = hashlib.md5((q + key).encode()).hexdigest()
    return p


QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"


def save_login_cookies() -> tuple[bool, str]:
    """把当前会话 cookie 落盘成 cookies.txt（供下次启动复用）。"""
    try:
        rows = ["# Netscape HTTP Cookie File"]
        for c in _jar():
            dom = c.domain if c.domain.startswith(".") else "." + c.domain
            rows.append("\t".join([
                dom, "TRUE", c.path or "/", "FALSE",
                str(int(c.expires or 0)), c.name, str(c.value),
            ]))
        if len(rows) <= 1:
            return False, "会话里没有 cookie"
        dest = _pc.data_dir() / "cookies.txt"
        dest.write_text("\n".join(rows) + "\n", encoding="utf-8")
        has = any(c.name == "SESSDATA" for c in _jar())
        return True, ("登录成功，已保存登录态" if has else "已保存，但没拿到 SESSDATA")
    except Exception as e:
        return False, f"保存失败：{e}"


def qr_login_generate() -> tuple[str, str, str]:
    """生成扫码登录二维码。返回 (扫码链接, qrcode_key, 错误说明)。"""
    _boot_cookie()
    try:
        req = urllib.request.Request(
            QR_GENERATE,
            headers={
                "User-Agent": UA,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        with _opener().open(req, timeout=25) as r:
            raw = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            import zlib
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        j = json.loads(raw.decode("utf-8", "ignore"))
        if j.get("code") != 0:
            return "", "", f"接口拒绝：code={j.get('code')} {j.get('message', '')}".strip()
        d = j.get("data") or {}
        url = str(d.get("url") or "")
        key = str(d.get("qrcode_key") or "")
        if not url or not key:
            return "", "", "返回数据为空（可能被风控）"
        return url, key, ""
    except Exception as e:
        return "", "", f"{type(e).__name__}: {e}"


def network_diag() -> str:
    """设置页「网络诊断」用：逐个探接口，把真实错误暴露出来。"""
    lines = []
    targets = [
        ("B站首页", "https://www.bilibili.com/"),
        ("扫码接口", QR_GENERATE),
        ("搜索接口", SEARCH_API + "?search_type=video&keyword=test&page=1"),
    ]
    for name, u in targets:
        t0 = time.time()
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA,
                                                     "Referer": "https://www.bilibili.com/"})
            with _opener().open(req, timeout=20) as r:
                n = len(r.read())
            lines.append(f"{name}: OK {r.status} {n}B {time.time()-t0:.1f}s")
        except Exception as e:
            lines.append(f"{name}: {type(e).__name__}: {e}")
    import ssl
    lines.append(f"SSL: {ssl.OPENSSL_VERSION}")
    cf = _pc.cookies_file()
    lines.append(f"cookie: {cf.name if cf else '无'}")
    return "\n".join(lines)


def qr_login_poll(qrcode_key: str) -> tuple[int, str]:
    """轮询扫码状态。0=成功 86101=未扫 86090=待确认 86038=已过期。"""
    try:
        q = urllib.parse.urlencode({"qrcode_key": qrcode_key, "source": "main-fe-header"})
        req = urllib.request.Request(
            f"{QR_POLL}?{q}",
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        )
        with _opener().open(req, timeout=15) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
        d = j.get("data") or {}
        code = int(d.get("code", -1))
        if code == 0:
            global _USER_COOKIES_LOADED
            _USER_COOKIES_LOADED = True
            ok, info = save_login_cookies()
            return (0, info) if ok else (-2, info)
        return code, str(d.get("message") or "")
    except Exception as e:
        return -1, f"网络异常：{e}"


def cookie_header() -> str:
    """当前会话的 Cookie 串，供 yt-dlp 下载时复用。"""
    _boot_cookie()
    return "; ".join(f"{c.name}={c.value}" for c in _jar())


DEFAULT_PRIORITY: list[str] = []   # 不再预置，交给用户自己配

# 非歌曲类标题黑名单
TITLE_BLOCK = re.compile(
    r"(教程|教学|如何|怎么|手书|攻略|实况|通关|记录|谱面|自制谱|全连|首杀|"
    r"理论值|解说|混剪|AMV|MAD|歌ってみた|翻唱|cover|reaction|合集|十选|"
    r"排行榜|盘点|chart|anim|animation|预告|宣传片)",
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
}

# ---------------------------------------------------------------- 配置

DEFAULT_SETTINGS: dict[str, Any] = {
    "output_dir": "",            # 空字符串 = 桌面
    "min_play": 1,               # 播放量门槛，默认只挡 play==0（冻结文档 §2.8）
    "min_dur": 50,               # 最短时长（秒，冻结文档 §2.7）
    "max_dur": 720,              # 最长时长（秒）
    "rules": 2,                  # 选片规则版本；1 = 冻结版之前的老门槛
    "priority_words": list(DEFAULT_PRIORITY),
    "use_cookies": False,
    "last_txt_dir": "",
}


_LEGACY_PRIORITY = ["本家", "百万级装备听", "百万级录音棚听"]


def load_settings() -> dict[str, Any]:
    cfg = dict(DEFAULT_SETTINGS)
    raw: dict[str, Any] = {}
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    # 迁移：旧版预置的三个词条清掉，让用户自己加
    if cfg.get("priority_words") == _LEGACY_PRIORITY:
        cfg["priority_words"] = []
    # 迁移：选片规则换成冻结版后，老门槛（播放量 1 万 / 时长 90s）会把
    # 合格候选挡在外面（§2 ⑦⑧ 默认 50–720s、play>=1），这里一次性重置
    if int(raw.get("rules") or 1) < 2:
        cfg["min_play"] = DEFAULT_SETTINGS["min_play"]
        cfg["min_dur"] = DEFAULT_SETTINGS["min_dur"]
        cfg["max_dur"] = DEFAULT_SETTINGS["max_dur"]
        cfg["rules"] = 2
    return cfg


def save_settings(cfg: dict[str, Any]) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def desktop_dir() -> Path:
    """默认输出目录：桌面用 ~/Music/SongGrab，Android 用 /sdcard/Music/SongGrab。"""
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
    """打开文件夹：Windows 资源管理器 / Linux xdg-open / Android 文件管理器。"""
    return _pc.open_folder(folder, select_file)


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


def load_artists() -> dict[str, str]:
    try:
        if ARTISTS_FILE.exists():
            data = json.loads(ARTISTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------- 文本处理

def normalize(s: str) -> str:
    """去标点、繁简归一、小写，只保留字母数字与中日文。"""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    s = "".join(ch for ch in s if ch.isalnum())
    return "".join(VARIANT.get(ch, ch) for ch in s)


TITLE_SPLIT = re.compile(r"[\s【】\[\]（）()『』「」<>《》/／|｜~～\-—_,，.。:：;；+]+")


def title_tokens(title: str) -> list[str]:
    return [t for t in TITLE_SPLIT.split(str(title)) if t]


def title_matched(title: str, keyword: str) -> bool:
    """标题是否真的对得上歌名（挡住同名不同曲）。"""
    k = normalize(keyword)
    if not k:
        return False
    # 多词拉丁歌名（含空格）：整标题包含即可，避免被空格切碎
    if re.search(r"\s", str(keyword).strip()) and k in normalize(title):
        return True
    for tok in title_tokens(title):
        n = normalize(tok)
        if not n:
            continue
        if n == k:
            return True
        if k in n and len(n) - len(k) <= 2:
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


def sanitize_filename(name: str, max_bytes: int = 120) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", str(name))
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    raw = name.encode("utf-8")
    if len(raw) > max_bytes:
        name = raw[:max_bytes].decode("utf-8", "ignore").rstrip(". ")
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
    typeid: int = 0
    videos: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _http_json(url: str, timeout: int = 15) -> dict[str, Any]:
    _boot_cookie()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": "https://search.bilibili.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


_MIN_INTERVAL = 1.2
_last_req = 0.0
_SEARCH_CACHE: dict[str, tuple[float, list[Card]]] = {}
_CACHE_TTL = 300.0


def _throttle() -> None:
    global _last_req
    gap = time.time() - _last_req
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _last_req = time.time()


def search_page(keyword: str, page: int = 1, order: str = "click", tids: int | None = None) -> list[Card]:
    params = {"search_type": "video", "keyword": keyword, "order": order, "page": str(page)}
    if tids:
        params["tids_1"] = str(tids)
    url = f"{SEARCH_API}?{urllib.parse.urlencode(_wbi_sign(params))}"
    _throttle()
    data = _http_json(url)
    if data.get("code") != 0:
        raise RuntimeError(f"搜索接口返回 code={data.get('code')} {data.get('message')}")
    result = (data.get("data") or {}).get("result") or []
    cards: list[Card] = []
    for it in result:
        title = re.sub(r"<.*?>", "", str(it.get("title") or ""))
        cards.append(Card(
            bvid=str(it.get("bvid") or ""), title=title,
            play=int(it.get("play") or 0),
            duration=parse_duration(it.get("duration") or "0:0"),
            author=str(it.get("author") or ""),
            typeid=int(it.get("typeid") or 0),
            videos=int(it.get("videos") or 0),
        ))
    return [c for c in cards if c.bvid]


_RETRY_CODES = ("412", "429", "503", "Precondition Failed", "Too Many Requests")


def _search_retry(keyword: str, page: int = 1, tids: int | None = None,
                  order: str = "click", tries: int = 3) -> list[Card]:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return search_page(keyword, page=page, tids=tids, order=order)
        except Exception as e:
            last = e
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
    if last:
        raise last
    return []


def search_both(keyword: str, page: int = 1) -> list[Card]:
    """§1：音乐分区（totalrank）+ 综合区（click），按 bvid 去重，留音乐分区那条。"""
    keyword = str(keyword or "").strip()
    if not keyword:
        return []
    now = time.time()
    hit = _SEARCH_CACHE.get(keyword)
    if hit and now - hit[0] < _CACHE_TTL:
        return list(hit[1])

    merged: dict[str, Card] = {}
    ok_routes = 0
    try:
        for c in _search_retry(keyword, page=page, tids=3, order="totalrank"):
            c.from_music = True
            merged[c.bvid] = c
        ok_routes += 1
    except Exception:
        pass
    try:
        for c in _search_retry(keyword, page=page, order="click"):
            merged.setdefault(c.bvid, c)
        ok_routes += 1
    except Exception:
        pass
    if ok_routes == 0:
        raise RuntimeError("B站搜索被限流，请稍后重试")

    cards = list(merged.values())
    if not cards:
        # B站被风控时会返回 code=0 但结果为空，别把空结果写进缓存
        raise RuntimeError("B站搜索无结果（可能被风控，请稍后重试）")
    if len(_SEARCH_CACHE) > 64:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[keyword] = (now, cards)
    return list(cards)


def video_info(bvid: str) -> Card | None:
    """按 bvid 取单条视频信息（已确认音源，下载阶段不再搜索）。"""
    try:
        url = f"{VIEW_API}?bvid={urllib.parse.quote(str(bvid))}"
        data = _http_json(url)
        d = data.get("data") or {}
        if not d:
            return None
        return Card(
            bvid=str(d.get("bvid") or bvid), title=str(d.get("title") or ""),
            play=int(((d.get("stat") or {}).get("view")) or 0),
            duration=int(d.get("duration") or 0),
            author=str(((d.get("owner") or {}).get("name")) or ""),
            typeid=int(d.get("tid") or 0), videos=int(d.get("videos") or 0),
        )
    except Exception:
        return None


# ---------------------------------------------------------------- 选片

@dataclass
class PickResult:
    card: Card | None
    reason: str
    candidates: list[Card] = field(default_factory=list)


_REGEX_HINT = set("|()[]{}+*?^$\\")


def _match_word(word: str, title: str) -> bool:
    """优先词匹配：含正则元字符就按正则，否则按子串。

    这样普通用户写「本家」照样能用，高级用户可写「(Hi-?res|无损)」。
    正则写坏了自动退回子串，不会静默失效。
    """
    w = str(word).strip()
    if not w:
        return False
    if any(ch in _REGEX_HINT for ch in w):
        try:
            return re.search(w, title, re.IGNORECASE) is not None
        except re.error:
            pass
    return w.lower() in title.lower()


def pick_card(
    cards: list[Card],
    keyword: str,
    min_play: int = 1,
    min_dur: int = 50,
    priority_words: list[str] | None = None,
    strict_music: bool = True,
    max_dur: int = 720,
    artist: str = "",
    ref_dur: int = 0,
) -> PickResult:
    """§2–§4 的包装：真正的规则在 select.py（对应冻结文档）。"""
    picked = _sel.pick(
        cards, keyword,
        min_play=int(min_play), min_dur=int(min_dur), max_dur=int(max_dur),
        priority_words=priority_words if priority_words is not None else DEFAULT_PRIORITY,
        canonical=artist, ref_dur=int(ref_dur),
    )
    card = picked.cand.raw if picked.cand is not None else None
    return PickResult(card, picked.reason, [c.raw for c in picked.top])


def describe_card(card: Card | None) -> str:
    if card is None:
        return ""
    return f"{card.title[:42]} · {card.author} · {card.play} 播 · {card.duration}s"


def trial_song(raw: str, cfg: dict[str, Any], progress=None) -> dict[str, Any]:
    """§8.1–8.4 试搜：查作者 → 带作者搜 → 分支判定 → 三档结果。"""
    say = progress or (lambda *a, **k: None)
    song, hint = parse_song_entry(raw)
    base: dict[str, Any] = {
        "stage": "fail", "song": song, "bvid": "", "title": "", "author": "",
        "play": 0, "duration": 0, "artist": "", "ref_dur": 0, "source": "",
        "reason": "", "candidates": [], "pool_n": 0, "_card": None, "_pool": [],
    }
    if not song:
        base["reason"] = "先输入歌名"
        return base

    min_play = int(cfg.get("min_play", 1))
    min_dur = int(cfg.get("min_dur", 50))
    max_dur = int(cfg.get("max_dur", 720))
    words = cfg.get("priority_words", DEFAULT_PRIORITY)

    say("artist", f"查曲师「{song}」…")
    try:
        artist, ref_dur, source = metasrc.lookup(song, hint)
    except Exception:
        artist, ref_dur, source = hint.strip(), 0, ("用户指定" if hint else "")
    base.update(artist=artist, ref_dur=ref_dur, source=source)
    say("artist", f"{source}：{artist}" + (f"（{ref_dur}s）" if ref_dur else "")
        if artist else "平台没查到作者，走不带作者的搜索")

    canonical = artist if (artist and metasrc.artist_ok(artist)) else ""

    def _search(kw: str) -> list[Card]:
        try:
            say("search", f"搜索「{kw}」")
            return search_both(kw)
        except Exception as e:
            say("search", f"搜索失败：{e}")
            return []

    def _pick(cards: list[Card], canon: str) -> Any:
        return _sel.pick(
            cards, song, min_play=min_play, min_dur=min_dur, max_dur=max_dur,
            priority_words=words, canonical=canon, ref_dur=ref_dur,
        )

    kw = f"{song} {artist}".strip() if artist else song
    cards = _search(kw)
    if not cards and artist and kw != song:
        # 带作者搜不到（B站对「歌名 作者」这种组合经常 0 条）先回退纯歌名
        say("search", "带作者搜索无结果，回退不带作者")
        cards = _search(song)
    if not cards:
        base["reason"] = "搜索无结果（可能被风控，稍后重试）"
        return base
    picked = _pick(cards, canonical)
    pool = picked.pool

    hot = len([c for c in pool if c.play > 10000])
    branch1 = len(pool) > 5 and hot >= 2

    if not branch1 and artist:
        say("search", f"命中不足（{len(pool)} 条，过万 {hot} 条），回退不带作者")
        merged: dict[str, Card] = {c.bvid: c for c in cards}
        for c in _search(song):
            merged.setdefault(c.bvid, c)
        picked = _pick(list(merged.values()), "")
        picked.rebuilt = True
        pool = picked.pool

    base["pool_n"] = len(pool)
    if not pool:
        base["reason"] = picked.reason or "无搜索结果"
        return base

    if branch1 and picked.cand is not None and canonical:
        top = picked.cand
        full = (normalize(song) in normalize(top.title)
                and normalize(canonical) in normalize(top.title))
        unique = len(pool) == 1 or abs(pool[0].score - pool[1].score) > 1e-9
        stage = "ok" if (full and unique) else "confirm"
        reason = "完全命中：标题同时含歌名和作者" if stage == "ok" else "待确认：多个高分候选"
    else:
        stage = "confirm"
        reason = "待确认：候选不足或跨歌手，取加权前三"

    chosen, who, why = _sel.choose_card(pool, song, canonical=canonical, ref_dur=ref_dur)
    if chosen is None:
        chosen = pool[0]
        why = "作者判定链没定位到正片，退回加权第一"
    cand = chosen.raw
    base.update(
        stage=stage, bvid=cand.bvid, title=cand.title, author=cand.author,
        play=cand.play, duration=cand.duration,
        artist=(who or artist or ""), reason=f"{reason}；{why}",
        _card=cand, _pool=pool,
        candidates=[{"bvid": c.bvid, "title": c.title, "author": c.author,
                     "play": c.play, "duration": c.duration,
                     "score": round(c.score, 4)} for c in pool[:3]],
    )
    return base


# ---------------------------------------------------------------- 下载与转换

def _dump_cookiefile(path: Path) -> bool:
    """把会话 cookie 写成 yt-dlp 的 Netscape 格式，下载时复用。"""
    try:
        rows = ["# Netscape HTTP Cookie File"]
        for c in _jar():
            dom = c.domain if c.domain.startswith(".") else "." + c.domain
            rows.append("\t".join([
                dom, "TRUE", c.path or "/", "FALSE",
                str(int(c.expires or 0)), c.name, str(c.value),
            ]))
        if len(rows) <= 1:
            return False
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


class _QuietLogger:
    """yt-dlp 的 logger：全部吞掉，避免它去碰 sys.stderr。"""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def download_audio(bvid: str, workdir: Path, use_cookies: bool = False) -> Path:
    """用 yt-dlp 只下音频，返回落盘文件路径。"""
    import io
    import yt_dlp  # 延迟导入，便于单测不依赖

    workdir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in workdir.iterdir() if p.is_file()}
    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        # p4a 环境里 sys.stdout/stderr 不是正常文件对象，yt-dlp 写日志会抛
        # 'str' object has no attribute 'write'，这里彻底切断它的输出通道
        "logger": _QuietLogger(),
        "no_color": True,
    }
    # 有用户 cookie 就优先用（登录态能解锁高音质）；否则用会话 buvid3
    _load_user_cookies()
    cf = _pc.cookies_file()
    if cf is None:
        tmp = workdir / ".session_cookies.txt"
        if _dump_cookiefile(tmp):
            cf = tmp
    if cf is not None:
        opts["cookiefile"] = str(cf)

    url = f"https://www.bilibili.com/video/{bvid}"
    _old_out, _old_err = sys.stdout, sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    finally:
        sys.stdout, sys.stderr = _old_out, _old_err

    fresh = [
        p for p in workdir.iterdir()
        if p.is_file() and p.name not in before and not p.name.endswith((".part", ".ytdl"))
    ]
    if not fresh:
        raise RuntimeError("yt-dlp 未产出文件")
    fresh.sort(key=lambda p: p.stat().st_size, reverse=True)
    return fresh[0]


def _m4a_tag_pure(src: Path, dst: Path, title: str, artist: str) -> None:
    """无 ffmpeg 时的纯 Python 路径：直接落地 + 写 MP4 标签。

    B 站 DASH 音频本身就是 AAC/m4a，复制容器即可，不需要重封装。
    """
    if src.resolve() != dst.resolve():
        shutil.copyfile(src, dst)
    try:
        from mutagen.mp4 import MP4  # 纯 Python，Android 可装
        a = MP4(str(dst))
        a["\xa9nam"] = [title]
        a["\xa9ART"] = [artist]
        a["aART"] = [artist]
        a.save()
    except Exception:
        pass  # 没标签也能听，不让它挡住下载


def _mp4_duration_pure(path: Path) -> float:
    """不依赖第三方库，直接读 MP4 的 mvhd box 拿时长。

    鸿蒙等 ROM 上 mutagen 可能导入失败，成品明明完好却被判成「读不出时长」。
    """
    try:
        total = path.stat().st_size
        with path.open("rb") as f:
            pos = 0
            while pos + 8 <= total:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                size = int.from_bytes(hdr[0:4], "big")
                kind = hdr[4:8]
                if size == 1:
                    size = int.from_bytes(f.read(8), "big")
                elif size == 0:
                    size = total - pos
                if size < 8:
                    break
                if kind == b"sidx":
                    # 分片 MP4（B站 DASH 原始流）：moov 的 duration 是 0，
                    # 真实时长要从 sidx 里把各分片累计起来
                    body = f.read(max(0, min(size - 8, 8192)))
                    if len(body) < 22:
                        return 0.0
                    ver = body[0]
                    ts = int.from_bytes(body[8:12], "big")
                    off = 12
                    off += 4 if ver == 0 else 8      # earliest_presentation_time
                    off += 4 if ver == 0 else 8      # first_offset
                    off += 2                          # reserved
                    if off + 2 > len(body):
                        return 0.0
                    count = int.from_bytes(body[off:off + 2], "big")
                    off += 2
                    total = 0
                    # 每个 reference 12 字节：ref_type+size(4) dur(4) sap(4)
                    for _ in range(count):
                        if off + 12 > len(body):
                            break
                        total += int.from_bytes(body[off + 4:off + 8], "big")
                        off += 12
                    return float(total) / ts if ts else 0.0
                if kind == b"moov":
                    end = pos + size
                    p2 = pos + 8
                    while p2 + 8 <= end:
                        f.seek(p2)
                        h2 = f.read(8)
                        if len(h2) < 8:
                            break
                        s2 = int.from_bytes(h2[0:4], "big")
                        t2 = h2[4:8]
                        if s2 == 1:
                            s2 = int.from_bytes(f.read(8), "big")
                        elif s2 == 0:
                            s2 = end - p2
                        if t2 == b"mvhd":
                            body = f.read(min(32, max(0, s2 - 8)))
                            if len(body) < 20:
                                break
                            if body[0] == 1:
                                if len(body) < 32:
                                    break
                                ts = int.from_bytes(body[20:24], "big")
                                dur = int.from_bytes(body[24:32], "big")
                            else:
                                ts = int.from_bytes(body[12:16], "big")
                                dur = int.from_bytes(body[16:20], "big")
                            val = float(dur) / ts if ts else 0.0
                            if val > 0:
                                return val
                            # 分片 MP4 的 mvhd 时长恒为 0，继续往后找 sidx
                            break
                        if s2 <= 0:
                            break
                        p2 += s2
                pos += size
    except Exception:
        return 0.0
    return 0.0


def _m4a_ok(path: Path) -> bool:
    """成品是否真的可用：存在、不是空壳、能读出时长。"""
    try:
        if not path.exists() or path.stat().st_size < 16 * 1024:
            return False
        return _mp4_duration_pure(path) > 0.5
    except Exception:
        return False


def convert_to_m4a(src: Path, dst: Path, title: str, artist: str) -> None:
    exe = Path(FFMPEG) if FFMPEG else None
    if exe and Path(exe).exists():
        cmd = [
            str(exe), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src),
            "-vn", "-map", "0:a:0", "-c:a", "copy",
            "-metadata", f"title={title}",
            "-metadata", f"artist={artist}",
            "-metadata", f"album_artist={artist}",
            "-movflags", "+faststart",
            str(dst),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, creationflags=NO_WINDOW, timeout=300
            )
            # 有的 ROM 上 ffmpeg 会「返回 0 但产出空文件」，不能只看返回码
            if proc.returncode == 0 and _m4a_ok(dst):
                return
            # 非 AAC 源 copy 会失败，转 AAC 再试一次
            cmd2 = [c for c in cmd]
            i = cmd2.index("-c:a")
            cmd2[i + 1] = "aac"
            cmd2.insert(i + 2, "-b:a")
            cmd2.insert(i + 3, "192k")
            proc2 = subprocess.run(
                cmd2, capture_output=True, creationflags=NO_WINDOW, timeout=600
            )
            if proc2.returncode == 0 and _m4a_ok(dst):
                return
        except Exception:
            # Android 上可能因 SELinux 禁止 exec（非 nativeLibraryDir）而失败
            pass
    # 兜底：直接落地 + mutagen 写标签，保证下载不因 ffmpeg 不可用而失败
    try:
        dst.unlink(missing_ok=True)   # 清掉 ffmpeg 留下的半成品
    except Exception:
        pass
    _m4a_tag_pure(src, dst, title, artist)


def _diag_file(path: Path) -> str:
    """失败现场诊断：文件头、各解析器结果。"""
    parts = []
    try:
        with path.open("rb") as f:
            parts.append("head=" + f.read(16).hex())
    except Exception as e:
        parts.append(f"head=ERR({type(e).__name__})")
    try:
        from mutagen.mp4 import MP4
        parts.append(f"mut={float(MP4(str(path)).info.length):.2f}")
    except Exception as e:
        parts.append(f"mut=ERR({type(e).__name__})")
    parts.append(f"pure={_mp4_duration_pure(path):.2f}")
    return " ".join(parts)


def probe_duration(path: Path) -> float:
    exe = Path(FFMPEG) if FFMPEG else None
    if exe and Path(exe).exists():
        try:
            proc = subprocess.run(
                [str(exe), "-hide_banner", "-i", str(path)],
                capture_output=True, creationflags=NO_WINDOW, timeout=60,
            )
            text = (proc.stderr or b"").decode("utf-8", "ignore")
            m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        except Exception:
            # 鸿蒙等 ROM 上 exec 可能被拒，直接退回纯 Python 读时长
            pass
    try:
        from mutagen.mp4 import MP4
        d = float(MP4(str(path)).info.length)
        if d > 0:
            return d
    except Exception:
        pass
    return _mp4_duration_pure(path)


# ---------------------------------------------------------------- 台账

def catalog_path(out_dir: Path) -> Path:
    return out_dir / "catalog.json"


def load_catalog(out_dir: Path) -> list[dict[str, Any]]:
    p = catalog_path(out_dir)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def append_catalog(out_dir: Path, entry: dict[str, Any]) -> None:
    rows = [r for r in load_catalog(out_dir) if r.get("file") != entry.get("file")]
    rows.append(entry)
    try:
        catalog_path(out_dir).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def find_existing(out_dir: Path, song: str) -> Path | None:
    """幂等：同歌名的成品已存在则返回它。"""
    prefix = sanitize_filename(song) + "+"
    try:
        for p in out_dir.iterdir():
            if p.is_file() and p.suffix.lower() == ".m4a" and p.name.startswith(prefix):
                return p
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 主流程

@dataclass
class SongResult:
    song: str
    status: str          # ok / skip / fail / confirm
    reason: str = ""
    file: str = ""
    bvid: str = ""
    artist: str = ""
    pending: bool = False  # 曲师未查到，暂用 UP 主名
    title: str = ""        # confirm 时给候选标题
    candidates: list = field(default_factory=list)  # confirm 时的前三个候选


# ---------------------------------------------------------------- 联网查曲师

NETEASE_SEARCH = "https://music.163.com/api/search/get"


def parse_song_entry(text: str) -> tuple[str, str]:
    """把输入拆成 (歌名, 歌手)。

    规则只有一条：**加号分隔**。加号之前是歌名，之后是歌手。
      "晴天+周杰伦" / "晴天 + 周杰伦" / "晴天＋周杰伦"（全角）
    其余一律当成完整歌名：
      "Evans Gambit" / "起风了 - 完整版" / "童话 光良" / "Shape of You"

    这样歌名里自带的空格、破折号、竖线都不会被误判成歌手。
    """
    t = (text or "").strip()
    if not t:
        return "", ""
    for sep in ("＋", "+"):
        if sep in t:
            a, b = t.rsplit(sep, 1)
            if a.strip() and b.strip():
                return a.strip(), b.strip()
    return t, ""


_FAKE_ARTIST = re.compile(
    r"^(我想|这是|那个|这个|我们|你们|他们|什么|怎么|就是|如果|因为|所以|但是|"
    r"一个|一首|今天|昨天|明天|大家|自己|真的|还是|已经|可以|没有|知道|觉得)$"
)


def artist_from_title(title: str, song: str) -> str:
    """从 B 站标题里抠歌手，常见格式：
    「歌手《歌名》」/「歌手 - 歌名」/「《歌名》- 歌手」
    """
    if not title or not song:
        return ""
    esc = re.escape(song)
    keep = r"[^\s【】\[\]（）()《》/|,，。！!…~\-—–]{2,16}"

    m = re.search(r"(" + keep + r")\s*《\s*" + esc, title)
    if not m:
        m = re.search(r"(" + keep + r")\s*[-—–]\s*" + esc, title)
    if not m:
        m = re.search("《" + esc + r"》\s*[-—–]\s*(" + keep + ")", title)
    if not m:
        return ""
    cand = m.group(1).strip(" 【】[]()（）-—_")
    cand = re.sub(r"^(4K|8K|HD|高清|修复|完整版)", "", cand, flags=re.I).strip()
    if len(cand) < 2 or len(cand) > 16 or _FAKE_ARTIST.match(cand):
        return ""
    return cand


def resolve_artist(song: str, card_title: str, net_artist: str) -> str:
    """标题与网易云交叉验证。

    网易云缺版权时会返回翻唱者（如「晴天」→ Lucky小爱），
    而 B 站标题通常写着原唱（周杰伦《晴天》），对不上时信标题。
    """
    t = artist_from_title(card_title, song)
    if t and net_artist:
        if net_artist.lower() in (card_title or "").lower():
            return net_artist        # 标题印证了网易云
        return t                     # 不一致，以视频标题为准
    return t or net_artist or ""


def lookup_artist(song: str) -> tuple[str, str]:
    """联网查曲师（网易云搜索）。返回 (曲师, 来源)；查不到返回 ("", "")。"""
    try:
        body = urllib.parse.urlencode(
            {"s": song, "type": 1, "limit": 5, "offset": 0}
        ).encode()
        req = urllib.request.Request(
            NETEASE_SEARCH, data=body,
            headers={"User-Agent": UA, "Referer": "https://music.163.com/"},
        )
        with _opener().open(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        songs = ((data.get("result") or {}).get("songs") or [])[:5]
        nb = normalize(song)
        for s in songs:
            name = str(s.get("name") or "")
            na = normalize(name)
            if not na or not nb:
                continue
            if not (na in nb or nb in na):
                continue
            arts = [str(a.get("name") or "").strip() for a in (s.get("artists") or [])]
            arts = [a for a in arts if a]
            if arts:
                return arts[0], "网易云"
    except Exception:
        pass
    return "", ""


def save_artist(song: str, artist: str) -> None:
    """把查到的曲师写回 artists.json。"""
    try:
        data = load_artists()
        data[song] = artist
        ARTISTS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def process_song(
    song: str,
    cfg: dict[str, Any],
    workdir: Path,
    progress=None,
    artist_hint: str = "",
    bvid: str = "",
    artist: str = "",
    no_artist: bool = False,
) -> SongResult:
    """处理单首歌（§8.1：已确认音源就按 bvid 直下，下载阶段不再搜索）。

    artist_hint = 用户用 `+` 写的作者
    artist      = 界面确认/改过的作者
    bvid        = 界面已确认的候选
    no_artist   = 用户主动标「未命名」：不查作者、不写作者，文件名叫「歌名+未命名」
    """
    say = progress or (lambda *a, **k: None)
    out_dir = resolve_output_dir(cfg)
    artists = load_artists()

    artist = "" if no_artist else (artist or artist_hint or "").strip()

    # 已有成品：曲师变了就顺手改名，避免补表后留下旧名字
    existing = find_existing(out_dir, song)
    if existing:
        if artist:
            want = sanitize_filename(f"{song}+{artist}") + ".m4a"
            if existing.name != want:
                try:
                    existing = existing.rename(out_dir / want)
                except Exception:
                    pass
        return SongResult(song, "skip", "已存在", existing.name, artist=artist)

    card: Card | None = None
    reason = ""
    if bvid:
        say("pick", f"已确认音源 {bvid}，直接下载")
        card = video_info(bvid)
        if card is None:
            return SongResult(song, "fail", "取不到已确认音源的详情", bvid=bvid)
        reason = "已确认音源"
        if not artist and not no_artist:
            try:
                artist = metasrc.lookup(song, "")[0]
            except Exception:
                artist = ""
    else:
        tr = trial_song(f"{song}+{artist_hint}" if artist_hint else song, cfg, say)
        if not tr["bvid"]:
            return SongResult(song, "fail", tr["reason"] or "搜索无结果")
        card = tr["_card"]
        reason = tr["reason"] or "试搜选中"
        if not artist and not no_artist:
            artist = tr.get("artist") or ""
        say("pick", f"选中 {card.play} 播 · {card.title[:30]}")

    if artist and not artist_hint:
        # 平台/标题判出来的作者写进缓存，下次断网也有兜底
        try:
            save_artist(song, artist)
        except Exception:
            pass
    if not artist and not no_artist:
        artist = artists.get(song, "").strip()

    pending = False
    if not artist and not no_artist:
        artist = (card.author or "").strip() or "未知"
        pending = True
        say("artist", f"未查到曲师，暂用 UP 主「{artist}」")

    raw = None
    try:
        say("download", "下载音频…")
        raw = download_audio(card.bvid, workdir, use_cookies=bool(cfg.get("use_cookies")))
        if raw.stat().st_size < 100 * 1024:
            raise RuntimeError("下载文件过小")

        fname = sanitize_filename(f"{song}+{artist or '未命名'}") + ".m4a"
        dst = out_dir / fname
        say("convert", "转封装…")
        convert_to_m4a(raw, dst, title=song, artist=artist)

        dur = probe_duration(dst)
        if dur <= 0:
            src_kb = raw.stat().st_size // 1024 if raw.exists() else -1
            dst_kb = dst.stat().st_size // 1024 if dst.exists() else -1
            # 保留现场，方便把坏文件捞出来分析
            bad = dst.with_suffix(".bad.m4a")
            try:
                if dst.exists():
                    dst.replace(bad)
            except Exception:
                pass
            raise RuntimeError(
                f"成品无法读取时长（源 {src_kb}KB / 产物 {dst_kb}KB / "
                f"ffmpeg {'在' if FFMPEG and Path(FFMPEG).exists() else '不在'} / "
                f"{_diag_file(dst if dst.exists() else bad)}）")

        append_catalog(out_dir, {
            "song": song, "file": fname, "title": song, "artist": artist,
            "bytes": dst.stat().st_size, "dur": round(dur, 2), "bv": card.bvid,
            "pending_artist": pending,
        })
        if pending:
            reason = f"{reason}；曲师未查到，暂用 UP 主名"
        return SongResult(song, "ok", reason, fname, card.bvid, artist=artist, pending=pending)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        try:
            Path("/sdcard/Music/SongGrab/sg-error.log").open("a", encoding="utf-8").write(
                f"\n===== {song} @ {time.strftime('%H:%M:%S')} =====\n{tb}\n")
        except Exception:
            pass
        return SongResult(song, "fail",
                          f"下载/转换失败：{type(e).__name__}: {e}", bvid=card.bvid)
    finally:
        if raw is not None:
            try:
                raw.unlink(missing_ok=True)
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
    """按 utf-8-sig → utf-8 → gbk → utf-16 依次尝试解码。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        try:
            text = raw.decode(enc)
            if "\ufffd" not in text:
                return text
        except Exception:
            continue
    raise RuntimeError("无法识别文件编码，请另存为 UTF-8")
