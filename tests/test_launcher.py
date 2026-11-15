"""启动器回退的回归测试（对应 PROJECT_HANDOFF.md §8.1）。

不能 `import main`：模块级 `try: main()` 会真的加载 app_main → Kivy → 开窗。
这里把 main.py 源码里两个路径字面量替换成临时目录，再以 `__name__="__main__"`
exec；「内置代码」用一个临时的假 app_main 顶住（只 raise 一个哨兵异常）。

关键：**不**往 sys.modules 塞假 app_main。否则即使旧 bug（sys.path 里残留
LIVE 目录，`import app_main` 又命中坏文件）还在，也会被 sys.modules 掩盖。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BUILTIN_APP = '''\
class SongGrabApp:
    def run(self):
        raise RuntimeError("builtin-ran")
'''

BROKEN_APP = 'raise RuntimeError("boom")\n'


def test_launcher_falls_back_and_never_reloads_broken_live(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "app_main.py").write_text(BROKEN_APP, encoding="utf-8")
    log = tmp_path / "boot.log"

    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "app_main.py").write_text(BUILTIN_APP, encoding="utf-8")

    src = (ROOT / "main.py").read_text(encoding="utf-8")
    src = src.replace('Path("/sdcard/Music/SongGrab/code")', f'Path(r"{live}")')
    src = src.replace('LOG_FILE = Path("/sdcard/Music/SongGrab/sg-boot.log")',
                      f'LOG_FILE = Path(r"{log}")')
    assert str(live) in src, "main.py 的 LIVE_CANDIDATES 改过了，测试要跟着调"
    assert str(log) in src, "main.py 的 LOG_FILE 改过了，测试要跟着调"

    saved_modules = {k: sys.modules.get(k) for k in ("app_main", "sg_app")}
    sys.path.insert(0, str(builtin))          # 模拟「内置代码在 import path 上」
    path_before = list(sys.path)              # 以插入后的状态为基准比较
    try:
        g = {"__name__": "__main__", "__file__": str(ROOT / "main.py")}
        exec(compile(src, str(ROOT / "main.py"), "exec"), g)

        text = log.read_text(encoding="utf-8")
        head, sep, tail = text.partition("使用内置代码")
        assert sep, f"没有走到回退分支：\n{text}"
        assert "RuntimeError: boom" in head, "外部代码没被真正执行到"
        assert str(live) not in tail, f"回退之后又加载了坏的外部代码：\n{tail}"
        assert "builtin-ran" in tail, f"回退后没有加载内置代码：\n{tail}"
        assert sys.path == path_before, "外部代码污染的 sys.path 没被恢复"
        assert "sg_app" not in sys.modules, "外部代码模块残留在 sys.modules"
    finally:
        sys.path[:] = path_before
        for k, v in saved_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v