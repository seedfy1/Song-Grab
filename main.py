"""启动壳：优先从 /sdcard 加载外部代码，实现「改代码不用重新打包」。

外部目录：/sdcard/dsh/songgrab-live/
  ├── app_main.py    真正的 App
  ├── songgrab.kv    界面布局
  ├── src/           业务代码
  └── assets/        cacert.pem 等

首次启动会把 APK 内置的代码复制过去，之后就一直用外部那份。
改完文件重开 App 即生效，不需要 buildozer。
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import traceback
from pathlib import Path

LIVE = Path("/sdcard/dsh/songgrab-live")
HERE = Path(__file__).resolve().parent


def _copy_builtin_to_live() -> bool:
    """首次运行：把内置代码铺到外部目录。"""
    try:
        LIVE.mkdir(parents=True, exist_ok=True)
        for name in ("app_main.py", "songgrab.kv"):
            src = HERE / name
            if src.exists():
                shutil.copy2(src, LIVE / name)
        for d in ("src", "assets"):
            s, t = HERE / d, LIVE / d
            if s.is_dir():
                t.mkdir(parents=True, exist_ok=True)
                for f in s.iterdir():
                    if f.is_file():
                        shutil.copy2(f, t / f.name)
        return (LIVE / "app_main.py").exists()
    except Exception:
        return False


def _load(path: Path):
    sys.path.insert(0, str(path / "src"))
    sys.path.insert(0, str(path))
    spec = importlib.util.spec_from_file_location("sg_app", path / "app_main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sg_app"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_boot_log(msg: str) -> None:
    try:
        p = Path("/sdcard/dsh/sg-boot.log")
        with p.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def main() -> None:
    _write_boot_log(f"--- 启动 {__import__('time').strftime('%H:%M:%S')} ---")
    # 1) 外部代码
    if not (LIVE / "app_main.py").exists():
        _copy_builtin_to_live()
    if (LIVE / "app_main.py").exists():
        try:
            mod = _load(LIVE)
            _write_boot_log(f"加载外部代码：{LIVE}")
            mod.SongGrabApp().run()
            return
        except Exception:
            _write_boot_log("外部代码失败，回退内置：\n" + traceback.format_exc())
    # 2) 回退内置
    _write_boot_log("使用内置代码")
    import app_main as builtin  # noqa: E402
    builtin.SongGrabApp().run()


if __name__ == "__main__":
    main()
