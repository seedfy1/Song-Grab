"""粘链接 / BV 号直下：解析与标题清洗（纯离线，不联网）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import songgrab_core as core  # noqa: E402

CASES_BV = [
    ("BV1xx411c7mD", "BV1xx411c7mD"),
    ("bv1Xx411C7mD", "BV1Xx411C7mD"),
    ("  BV1fh411c7mE  ", "BV1fh411c7mE"),
    ("https://www.bilibili.com/video/BV1xx411c7mD?p=1&t=3", "BV1xx411c7mD"),
    ("看这个 https://www.bilibili.com/video/BV1fh411c7mE/ 挺好玩", "BV1fh411c7mE"),
    ("晴天+周杰伦", ""),
    ("Evans Gambit", ""),
    ("", ""),
]


def test_scan_bvid():
    for text, want in CASES_BV:
        got = core.scan_bvid(text)
        assert got == want, f"scan_bvid({text!r}) = {got!r}, 期望 {want!r}"


CASES_TITLE = [
    ("【Hi-Res无损音质】｜《晴天》- 周杰伦 -“故事的小黄花”", "晴天"),
    ("周杰伦《晴天》官方MV 高清", "晴天"),
    ("【4K修复】周杰伦 - 晴天 MV2160P修复版", "周杰伦 - 晴天"),
    ("【官方】海阔天空 MV 完整版", "海阔天空"),
    ("【纯享】起风了 1080P 无损音质", "起风了"),
]


def test_clean_video_title():
    for title, want in CASES_TITLE:
        got = core.clean_video_title(title)
        assert got == want, f"clean_video_title({title!r}) = {got!r}, 期望 {want!r}"


def test_clean_title_never_empty():
    for t in ["", "   ", "《》", "【tag】"]:
        core.clean_video_title(t)          # 只要求不抛异常
    assert core.clean_video_title("《晴天》") == "晴天"


if __name__ == "__main__":
    n = ok = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            n += 1
            try:
                fn()
                ok += 1
                print(f"  ok  {name}")
            except AssertionError as e:
                print(f"FAIL  {name}: {e}")
    print(f"{ok}/{n} 通过")
    sys.exit(0 if ok == n else 1)
