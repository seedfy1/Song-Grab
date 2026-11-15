"""二维码本机生成的回归测试（不联网）。"""
import inspect
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import songgrab_core as core  # noqa: E402

SIDE = 420


def _png_side(png: bytes) -> int:
    assert png.startswith(b"\x89PNG\r\n\x1a\n"), "不是 PNG"
    assert png[12:16] == b"IHDR"
    w, h = struct.unpack(">II", png[16:24])
    assert w == h, "二维码必须是正方形"
    assert png.endswith(b"IEND\xaeB`\x82"), "PNG 没有正常收尾"
    return w


def test_qr_png_is_valid_png():
    png = core.qr_png("https://passport.bilibili.com/h5-app/passport/login/scan?k=" + "a" * 32)
    # 实际边长 = 模块数 × 整数缩放，所以只会 ≤ side（不会被拉伸插值）
    assert 0 < _png_side(png) <= SIDE
    assert len(png) > 100, "文件小得像空图（白板）"


def test_qr_png_quiet_zone_default_follows_spec():
    assert inspect.signature(core.qr_png).parameters["quiet"].default == 4
    png = core.qr_png("hello")
    assert _png_side(png) > 0


def test_qr_png_output_is_stable():
    data = "https://example.invalid/scan?k=abc"
    assert core.qr_png(data) == core.qr_png(data)