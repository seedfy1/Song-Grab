"""任务编排层：不依赖任何 UI 框架，Kivy / CLI / 测试都能驱动。

设计要点：
- 下载跑在后台线程，事件通过 queue 交给 UI 主循环消费；
- 停止用 threading.Event，process_song 之间 + 下载前后都能中断；
- 所有平台差异都走 platform_compat。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import platform_compat as pc
import songgrab_core as core


@dataclass
class Event:
    kind: str                  # start | progress | result | done | stopped | log
    song: str = ""
    msg: str = ""
    index: int = 0
    total: int = 0
    result: Any = None
    stats: dict[str, int] = field(default_factory=dict)


class Controller:
    def __init__(self) -> None:
        self.events: queue.Queue[Event] = queue.Queue()
        self.stop_flag = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 状态
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stop(self) -> None:
        self.stop_flag.set()

    def workdir(self) -> Path:
        d = pc.data_dir() / "work"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------ 配置
    def load_cfg(self) -> dict[str, Any]:
        return core.load_settings()

    def save_cfg(self, cfg: dict[str, Any]) -> None:
        core.save_settings(cfg)

    def ffmpeg_status(self) -> tuple[bool, str]:
        return pc.ffmpeg_ok()

    def open_folder(self, folder: Path, pick: Path | None = None) -> tuple[bool, str]:
        return pc.open_folder(Path(folder), pick)

    def parse_songs(self, text: str) -> list[str]:
        return core.parse_playlist_text(text)

    def read_playlist(self, path: Path) -> list[str]:
        return core.parse_playlist_text(core.read_text_file(Path(path)))

    def existing(self, out_dir: Path, song: str) -> Path | None:
        return core.find_existing(out_dir, song)

    # ------------------------------------------------------------ 主流程
    def start(self, songs: list[str], cfg: dict[str, Any]) -> bool:
        with self._lock:
            if self.running():
                return False
            self.stop_flag.clear()
            self._thread = threading.Thread(
                target=self._run, args=(list(songs), dict(cfg)), daemon=True
            )
            self._thread.start()
            return True

    def _emit(self, ev: Event) -> None:
        self.events.put(ev)

    def _run(self, songs: list[str], cfg: dict[str, Any]) -> None:
        total = len(songs)
        ok = skip = fail = 0
        try:
            out_dir = core.resolve_output_dir(cfg)
            workdir = self.workdir()
            for i, song in enumerate(songs, 1):
                if self.stop_flag.is_set():
                    self._emit(Event("stopped", stats={"ok": ok, "skip": skip, "fail": fail}))
                    return
                self._emit(Event("start", song=song, index=i, total=total))

                def say(kind: str, msg: str, _song=song) -> None:
                    self._emit(Event("progress", song=_song, msg=str(msg)))

                try:
                    res = core.process_song(song, cfg, workdir, progress=say)
                except Exception as e:  # 兜底，绝不让线程炸掉
                    self._emit(Event("log", msg=f"{song} 异常：{e}"))
                    fail += 1
                    continue

                if res.status == "ok":
                    ok += 1
                elif res.status == "skip":
                    skip += 1
                else:
                    fail += 1
                self._emit(Event("result", song=song, result=res, index=i, total=total))

            self._emit(Event("done", stats={"ok": ok, "skip": skip, "fail": fail},
                             msg=str(out_dir)))
        except Exception as e:
            self._emit(Event("log", msg=f"任务异常终止：{e}"))
            self._emit(Event("stopped", stats={"ok": ok, "skip": skip, "fail": fail}))

    # ------------------------------------------------------------ 单曲快速检查
    def probe(self, song: str, cfg: dict[str, Any]) -> str:
        """同步搜索+选片，只返回一行说明（用于「试搜」按钮）。"""
        cards = core.search_both(song)
        if not cards:
            return "搜索无结果（可能被 B 站限流，稍后重试）"
        pick = core.pick_card(
            cards, song,
            min_play=int(cfg.get("min_play", 10000)),
            min_dur=int(cfg.get("min_dur", 90)),
            priority_words=cfg.get("priority_words", core.DEFAULT_PRIORITY),
        )
        if not pick.card:
            return f"未选中：{pick.reason}"
        c = pick.card
        return f"将选：{c.title[:40]} · {c.play} 播 · {c.duration}s（{pick.reason}）"


def consume(ctrl: Controller, handler: Callable[[Event], None], interval: float = 0.1) -> None:
    """给 UI 用的轮询消费（Kivy 侧包 Clock.schedule_interval）。"""
    while True:
        try:
            ev = ctrl.events.get_nowait()
        except queue.Empty:
            return
        handler(ev)
