"""热更版本号：数值比较 + seed 包内版本校验。"""
from __future__ import annotations

import ast
import io
import json
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_MAIN = REPO / "app_main.py"
WANTED = {"_ver_key", "_fetch_seed", "check_and_update"}


def _updater_ns() -> dict:
    """把 app_main 里的更新器函数抽出来跑（app_main 依赖 Kivy，不能直接 import）。"""
    tree = ast.parse(APP_MAIN.read_text(encoding="utf-8"))
    defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANTED]
    assert {n.name for n in defs} == WANTED, "更新器函数被改名/删除了，测试需同步"
    ns = {
        "json": json,
        "Path": Path,
        "CODE_DIR": Path("/nonexistent"),
        "UPDATE_SEED_URLS": ["https://example.invalid/seed.zip"],
        "UPDATE_VERSION_URLS": ["https://example.invalid/version.json"],
    }
    exec(compile(ast.Module(body=defs, type_ignores=[]), "app_main.py", "exec"), ns)
    return ns


def _seed_bytes(version: str, *, with_version: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("app_main.py", "# fake app_main\n")
        z.writestr("songgrab.kv", "# fake kv\n")
        # _fetch_seed 要求包体 >1000B（防截断），测试包补个填充文件
        z.writestr("assets/fonts/_pad.bin", b"0" * 2048)
        if with_version:
            z.writestr("version.json", json.dumps({"build": version}))
    return buf.getvalue()


def test_ver_key_compares_numerically():
    key = _updater_ns()["_ver_key"]
    seq = ["1.0.0", "1.0.1", "1.0.9", "1.0.10", "1.0.11", "1.1.0"]
    keys = [key(v) for v in seq]
    assert keys == sorted(keys), f"版本序错了：{list(zip(seq, keys, strict=True))}"
    # 旧日期时间戳视为最旧，保证新语义化版本能覆盖老设备
    assert key("20261115-0027") == (0, 0, 0)
    assert key("1.0.0") > key("20261115-0027")
    for bad in ("", "1.0", "1.0.x", "abc", None):
        assert key(bad) == (0, 0, 0)


def test_fetch_seed_rejects_mismatched_version():
    ns = _updater_ns()
    stale = _seed_bytes("0.9.9")
    ns["_http_get"] = lambda url, timeout: stale
    assert ns["_fetch_seed"]("1.0.0") is None, "CDN 旧包必须被拒，不能写新版本号"

    good = _seed_bytes("1.0.0")
    ns["_http_get"] = lambda url, timeout: good
    assert ns["_fetch_seed"]("1.0.0") == good


def test_fetch_seed_accepts_legacy_pack_without_version():
    """过渡期：还没打进 version.json 的旧 seed 仍要能更新。"""
    ns = _updater_ns()
    legacy = _seed_bytes("", with_version=False)
    ns["_http_get"] = lambda url, timeout: legacy
    assert ns["_fetch_seed"]("1.0.0") == legacy


def test_check_and_update_applies_semver_over_timestamp(tmp_path):
    ns = _updater_ns()
    code = tmp_path / "code"
    code.mkdir()
    (code / ".version").write_text("20261115-0027", encoding="utf-8")
    ns["_http_get"] = lambda url, timeout: json.dumps(
        {"build": "1.0.0", "note": "ok"}
    ).encode()
    ns["_fetch_seed"] = lambda want="": _seed_bytes("1.0.0")

    ok, note = ns["check_and_update"](code)
    assert ok is True and note == "ok"
    assert (code / ".version").read_text(encoding="utf-8").strip() == "1.0.0"
    assert (code / "version.json").exists()
    assert not list(tmp_path.glob("sg-upd-*")), "临时目录没清干净"

    ok2, _ = ns["check_and_update"](code)
    assert ok2 is False, "同版本不应重复更新"


def test_check_and_update_rejects_mismatched_seed(tmp_path):
    ns = _updater_ns()
    code = tmp_path / "code"
    code.mkdir()
    (code / ".version").write_text("1.0.0", encoding="utf-8")
    ns["_http_get"] = lambda url, timeout: json.dumps({"build": "1.0.1"}).encode()
    ns["_fetch_seed"] = lambda want="": _seed_bytes("1.0.0")

    ok, note = ns["check_and_update"](code)
    assert ok is False and "不一致" in note
    assert (code / ".version").read_text(encoding="utf-8").strip() == "1.0.0"
    assert not (code / "app_main.py").exists(), "校验失败时不能写入任何文件"