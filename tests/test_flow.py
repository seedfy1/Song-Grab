"""流程测试：未确认不下载、确认后按 bvid 直下、文件名/标签、多P、失效回退。

全部离线（打桩），不访问网络。
"""
import sys
import types
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import controller  # noqa: E402
import songgrab_core as core  # noqa: E402

WORK = Path("/tmp/sg-flow-work")
OUT = Path("/tmp/sg-flow-out")


@contextmanager
def patched(**kw):
    old = {k: getattr(core, k) for k in kw}
    for k, v in kw.items():
        setattr(core, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(core, k, v)


def _drain(ctrl, timeout=5.0):
    import time
    evs = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            evs.append(ctrl.events.get(timeout=0.1))
        except Exception:
            if not ctrl.running():
                try:
                    evs.append(ctrl.events.get(timeout=0.1))
                except Exception:
                    pass
                break
    return evs


def _fake_trial(stage="pending"):
    def fn(line, cfg=None, say=None, cancel=None, bvid_override=None, **k):
        if say:
            say("search", f"搜索「{line}」")
        if stage == "pending":
            return {"stage": "pending", "reason": "待确认：多个高分候选",
                    "song": line, "artist": "周杰伦", "artist_source": "qq",
                    "bvid": "BV1confirm", "title": "周杰伦《晴天》",
                    "candidates": [{"bvid": "BV1a", "title": "周杰伦《晴天》",
                                    "author": "UP", "play": 50000, "duration": 269}],
                    "fname": "晴天.m4a", "pool_count": 9}
        return {"stage": "success", "reason": "完全命中", "song": line,
                "artist": "周杰伦", "artist_source": "qq", "bvid": "BV1ok",
                "title": "周杰伦《晴天》", "candidates": [], "fname": "晴天.m4a",
                "pool_count": 9}
    return fn


def _ok_result(song, **kw):
    return core.SongResult(song, "ok", "下完了", "x.m4a", **kw)


# ---------- controller 流程 ----------

def test_unconfirmed_is_skipped():
    called = {"n": 0}

    def fake_process(*a, **k):
        called["n"] += 1
        return _ok_result(a[0])

    with patched(trial_song=_fake_trial("pending"), process_song=fake_process):
        c = controller.Controller()
        cfg = {"output_dir": str(OUT), "require_confirm": True}
        assert c.start(["晴天+周杰伦"], cfg)
        evs = _drain(c)
        res = [e for e in evs if e.kind == "result"][0].result
        assert res.status == "confirm", res.status
        assert res.candidates and res.candidates[0]["bvid"] == "BV1a"
        assert called["n"] == 0, "未确认的曲目不应该进入下载"
        done = [e for e in evs if e.kind == "done"]
        assert done and done[0].stats["confirm"] == 1, done


def test_confirmed_downloads_by_bvid():
    seen = {}

    def fake_process(song, cfg, workdir, progress=None, cancel=None,
                     bvid_override=None, on_progress=None, no_artist=False, **k):
        seen.update(bvid=bvid_override, no_artist=no_artist)
        return _ok_result(song, bvid=bvid_override or "")

    with patched(process_song=fake_process):
        c = controller.Controller()
        cfg = {"output_dir": str(OUT), "require_confirm": True}
        item = {"raw": "晴天+周杰伦", "song": "晴天", "hint": "周杰伦",
                "bvid": "BV1zzz", "artist": "周杰伦"}
        assert c.start([item], cfg)
        evs = _drain(c)
        res = [e for e in evs if e.kind == "result"][0].result
        assert res.status == "ok", res.status
        assert seen["bvid"] == "BV1zzz", seen
        assert seen["no_artist"] is False, seen


def test_require_confirm_off_downloads_anyway():
    called = {"n": 0}

    def fake_process(song, cfg, workdir, progress=None, **k):
        called["n"] += 1
        return _ok_result(song)

    with patched(trial_song=_fake_trial("pending"), process_song=fake_process):
        c = controller.Controller()
        cfg = {"output_dir": str(OUT), "require_confirm": False}
        assert c.start(["晴天"], cfg)
        evs = _drain(c)
        res = [e for e in evs if e.kind == "result"][0].result
        assert res.status == "ok", res.status
        assert called["n"] == 1


def test_no_artist_locks_unnamed():
    seen = {}

    def fake_process(song, cfg, workdir, progress=None, cancel=None,
                     bvid_override=None, on_progress=None, no_artist=False, **k):
        seen.update(no_artist=no_artist, bvid=bvid_override)
        return _ok_result(song)

    with patched(process_song=fake_process):
        c = controller.Controller()
        cfg = {"output_dir": str(OUT), "require_confirm": True}
        item = {"raw": "晴天", "song": "晴天", "hint": "", "bvid": "BV1zzz",
                "artist": "", "no_artist": True}
        assert c.start([item], cfg)
        _drain(c)
        assert seen["no_artist"] is True, seen


# ---------- process_song 行为 ----------

def test_stale_bvid_falls_back_to_trial():
    called = {"trial": 0}

    def boom(_info):
        raise RuntimeError("取不到视频信息（啥都没有）")

    def fake_search(*a, **k):
        called["trial"] += 1
        raise RuntimeError("B站搜索无结果（可能被风控，请稍后重试）")

    with patched(resolve_video=boom, search_both=fake_search,
                 known_good_lookup=lambda *a, **k: None,
                 lookup_artist_priority=lambda *a, **k: ("", ""),
                 lookup_artist_candidates=lambda *a, **k: []):
        res = core.process_song("晴天", {"output_dir": str(OUT)}, WORK,
                                bvid_override="BVdead")
    assert res.status != "ok", res.status
    assert called["trial"] >= 1, called


def _stub_media(seen, name="晴天.m4a"):
    def fake_dl(bvid, workdir, use_cookies=False, cancel=None, page=1,
                on_progress=None, cid=0, **k):
        seen["dl_bvid"] = bvid
        seen["page"] = page
        p = Path(workdir) / f"{bvid}.m4a"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * 2048)
        return p

    def fake_conv(src, dst, title="", artist="", fmt="m4a", cancel=None, **k):
        seen.update(title=title, artist=artist, fmt=fmt, dst=Path(dst).name)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\0" * 4096)
        return Path(dst)

    return fake_dl, fake_conv


