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
                     bvid="", artist=""):
        seen.update(song=song, bvid=bvid, artist=artist)
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
