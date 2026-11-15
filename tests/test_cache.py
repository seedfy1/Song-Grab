"""内存缓存 `_cache_put` 的并发安全测试。

旧实现无锁：2 路并发下「写入」与「淘汰」交错，可能把刚写入的新项当成
最早写入项 pop 掉（表现为搜索结果忽有忽无）。
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import songgrab_core as core  # noqa: E402


def test_cache_put_evicts_oldest_first():
    c = {}
    for i in range(5):
        core._cache_put(c, f"k{i}", i, 3)
    assert list(c) == ["k2", "k3", "k4"]


def test_cache_put_keeps_new_key_after_eviction():
    c = {}
    core._cache_put(c, "a", 1, 2)
    core._cache_put(c, "b", 2, 2)
    core._cache_put(c, "c", 3, 2)
    assert list(c) == ["b", "c"], "新写入的 c 不能被自己这次淘汰掉"


def test_cache_put_overwrite_is_idempotent():
    c = {}
    core._cache_put(c, "a", 1, 2)
    core._cache_put(c, "a", 2, 2)
    assert c == {"a": 2}
    assert len(c) == 1


def test_cache_put_concurrent_invariants():
    c = {}
    limit = 32
    n_threads, per = 8, 300
    barrier = threading.Barrier(n_threads)
    errors: list[str] = []

    def worker(t: int) -> None:
        try:
            barrier.wait(timeout=10)
            for i in range(per):
                core._cache_put(c, (t, i), i, limit)
                if len(c) > limit:
                    errors.append(f"超过上限：{len(c)}")
        except BaseException as e:      # noqa: BLE001
            errors.append(repr(e))

    ths = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    assert not errors
    assert len(c) <= limit
    valid = {(t, i) for t in range(n_threads) for i in range(per)}
    assert set(c) <= valid, "缓存里出现了从未写入的键"