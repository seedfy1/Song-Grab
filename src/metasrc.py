"""§8.2 作者来源：用户 `+` > QQ音乐 > Apple Music > 网易云。

每个源只取返回列表第一条（接口没有播放量字段）。
作者只用于搜索关键词 + 命名，不当换候选锚点（冻结文档 §7 已否决）。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

QQ_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
APPLE_SEARCH = "https://itunes.apple.com/search"
NETEASE_SEARCH = "https://music.163.com/api/search/get"


def _get(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None,
         timeout: int = 12) -> Any:
    try:
        import songgrab_core as core
        opener = core._opener()
    except Exception:
        opener = urllib.request.build_opener()
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with opener.open(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")
    return json.loads(raw)


def _clean(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "").strip())
    return name[:40]


def qq_artist(song: str) -> tuple[str, int, str]:
    try:
        q = urllib.parse.urlencode({"w": song, "format": "json", "n": 1, "t": 0, "p": 1})
        data = _get(f"{QQ_SEARCH}?{q}", headers={"Referer": "https://y.qq.com/"})
        songs = (((data.get("data") or {}).get("song") or {}).get("list") or [])
        if not songs:
            return "", 0, ""
        s = songs[0]
        singers = [str(x.get("name") or "").strip() for x in (s.get("singer") or [])]
        singers = [x for x in singers if x]
        if singers:
            return _clean(singers[0]), int(s.get("interval") or 0), "QQ音乐"
    except Exception:
        pass
    return "", 0, ""


def apple_artist(song: str) -> tuple[str, int, str]:
    try:
        if re.search(r"[\u3040-\u30ff]", song):
            countries = ("jp", "cn")
        elif any(ch in song for ch in "們來個為與會說讓還這點樣聲聽覺見記憶讀寫書畫"):
            countries = ("tw", "hk", "cn")
        else:
            countries = ("cn", "tw")
        for cc in countries:
            q = urllib.parse.urlencode({"term": song, "entity": "song", "limit": 1, "country": cc})
            data = _get(f"{APPLE_SEARCH}?{q}")
            res = data.get("results") or []
            if not res:
                continue
            r = res[0]
            name = _clean(r.get("artistName") or "")
            dur = int((r.get("trackTimeMillis") or 0) // 1000)
            if name:
                return name, dur, f"Apple Music({cc})"
    except Exception:
        pass
    return "", 0, ""


def netease_artist(song: str) -> tuple[str, int, str]:
    try:
        body = urllib.parse.urlencode({"s": song, "type": 1, "limit": 1, "offset": 0}).encode()
        data = _get(NETEASE_SEARCH, data=body, headers={"Referer": "https://music.163.com/"})
        songs = ((data.get("result") or {}).get("songs") or [])[:1]
        if not songs:
            return "", 0, ""
        s = songs[0]
        arts = [str(a.get("name") or "").strip() for a in (s.get("artists") or [])]
        arts = [a for a in arts if a]
        if arts:
            return _clean(arts[0]), int((s.get("duration") or 0) // 1000), "网易云"
    except Exception:
        pass
    return "", 0, ""


def lookup(song: str, user_artist: str = "") -> tuple[str, int, str]:
    song = (song or "").strip()
    if not song:
        return "", 0, ""
    ua = _clean(user_artist)
    if ua:
        for fn in (qq_artist, apple_artist, netease_artist):
            _, dur, src = fn(song)
            if dur:
                return ua, dur, f"用户指定（时长 {src}）"
        return ua, 0, "用户指定"
    for fn in (qq_artist, apple_artist, netease_artist):
        artist, dur, src = fn(song)
        if artist:
            return artist, dur, src
    return "", 0, ""


def artist_ok(artist: str) -> bool:
    a = _clean(artist)
    if len(a) < 2 or len(a) > 20:
        return False
    try:
        import picker as _picker
        if _picker._NOT_ARTIST_RX.search(a) or _picker._FAKE_ARTIST.match(a):
            return False
    except Exception:
        pass
    return True
