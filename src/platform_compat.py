"""平台差异收口：Android(buildozer/python-for-android) 与桌面共用同一套 API。

core 只依赖这里的函数，不再直接碰 ctypes.windll / explorer / ffmpeg.exe。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# p4a 启动时会设置 ANDROID_ARGUMENT / ANDROID_PRIVATE
IS_ANDROID = "ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")
IS_WIN = os.name == "nt"


# ------------------------------------------------------------------ 路径

def data_dir() -> Path:
    """可写目录：Android 用应用私有目录，桌面用 XDG 数据目录。"""
    if IS_ANDROID:
        try:
            from android.storage import app_storage_path  # type: ignore
            p = Path(app_storage_path())
        except Exception:
            p = Path(os.environ.get("ANDROID_PRIVATE", "/data/data/songgrab/files"))
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        p = Path(base) / "songgrab"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_file() -> Path:
    return data_dir() / "settings.json"


def artists_file() -> Path:
    return data_dir() / "artists.json"


def default_output_dir() -> Path:
    """默认成品目录。Android 直接落到公共音乐目录。"""
    if IS_ANDROID:
        try:
            from android.storage import primary_external_storage_path  # type: ignore
            p = Path(primary_external_storage_path()) / "Music" / "SongGrab"
        except Exception:
            p = Path("/sdcard/Music/SongGrab")
    elif IS_WIN:
        p = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Music" / "SongGrab"
    else:
        p = Path.home() / "Music" / "SongGrab"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def ca_bundle() -> Path | None:
    """CA 证书包。Android 上没有系统证书路径，必须自带一份。"""
    cands = [PROJ_ROOT / "assets" / "cacert.pem"]
    try:
        import certifi  # type: ignore
        cands.append(Path(certifi.where()))
    except Exception:
        pass
    for c in cands:
        try:
            if c.is_file() and c.stat().st_size > 10_000:
                return c
        except Exception:
            continue
    return None


def cookies_file() -> Path | None:
    """用户导入的 cookies.txt。

    Android 上放开多个容易访问的位置，避免要求用户往 app 私有目录塞文件。
    """
    candidates = [data_dir() / "cookies.txt"]
    if IS_ANDROID:
        candidates += [
            Path("/sdcard/Download/cookies.txt"),
            Path("/sdcard/cookies.txt"),
            Path("/storage/emulated/0/Download/cookies.txt"),
            Path("/sdcard/Android/data/org.fishmusic.songgrab/files/cookies.txt"),
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
        except Exception:
            continue
    return None


def cookies_hint() -> str:
    """给 UI 显示当前登录态来源。"""
    cf = cookies_file()
    if cf is None:
        return "未导入登录 cookie（匿名，易限流）"
    try:
        text = cf.read_text(encoding="utf-8", errors="ignore")
        if "SESSDATA" in text:
            return f"已导入登录 cookie：{cf.name}"
        return f"已找到 {cf.name}，但里面没有 SESSDATA（可能没导出登录态）"
    except Exception:
        return f"已找到 {cf.name}"


# ------------------------------------------------------------------ ffmpeg

HERE = Path(__file__).resolve().parent
PROJ_ROOT = HERE.parent


def android_native_lib(name: str = "libffmpeg.so") -> Path | None:
    """APK 的 nativeLibraryDir（唯一允许 exec 的目录，Android 10+ 限制）。"""
    try:
        from jnius import autoclass  # type: ignore
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        info = PythonActivity.mActivity.getApplicationInfo()
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
    """把 APK assets 里的二进制解出来（仅在 Android 上有效）。"""
    try:
        from android import mActivity  # type: ignore
        assets = mActivity.getAssets()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with assets.open(name) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        dest.chmod(0o755)
        return True
    except Exception:
        return False


def ffmpeg_path() -> Path:
    """Android 上从 nativeLibraryDir 取（该目录允许 exec）；桌面上找系统 ffmpeg。"""
    if IS_ANDROID:
        native = android_native_lib()
        if native is not None:
            return native
        cached = data_dir() / "bin" / "ffmpeg"
        vendored = vendor_ffmpeg()
        if vendored is not None:
            try:
                need_copy = (not cached.exists()
                             or cached.stat().st_size != vendored.stat().st_size)
                if need_copy:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(vendored, cached)
                    cached.chmod(0o755)
                return cached
            except Exception:
                pass
        if cached.exists():
            return cached
        for name in ("ffmpeg", "ffmpeg-aarch64"):
            if _extract_asset(name, cached):
                return cached
        found = shutil.which("ffmpeg")
        return Path(found) if found else cached

    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    return PROJ_ROOT / "ffmpeg"


def ffmpeg_ok() -> tuple[bool, str]:
    exe = ffmpeg_path()
    if not exe.exists():
        return False, f"未找到 ffmpeg：{exe}"
    try:
        r = subprocess.run([str(exe), "-version"], capture_output=True, timeout=15)
        if r.returncode == 0:
            first = (r.stdout or b"").decode("utf-8", "ignore").splitlines()[:1]
            return True, first[0] if first else str(exe)
        return False, f"ffmpeg 退出码 {r.returncode}"
    except Exception as e:
        return False, f"ffmpeg 无法执行：{e}"


# ------------------------------------------------------------------ 打开目录

def _intent_open(uri: str, mime: str | None = None) -> None:
    from jnius import autoclass, cast  # type: ignore
    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Intent = autoclass("android.content.Intent")
    Uri = autoclass("android.net.Uri")
    intent = Intent(Intent.ACTION_VIEW)
    intent.setDataAndType(Uri.parse(uri), mime) if mime else intent.setData(Uri.parse(uri))
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    PythonActivity.mActivity.startActivity(intent)


def open_folder(folder: Path, select_file: Path | None = None) -> tuple[bool, str]:
    folder = Path(folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"目录不可用：{e}"

    if IS_ANDROID:
        # Android 11+ 无法直接用 file:// 拉起浏览器；走系统文件管理器的 documents provider
        try:
            _intent_open("content://com.android.externalstorage.documents/root/primary")
            return True, f"已打开文件管理器，请进入 {folder}"
        except Exception:
            pass
        try:
            _intent_open(str(folder))
            return True, f"已打开 {folder}"
        except Exception as e:
            return False, f"无法打开目录（路径：{folder}）：{e}"

    try:
        if IS_WIN:
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return True, f"已打开 {folder}"
    except Exception as e:
        return False, f"打开失败：{e}（路径：{folder}）"
