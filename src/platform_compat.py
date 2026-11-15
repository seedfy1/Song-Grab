"""平台差异收口（Android 优先，桌面仅用于开发期测试）。

core / controller / UI 只依赖这里的函数，不直接碰 jnius、intent、ffmpeg 路径。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ_ROOT = HERE.parent

# p4a 启动时会设置 ANDROID_ARGUMENT / ANDROID_PRIVATE
IS_ANDROID = "ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")


# ------------------------------------------------------------------ 路径

def data_dir() -> Path:
    """可写目录：Android 用应用私有目录，桌面用 XDG 数据目录。"""
    if IS_ANDROID:
        try:
            from android.storage import app_storage_path  # type: ignore
            p = Path(app_storage_path())
        except Exception:
            p = Path(os.environ.get("ANDROID_PRIVATE", "/data/data/org.fishmusic.songgrab/files"))
    else:
        p = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "songgrab"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_file() -> Path:
    return data_dir() / "settings.json"


def artists_file() -> Path:
    return data_dir() / "artists.json"


def default_output_dir() -> Path:
    """默认成品目录：Android 落到公共音乐目录，桌面落到 ~/Music/SongGrab。"""
    if IS_ANDROID:
        try:
            from android.storage import primary_external_storage_path  # type: ignore
            p = Path(primary_external_storage_path()) / "Music" / "SongGrab"
        except Exception:
            p = Path("/sdcard/Music/SongGrab")
    else:
        p = Path.home() / "Music" / "SongGrab"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def ca_bundle() -> Path | None:
    """CA 证书包。Android 上没有系统证书目录，必须自带一份。

    热更目录里没有 APK 的 assets，所以要顺着 private 目录浅递归找 *.pem
    （p4a 各版本分别解到 files/ 或 files/app/）。
    """
    for c in (PROJ_ROOT / "assets" / "cacert.pem", data_dir() / "cacert.pem"):
        if _is_ca(c):
            return c
    try:
        import certifi  # type: ignore
        c = Path(certifi.where())
        if _is_ca(c):
            return c
    except Exception:
        pass
    for root in (PROJ_ROOT, data_dir()):
        for f in _shallow_pems(root):
            if _is_ca(f):
                return f
    if IS_ANDROID:
        dest = data_dir() / "cacert.pem"
        for name in ("cacert.pem", "assets/cacert.pem"):
            if _extract_asset(name, dest) and _is_ca(dest):
                return dest
    return None


def _is_ca(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > 10_000
    except OSError:
        return False


def _shallow_pems(root: Path, max_depth: int = 3):
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath[len(str(root)):].count(os.sep) >= max_depth:
                dirnames[:] = []
            for fn in filenames:
                if fn.endswith(".pem"):
                    yield Path(dirpath) / fn
    except OSError:
        return


def cookie_txt() -> Path:
    """扫码登录/导入之后的 Cookie 落盘位置（必须是 core 与 UI 共用的同一份）。

    FIX(M34)：旧实现有两个落点——core 写 APP_DIR/cookies.txt（热更目录），
    cookies_hint()/cookies_file() 却只认 data_dir()/cookies.txt，结果扫码
    提示「已保存 5 条 Cookie」，设置页却仍显示「未导入登录 cookie」，
    下载也读不到这份 Cookie。统一走应用私有目录。
    """
    return data_dir() / "cookies.txt"


def cookies_file() -> Path | None:
    """用户导入的 cookies.txt（Android 上放开几个容易访问的位置）。"""
    candidates = [cookie_txt()]
    if IS_ANDROID:
        candidates += [
            Path("/sdcard/Download/cookies.txt"),
            Path("/sdcard/cookies.txt"),
            Path("/storage/emulated/0/Download/cookies.txt"),
            Path("/sdcard/Android/data/org.fishmusic.songgrab/files/cookies.txt"),
            # 兼容 0025 及更早版本写在热更目录里的那份（读到就会迁回私有目录）
            Path("/sdcard/Music/SongGrab/code/cookies.txt"),
        ]
    else:
        candidates += [
            Path.home() / "Downloads" / "cookies.txt",
            Path.home() / "cookies.txt",
        ]
    for c in candidates:
        try:
            if c.exists() and c.stat().st_size > 200:
                return c
        except OSError:
            continue
    return None


def cookies_hint() -> str:
    """给 UI 显示当前登录态来源。"""
    cf = cookies_file()
    if cf is None:
        return "未导入登录 cookie（匿名，易限流）"
    try:
        if "SESSDATA" in cf.read_text(encoding="utf-8", errors="ignore"):
            return f"已导入登录 cookie：{cf.name}"
        return f"已找到 {cf.name}，但里面没有 SESSDATA（可能没导出登录态）"
    except OSError:
        return f"已找到 {cf.name}"


# ------------------------------------------------------------------ ffmpeg

def android_native_lib(name: str = "libffmpeg.so") -> Path | None:
    """APK 的 nativeLibraryDir —— Android 10+ 唯一允许 exec 的目录。"""
    try:
        from jnius import autoclass  # type: ignore
        info = autoclass("org.kivy.android.PythonActivity").mActivity.getApplicationInfo()
        p = Path(str(info.nativeLibraryDir)) / name
        return p if p.exists() else None
    except Exception:
        return None


def vendor_ffmpeg() -> Path | None:
    """项目内置的 Android arm64 ffmpeg（随 APK 分发）。"""
    for cand in (
        PROJ_ROOT / "libs" / "arm64-v8a" / "libffmpeg.so",
        PROJ_ROOT / "assets" / "ffmpeg.bin",
        PROJ_ROOT / "assets" / "ffmpeg",
        PROJ_ROOT / "ffmpeg",
    ):
        if cand.exists() and cand.stat().st_size > 1_000_000:
            return cand
    return None


def _extract_asset(name: str, dest: Path) -> bool:
    """把 APK assets 里的文件解出来（仅在 Android 上有效）。"""
    try:
        from android import mActivity  # type: ignore
        dest.parent.mkdir(parents=True, exist_ok=True)
        with mActivity.getAssets().open(name) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        dest.chmod(0o755)
        return True
    except Exception:
        return False


def ffmpeg_path() -> Path:
    """Android 上从 nativeLibraryDir 取（该目录允许 exec）；桌面上找系统 ffmpeg。"""
    if not IS_ANDROID:
        found = shutil.which("ffmpeg")
        return Path(found) if found else PROJ_ROOT / "ffmpeg"

    native = android_native_lib()
    if native is not None:
        return native
    cached = data_dir() / "bin" / "ffmpeg"
    vendored = vendor_ffmpeg()
    if vendored is not None:
        try:
            if not cached.exists() or cached.stat().st_size != vendored.stat().st_size:
                cached.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(vendored, cached)
                cached.chmod(0o755)
        except OSError:
            pass
        if cached.exists():
            return cached
    for name in ("ffmpeg", "ffmpeg-aarch64"):
        if _extract_asset(name, cached):
            return cached
    found = shutil.which("ffmpeg")
    return Path(found) if found else cached


def ffmpeg_ok() -> tuple[bool, str]:
    exe = ffmpeg_path()
    if not exe.exists():
        return False, f"未找到 ffmpeg：{exe}"
    try:
        r = subprocess.run([str(exe), "-version"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"ffmpeg 无法执行：{e}"
    if r.returncode != 0:
        return False, f"ffmpeg 退出码 {r.returncode}"
    head = (r.stdout or b"").decode("utf-8", "ignore").splitlines()[:1]
    return True, head[0] if head else str(exe)


# ------------------------------------------------------------------ 打开目录

def _intent_open(uri: str, mime: str | None = None) -> None:
    from jnius import autoclass  # type: ignore
    Intent = autoclass("android.content.Intent")
    Uri = autoclass("android.net.Uri")
    intent = Intent(Intent.ACTION_VIEW)
    intent.setDataAndType(Uri.parse(uri), mime) if mime else intent.setData(Uri.parse(uri))
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    autoclass("org.kivy.android.PythonActivity").mActivity.startActivity(intent)


def open_folder(folder: Path, select_file: Path | None = None) -> tuple[bool, str]:
    folder = Path(folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"目录不可用：{e}"

    if not IS_ANDROID:
        opener = ["open", str(folder)] if sys.platform == "darwin" else ["xdg-open", str(folder)]
        try:
            subprocess.Popen(opener)
            return True, f"已打开 {folder}"
        except OSError as e:
            return False, f"打开失败：{e}（路径：{folder}）"

    # 1) 直接定位到目标目录（documents provider 的 document URI）
    for prefix in ("/sdcard/", "/storage/emulated/0/"):
        s = str(folder)
        if s.startswith(prefix):
            try:
                import urllib.parse as _up
                enc = _up.quote("primary:" + s[len(prefix):], safe="")
                _intent_open(
                    f"content://com.android.externalstorage.documents/document/{enc}",
                    "vnd.android.document/directory",
                )
                return True, f"已打开 {folder}"
            except Exception:
                break
    # 2) 回退到文件管理器根目录
    for uri in ("content://com.android.externalstorage.documents/root/primary", str(folder)):
        try:
            _intent_open(uri)
            return True, f"已打开 {folder}"
        except Exception:
            continue
    return False, f"无法打开目录（路径：{folder}）"