def test_filename_is_song_only_and_tag_unknown():
    seen = {}
    fake_dl, fake_conv = _stub_media(seen)
    OUT.mkdir(parents=True, exist_ok=True)
    for p in OUT.glob("*.m4a"):
        p.unlink()
    with patched(download_audio=fake_dl, convert_audio=fake_conv,
                 probe_media=lambda _p: (269.5, 246),
                 audio_ok=lambda _p, expect_dur=0.0: (True, ""),
                 record_download=lambda *a, **k: None,
                 known_good_put=lambda *a, **k: None,
                 load_artists=lambda: {}, save_artist=lambda *a, **k: None,
                 lookup_artist_priority=lambda *a, **k: ("", ""),
                 lookup_artist_candidates=lambda *a, **k: [],
                 resolve_video=lambda _i: {"bvid": "BV1x", "title": "周杰伦《晴天》",
                                           "duration": 269, "author": "UP",
                                           "play": 1000, "pages": 1, "page": 1}):
        res = core.process_song("晴天", {"output_dir": str(OUT)}, WORK,
                                bvid_override="BV1x", no_artist=True)
    assert res.status == "ok", (res.status, res.reason)
    assert seen["artist"] == core.UNNAMED, seen
    assert Path(res.file).name == "晴天.m4a", res.file


def test_multi_page_download_and_filename():
    seen = {}
    fake_dl, fake_conv = _stub_media(seen)
    OUT.mkdir(parents=True, exist_ok=True)
    for p in OUT.glob("*.m4a"):
        p.unlink()
    with patched(download_audio=fake_dl, convert_audio=fake_conv,
                 probe_media=lambda _p: (269.5, 246),
                 audio_ok=lambda _p, expect_dur=0.0: (True, ""),
                 record_download=lambda *a, **k: None,
                 known_good_put=lambda *a, **k: None,
                 load_artists=lambda: {}, save_artist=lambda *a, **k: None,
                 lookup_artist_priority=lambda *a, **k: ("", ""),
                 lookup_artist_candidates=lambda *a, **k: [],
                 resolve_video=lambda _i: {"bvid": "BV1x", "title": "002. 晴天-周杰伦",
                                           "duration": 269, "author": "UP",
                                           "play": 1000, "pages": 100, "page": 2}):
        res = core.process_song("晴天 P2+周杰伦", {"output_dir": str(OUT)}, WORK,
                                bvid_override="BV1x?p=2")
    assert res.status == "ok", (res.status, res.reason)
    assert seen["page"] == 2, seen
    assert Path(res.file).name == "晴天 P2.m4a", res.file


def test_ytdlp_fallback_keeps_page():
    cap = {}

    class FakeYDL:
        def __init__(self, opts):
            cap["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            cap["url"] = url
            raise RuntimeError("stop here")

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = FakeYDL
    old = sys.modules.get("yt_dlp")
    sys.modules["yt_dlp"] = mod
    try:
        try:
            core._download_ytdlp("BV1x", WORK, page=3)
        except Exception:
            pass
        assert "?p=3" in cap.get("url", ""), cap
    finally:
        if old is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = old


def test_video_pages_parses_multi():
    fake = {"code": 0, "data": {"pages": [
        {"page": 1, "part": "001. 稻香", "cid": 111, "duration": 216},
        {"page": 2, "part": "002. 晴天", "cid": 222, "duration": 269},
    ]}}
    with patched(_http_json=lambda *a, **k: fake):
        pages = core.video_pages("BV1q4FTeBEMj")
    assert len(pages) == 2, pages
    assert pages[1]["cid"] == 222 and pages[1]["page"] == 2, pages


# ---------- 选片：翻唱宽容 / 待选回退 ----------

def test_trial_all_covers_is_pending():
    cards = [core.Card(bvid="BV1", title="晴天 翻唱", play=100000, duration=269,
                       author="UP1"),
             core.Card(bvid="BV2", title="晴天 cover 女声", play=50000, duration=269,
                       author="UP2")]
    with patched(_trial_search=lambda kw, say=None: list(cards),
                 known_good_lookup=lambda *a, **k: None,
                 lookup_artist_priority=lambda *a, **k: ("", ""),
                 lookup_artist_candidates=lambda *a, **k: []):
        tr = core.trial_song("晴天", {"min_play": 1, "min_dur": 50, "max_dur": 720})
    assert tr["stage"] == "pending", tr
    assert tr["candidates"], tr


def test_trial_want_cover_not_filtered():
    cards = [core.Card(bvid="BV1", title="晴天 翻唱", play=100000, duration=269,
                       author="UP1")]
    with patched(_trial_search=lambda kw, say=None: list(cards),
                 known_good_lookup=lambda *a, **k: None,
                 lookup_artist_priority=lambda *a, **k: ("", ""),
                 lookup_artist_candidates=lambda *a, **k: []):
        tr = core.trial_song("晴天 翻唱", {"min_play": 1, "min_dur": 50, "max_dur": 720})
    assert tr["stage"] in ("success", "pending"), tr
    assert tr["bvid"] == "BV1", tr


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len([n for n in globals() if n.startswith('test_')]) - fails} passed, {fails} failed")
    sys.exit(1 if fails else 0)
