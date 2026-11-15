"""下载链路的回归测试：Range 校验 + 流式落盘（全部打桩，不联网）。

这两块以前零覆盖：
- `_fetch_chunk` 直接 `r.read()`：服务器忽略 Range 时会把整首读进内存；
- `_download_dash` 把分块攒在内存里 `b"".join()`：大文件峰值翻倍。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import songgrab_core as core  # noqa: E402

URL = "https://cdn.example.invalid/a.m4s"


class _Resp:
    """最小 urllib 响应替身：read(n) 支持限长，语义与 http.client 一致。"""

    def __init__(self, body: bytes, status: int = 206):
        self._body = body
        self.status = status

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._body)
        out, self._body = self._body[:n], self._body[n:]
        return out

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, resp):
        self.resp = resp
        self.calls: list = []

    def open(self, req, timeout=30):
        self.calls.append(req)
        return self.resp


def _patch_opener(monkeypatch, resp):
    op = _Opener(resp)
    monkeypatch.setattr(core, "_stream_opener", lambda: op)
    return op


# ------------------------------------------------------------ _fetch_chunk
def test_fetch_chunk_ok_and_range_header(monkeypatch):
    body = bytes(range(256)) * 4
    op = _patch_opener(monkeypatch, _Resp(body, 206))
    assert core._fetch_chunk(URL, 0, 1023) == body
    assert op.calls[0].get_header("Range") == "bytes=0-1023"


def test_fetch_chunk_accepts_200_at_offset_zero(monkeypatch):
    """start==0 时回 200 是合法的（服务器可能直接给整份，或只有一块）。"""
    body = b"z" * 100
    _patch_opener(monkeypatch, _Resp(body, 200))
    assert core._fetch_chunk(URL, 0, 99) == body


def test_fetch_chunk_rejects_200_for_later_block_without_retry(monkeypatch):
    op = _patch_opener(monkeypatch, _Resp(b"x" * 1000, 200))
    with pytest.raises(core._RangeIgnored):
        core._fetch_chunk(URL, 100, 199)
    assert len(op.calls) == 1, "确定性错误不该重试（重试等于再下几遍整首）"


def test_fetch_chunk_rejects_overlong_body(monkeypatch):
    op = _patch_opener(monkeypatch, _Resp(b"y" * 150, 206))
    with pytest.raises(core._RangeIgnored):
        core._fetch_chunk(URL, 0, 99)
    assert len(op.calls) == 1


def test_fetch_chunk_rejects_short_body(monkeypatch):
    _patch_opener(monkeypatch, _Resp(b"k" * 50, 206))
    with pytest.raises(IOError):
        core._fetch_chunk(URL, 0, 99, tries=1)


def test_fetch_chunk_retries_transient_error(monkeypatch):
    calls = {"n": 0}
    body = b"r" * 100

    class _Flaky:
        def open(self, req, timeout=30):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("连接被重置")
            return _Resp(body, 206)

    monkeypatch.setattr(core, "_stream_opener", lambda: _Flaky())
    monkeypatch.setattr(core.time, "sleep", lambda *_a, **_k: None)
    assert core._fetch_chunk(URL, 0, 99) == body
    assert calls["n"] == 2


# ----------------------------------------------------------- _download_dash
def _patch_dash(monkeypatch, total, chunk_fn):
    monkeypatch.setattr(core, "dash_audio_urls",
                        lambda bvid, cid=0: ([URL], 30280, "测试流"))
    monkeypatch.setattr(core, "_pick_fast_url", lambda urls: (URL, total))
    ranges = []

    def fake_fetch(url, start, end, tries=3):
        ranges.append((start, end))
        return chunk_fn(start, end)

    monkeypatch.setattr(core, "_fetch_chunk", fake_fetch)
    return ranges


def test_download_dash_writes_chunks_at_right_offsets(monkeypatch, tmp_path):
    total = 1_400_000
    data = b"A" * 700_000 + b"B" * 700_000
    ranges = _patch_dash(monkeypatch, total, lambda s, e: data[s:e + 1])
    dst = tmp_path / "song.m4a"

    got = core._download_dash("BV1test", dst)

    assert got == dst
    assert dst.read_bytes() == data
    assert not dst.with_suffix(".part").exists()
    assert len(ranges) == 2          # 1.4MB / (512KB/块) → 2 块
    assert sorted(ranges) == [(0, 699_999), (700_000, 1_399_999)]


def test_download_dash_single_block_for_small_file(monkeypatch, tmp_path):
    total = 300_000
    data = (bytes(range(256)) * 2000)[:total]
    ranges = _patch_dash(monkeypatch, total, lambda s, e: data[s:e + 1])
    dst = tmp_path / "small.m4a"

    core._download_dash("BV1test", dst)

    assert dst.read_bytes() == data
    assert ranges == [(0, total - 1)]


def test_download_dash_rejects_short_chunk_and_cleans_up(monkeypatch, tmp_path):
    total = 1_400_000
    _patch_dash(monkeypatch, total, lambda s, e: b"C" * (e - s))   # 少 1 字节
    dst = tmp_path / "song.m4a"

    with pytest.raises(RuntimeError, match="长度不符"):
        core._download_dash("BV1test", dst)

    assert not dst.exists()
    assert not dst.with_suffix(".part").exists()


def test_download_dash_propagates_range_ignored(monkeypatch, tmp_path):
    def boom(s, e):
        raise core._RangeIgnored("服务器忽略 Range")

    _patch_dash(monkeypatch, 1_400_000, boom)
    dst = tmp_path / "song.m4a"

    with pytest.raises(core._RangeIgnored):
        core._download_dash("BV1test", dst)

    assert not dst.exists()
    assert not dst.with_suffix(".part").exists()


def test_download_dash_honours_cancel(monkeypatch, tmp_path):
    _patch_dash(monkeypatch, 1_400_000, lambda s, e: b"D" * (e - s + 1))
    dst = tmp_path / "song.m4a"

    with pytest.raises(core.Cancelled):
        core._download_dash("BV1test", dst, cancel=lambda: True)

    assert not dst.with_suffix(".part").exists()