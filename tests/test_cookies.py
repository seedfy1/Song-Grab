"""Cookie 落点一致性回归测试（M34）。

真机现象：扫码登录弹窗显示「已保存 5 条 Cookie，含 SESSDATA」，设置页
却一直显示「未导入登录 cookie」，下载也拿不到登录态。原因是 core 把
cookies.txt 写到热更目录（APP_DIR），而 cookies_hint()/cookies_file()
只认应用私有目录（data_dir），两边永远对不上。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import platform_compat as pc  # noqa: E402
import songgrab_core as core  # noqa: E402

NETSCAPE = "# Netscape HTTP Cookie File\n" + "".join(
    f".bilibili.com\tTRUE\t/\tFALSE\t0\t{k}\t{v}\n"
    for k, v in (("SESSDATA", "abc%2C123"), ("bili_jct", "deadbeef"), ("buvid3", "x" * 60))
)


def _bind(tmp_path, monkeypatch):
    target = tmp_path / "private" / "cookies.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy = tmp_path / "live" / "cookies.txt"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(core, "COOKIE_TXT", target)
    monkeypatch.setattr(core, "_LEGACY_COOKIE_TXT", legacy)
    return target, legacy


def test_cookie_txt_is_in_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert pc.cookie_txt() == pc.data_dir() / "cookies.txt"


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    target, legacy = _bind(tmp_path, monkeypatch)
    n, sess = core.save_cookie_text(NETSCAPE)
    assert (n, sess) == (3, True)
    assert target.exists(), "必须落在应用私有目录"
    assert not legacy.exists()
    got = core.load_cookies()
    assert got["SESSDATA"] == "abc%2C123" and got["bili_jct"] == "deadbeef"


def test_legacy_cookie_is_read_and_migrated(tmp_path, monkeypatch):
    target, legacy = _bind(tmp_path, monkeypatch)
    legacy.write_text(NETSCAPE, encoding="utf-8")
    got = core.load_cookies()
    assert got["SESSDATA"] == "abc%2C123"
    assert target.exists() and "SESSDATA" in target.read_text(encoding="utf-8"), "旧文件应迁到新落点"


def test_save_falls_back_to_legacy_when_private_dir_unwritable(tmp_path, monkeypatch):
    target, legacy = _bind(tmp_path, monkeypatch)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(core, "COOKIE_TXT", blocker / "cookies.txt")
    n, sess = core.save_cookie_text(NETSCAPE)
    assert (n, sess) == (3, True), "私有目录不可写时不能把登录直接判失败"
    assert "SESSDATA" in legacy.read_text(encoding="utf-8")


def test_imported_cookie_is_visible_to_hint_and_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cf = pc.cookie_txt()
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text(NETSCAPE, encoding="utf-8")
    assert pc.cookies_file() == cf, "hint/loader 必须认 core 写的那个文件"
    assert "已导入登录 cookie" in pc.cookies_hint()
    core._USER_COOKIES_LOADED = False
    assert core._load_user_cookies() is True
    core._USER_COOKIES_LOADED = False
