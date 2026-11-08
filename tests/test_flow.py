"""流程测试：未确认音源不下载、确认后按 bvid 直下（不联网，打桩）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import controller  # noqa: E402
import songgrab_core as core  # noqa: E402


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


def _fake_trial(confirm=True):
    def fn(raw, cfg, progress=None):
        if progress:
            progress("search", f"搜索「{raw}」")
        if confirm:
            return {"stage": "confirm", "reason": "待确认：多个高分候选",
                    "artist": "周杰伦", "source": "QQ音乐", "bvid": "BV1confirm",
                    "title": "周杰伦《晴天》", "ref_dur": 269,
                    "candidates": [{"bvid": "BV1a", "title": "周杰伦《晴天》",
                                    "author": "UP", "play": 50000, "duration": 269}],
                    "_card": None, "_pool": []}
        return {"stage": "ok", "reason": "完全命中", "artist": "周杰伦",
                "source": "QQ音乐", "bvid": "BV1ok", "title": "周杰伦《晴天》",
                "ref_dur": 269, "candidates": [], "_card": None, "_pool": []}
    return fn


def test_unconfirmed_is_skipped():
    called = {"n": 0}

    def fake_process(*a, **k):
        called["n"] += 1
        return core.SongResult(a[0], "ok", "下完了", "x.m4a")

    old_t, old_p = core.trial_song, core.process_song
    core.trial_song, core.process_song = _fake_trial(True), fake_process
    try:
        c = controller.Controller()
        cfg = {"output_dir": "/tmp/sg-flow", "require_confirm": True}
        assert c.start(["晴天+周杰伦"], cfg)
        evs = _drain(c)
        kinds = [e.kind for e in evs]
        assert "result" in kinds, kinds
        res = [e for e in evs if e.kind == "result"][0].result
        assert res.status == "confirm", res.status
        assert res.candidates and res.candidates[0]["bvid"] == "BV1a"
        assert called["n"] == 0, "未确认的曲目不应该进入下载"
        done = [e for e in evs if e.kind == "done"]
        assert done and done[0].stats["confirm"] == 1, done
    finally:
        core.trial_song, core.process_song = old_t, old_p


def test_confirmed_downloads_by_bvid():
    seen = {}

    def fake_process(song, cfg, workdir, progress=None, artist_hint="",
                     bvid="", artist="", no_artist=False, **k):
        seen.update(song=song, bvid=bvid, artist=artist, no_artist=no_artist)
        return core.SongResult(song, "ok", "下完了", "x.m4a", bvid=bvid, artist=artist)

    old_p = core.process_song
    core.process_song = fake_process
    try:
        c = controller.Controller()
        cfg = {"output_dir": "/tmp/sg-flow", "require_confirm": True}
        item = {"raw": "晴天+周杰伦", "song": "晴天", "hint": "周杰伦",
                "bvid": "BV1zzz", "artist": "周杰伦"}
        assert c.start([item], cfg)
        evs = _drain(c)
        res = [e for e in evs if e.kind == "result"][0].result
        assert res.status == "ok", res.status
        assert seen["bvid"] == "BV1zzz", seen
        assert seen["no_artist"] is False, seen
    finally:
        core.process_song = old_p


def test_require_confirm_off_downloads_anyway():
    called = {"n": 0}

    def fake_process(song, cfg, workdir, progress=None, **k):
        called["n"] += 1
        return core.SongResult(song, "ok", "下完了", "x.m4a")

    old_t, old_p = core.trial_song, core.process_song
    core.trial_song, core.process_song = _fake_trial(True), fake_process
    try:
        c = controller.Controller()
        cfg = {"output_dir": "/tmp/sg-flow", "require_confirm": False}
        assert c.start(["晴天"], cfg)
        evs = _drain(c)
        res = [e for e in evs if e.kind == "result"][0].result
        assert res.status == "ok", res.status
        assert called["n"] == 1
    finally:
        core.trial_song, core.process_song = old_t, old_p


def test_no_artist_locks_unnamed():
    """用户在试搜弹窗里把曲师清空（主动标「未命名」）→ 下载阶段不查也不写作者。

    对应冻结文档 §8.6「有作者可改、无作者标未命名」。
    """
    seen = {}

    def fake_process(song, cfg, workdir, progress=None, **k):
        seen.update(k)
        return core.SongResult(song, "ok", "下完了", "晴天.m4a",
                               bvid=k.get("bvid", ""), artist="")

    old_p = core.process_song
    core.process_song = fake_process
    try:
        c = controller.Controller()
        cfg = {"output_dir": "/tmp/sg-flow", "require_confirm": True}
        item = {"raw": "晴天", "song": "晴天", "hint": "", "bvid": "BV1zzz",
                "artist": "", "no_artist": True}
        assert c.start([item], cfg)
        evs = _drain(c)
        res = [e for e in evs if e.kind == "result"][0].result
        assert res.status == "ok", res.status
        assert seen["no_artist"] is True, seen
        assert seen["artist"] == "", seen
    finally:
        core.process_song = old_p


def test_stale_bvid_falls_back_to_trial():
    """记忆里的 bvid 失效（稿件被删）→ 不直接失败，回退重新试搜。"""
    import pathlib as _pl
    called = {}
    old_i, old_t, old_a = core.video_info, core.trial_song, core.ARTISTS_FILE
    core.ARTISTS_FILE = _pl.Path("/tmp/sg-flow/artists.json")
    core.video_info = lambda bvid: None

    def fake_trial(raw, cfg, say=None):
        called["trial"] = raw
        return {"stage": "fail", "reason": "搜索无结果", "bvid": ""}

    core.trial_song = fake_trial
    try:
        res = core.process_song("晴天", {"output_dir": "/tmp/sg-flow-none"},
                                _pl.Path("/tmp/sg-flow-work"), bvid="BVdead")
        assert called.get("trial") == "晴天", called
        assert res.status == "fail", res.status
        assert "搜索无结果" in res.reason, res.reason
    finally:
        core.video_info, core.trial_song, core.ARTISTS_FILE = old_i, old_t, old_a


def test_filename_is_song_only_and_tag_unknown():
    """文件名只写歌名；作者进音频标签；曲师缺失（或用户清空）标签记 Unknown。"""
    import tempfile

    seen = {}
    old = (core.download_audio, core.convert_to_m4a, core.probe_duration,
           core.video_info, core.append_catalog, core.load_artists,
           core.save_artist)

    def fake_dl(bvid, workdir, use_cookies=False, progress=None, **k):
        p = Path(workdir) / "raw.webm"
        Path(workdir).mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * (120 * 1024))
        return p

    def fake_conv(src, dst, title="", artist=""):
        Path(dst).write_bytes(b"\0" * 1024)
        seen["title"] = title
        seen["artist"] = artist
        seen["dst"] = Path(dst).name

    try:
        core.download_audio = fake_dl
        core.convert_to_m4a = fake_conv
        core.probe_duration = lambda _p: 200.0
        core.video_info = lambda bvid: core.Card(bvid, "周杰伦《晴天》", 100, 269, "UP")
        core.append_catalog = lambda *a, **k: None
        core.load_artists = lambda: {}
        core.save_artist = lambda *a, **k: None
        with tempfile.TemporaryDirectory() as d:
            out, work = Path(d) / "out", Path(d) / "work"
            out.mkdir()
            work.mkdir()
            cfg = {"output_dir": str(out)}

            res = core.process_song("晴天", cfg, work, bvid="BV1x",
                                    artist="", no_artist=True)
            assert res.status == "ok", (res.status, res.reason)
            assert seen["dst"] == "晴天.m4a", seen
            assert seen["artist"] == core.UNNAMED, seen
            assert seen["title"] == "晴天", seen
            assert (out / "晴天.m4a").exists()

            # 有作者：文件名仍不带作者，作者只进标签
            res2 = core.process_song("稻香", cfg, work, bvid="BV1y",
                                     artist="周杰伦")
            assert res2.status == "ok", (res2.status, res2.reason)
            assert seen["dst"] == "稻香.m4a", seen
            assert seen["artist"] == "周杰伦", seen

            # 老命名（歌名+作者.m4a）仍当作已存在，不会重复下载
            (out / "发如雪+周杰伦.m4a").write_bytes(b"\0" * 1024)
            res3 = core.process_song("发如雪", cfg, work, bvid="BV1z",
                                     artist="周杰伦")
            assert res3.status == "skip", (res3.status, res3.reason)
            assert res3.file == "发如雪+周杰伦.m4a", res3.file
    finally:
        (core.download_audio, core.convert_to_m4a, core.probe_duration,
         core.video_info, core.append_catalog, core.load_artists,
         core.save_artist) = old

def test_trial_all_covers_is_confirm():
    """全是翻唱时 trial_song 走待确认，并把候选交出来（不能 0 条）。"""
    import songgrab_core as core
    import metasrc

    def fake_search(kw, page=1):
        return [core.Card(bvid="BVcover%d" % i, title="晴天 翻唱版 %d" % i,
                          play=50000 + i, duration=240, author="某UP",
                          from_music=True, typeid=3, videos=1) for i in range(3)]

    o1, o2 = core.search_both, metasrc.lookup
    core.search_both = fake_search
    metasrc.lookup = lambda song, hint="": ("", 0, "")
    try:
        cfg = dict(core.DEFAULT_SETTINGS)
        tr = core.trial_song("晴天", cfg)
        assert tr["stage"] == "confirm", tr
        assert tr["candidates"], tr
        assert tr["bvid"], tr
    finally:
        core.search_both, metasrc.lookup = o1, o2


def test_trial_want_cover_not_filtered():
    """歌名里自己写了「翻唱」→ 不该被判待确认/失败。"""
    import songgrab_core as core
    import metasrc

    def fake_search(kw, page=1):
        return [core.Card(bvid="BVwant1", title="晴天 翻唱版 完整版", play=90000,
                          duration=240, author="某UP", from_music=True,
                          typeid=3, videos=1)]

    o1, o2 = core.search_both, metasrc.lookup
    core.search_both = fake_search
    metasrc.lookup = lambda song, hint="": ("", 0, "")
    try:
        tr = core.trial_song("晴天 翻唱", dict(core.DEFAULT_SETTINGS))
        assert tr["stage"] == "ok", tr
        assert tr["bvid"] == "BVwant1", tr
    finally:
        core.search_both, metasrc.lookup = o1, o2


def test_video_pages_parses_multi():
    """多P视频要能解析出每一集的 cid —— 选「下第几集」全靠它。"""
    fake = {"code": 0, "data": {"bvid": "BVx", "title": "合集", "pages": [
        {"page": 1, "part": "第一首", "cid": 111, "duration": 200},
        {"page": 2, "part": "第二首", "cid": 222, "duration": 210},
    ]}}
    o = core._http_json
    core._http_json = lambda url, **k: fake
    try:
        pages = core.video_pages("BVx")
    finally:
        core._http_json = o
    assert [p["cid"] for p in pages] == [111, 222], pages
    assert pages[1]["part"] == "第二首"
    assert pages[1]["page"] == 2


def test_multi_page_download_and_filename():
    """多P：cid/page 要透传到下载层，文件名要带 P 号（否则各集互相覆盖）。"""
    seen = {}
    tips = []

    def fake_dl(bvid, workdir, use_cookies=False, progress=None, cid=0, page=0):
        seen.update(bvid=bvid, cid=cid, page=page)
        p = Path(workdir) / "raw.m4a"
        Path(workdir).mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * (120 * 1024))
        return p

    def fake_conv(src, dst, title="", artist=""):
        tips.append((dst.name, title, artist))
        dst.parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\0" * (100 * 1024))
        return dst

    o = (core.download_audio, core.convert_to_m4a, core.probe_duration,
         core.video_info, core.append_catalog, core.find_existing,
         core.load_artists, core.save_artist)
    core.download_audio = fake_dl
    core.convert_to_m4a = fake_conv
    core.probe_duration = lambda p: 200.0
    core.video_info = lambda bvid: core.Card(bvid, "合集", 1000, 400, "UP")
    core.append_catalog = lambda *a, **k: None
    core.find_existing = lambda *a, **k: None
    core.load_artists = lambda: {}
    core.save_artist = lambda *a, **k: None
    try:
        res = core.process_song("晴天", {}, Path("/tmp/sg-mp"), bvid="BVx",
                                artist="周杰伦", cid=222, page=2, suffix=" P2")
    finally:
        (core.download_audio, core.convert_to_m4a, core.probe_duration,
         core.video_info, core.append_catalog, core.find_existing,
         core.load_artists, core.save_artist) = o
    assert res.status == "ok", res.reason
    assert res.file == "晴天 P2.m4a", res.file
    assert seen == {"bvid": "BVx", "cid": 222, "page": 2}, seen
    assert tips and tips[0][0] == "晴天 P2.m4a", tips


def test_ytdlp_fallback_keeps_page():
    """直链失败回退 yt-dlp 时要带 ?p=N，否则只会下到 P1。"""
    got = {}

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            got["url"] = url
            return {"id": "BVx", "ext": "m4a"}

    import types
    fake_mod = types.SimpleNamespace(YoutubeDL=FakeYDL)
    o_sys = sys.modules.get("yt_dlp")
    sys.modules["yt_dlp"] = fake_mod
    o_cf = core._pc.cookies_file
    core._pc.cookies_file = lambda: None
    try:
        Path("/tmp/sg-mp2").mkdir(parents=True, exist_ok=True)
        try:
            core._download_ytdlp("BVx", Path("/tmp/sg-mp2"), page=3)
        except Exception:
            pass          # 只要 URL 拼对，落盘失败无所谓
    finally:
        if o_sys is None:
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = o_sys
        core._pc.cookies_file = o_cf
    assert got.get("url", "").endswith("?p=3"), got


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
