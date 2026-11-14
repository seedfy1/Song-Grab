"""任务编排层：不依赖任何 UI 框架，Kivy / CLI / 测试都能驱动。

设计要点：
- 下载跑在后台线程，事件通过 queue 交给 UI 主循环消费；
- 停止用 threading.Event，process_song 之间 + 下载前后都能中断；
- 所有平台差异都走 platform_compat；
- 选片/作者/命名全部交给 core（Windows 版同一套 describe_song），
  这里只负责「多P 拆条」「确认态」「事件转发」。
"""
from __future__ import annotations

import queue
import threading
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
    def start(self, songs: list[Any], cfg: dict[str, Any]) -> bool:
        """songs 每项可以是字符串，也可以是已确认音源的 dict：

        {"raw": "歌名+作者", "song": "歌名", "hint": "作者",
         "artist": "确认后的作者", "bvid": "BVxxx",
         "page": 2, "no_artist": True}

        带 bvid 的直接下载，不再搜索（冻结文档 §8.1）；
        page > 1 时文件名带「P{n}」，直链带 ?p={n}（多P 各集互不覆盖）。
        """
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

    def _run(self, songs: list[Any], cfg: dict[str, Any]) -> None:
        total = len(songs)
        ok = skip = fail = confirm = 0
        out_dir = core.resolve_output_dir(cfg)
        workdir = self.workdir()
        cancel = self.stop_flag.is_set

        def stats() -> dict[str, int]:
            return {"ok": ok, "skip": skip, "fail": fail, "confirm": confirm}

        # 续传点：上次停在第几首（raw 行完全对上才认，避免换歌单错位）
        raws = [str(it.get("raw") or it.get("song") or "") if isinstance(it, dict)
                else str(it) for it in songs]
        start = 0
        try:
            rs = core.load_resume()
        except Exception:
            rs = None
        if rs and list(rs.get("songs") or []) == raws:
            idx = int(rs.get("index") or 0)
            if 0 < idx < total:
                start = idx
                self._emit(Event("log", msg=f"从第 {idx + 1} 首接着下（上次停在这里）"))

        try:
            for i, item in enumerate(songs, 1):
                if i - 1 < start:
                    continue
                if self.stop_flag.is_set():
                    self._emit(Event("stopped", stats=stats()))
                    return
                bvid = artist = ""
                no_artist = False
                page = 0
                if isinstance(item, dict):
                    raw = str(item.get("raw") or item.get("song") or "")
                    song = str(item.get("song") or "")
                    hint = str(item.get("hint") or "")
                    bvid = str(item.get("bvid") or "").strip()
                    artist = str(item.get("artist") or "").strip()
                    no_artist = bool(item.get("no_artist"))
                    page = int(item.get("page") or 0)
                else:
                    raw = str(item)
                    song, hint = core.parse_song_entry(raw)
                if not song:
                    song, hint = core.parse_song_entry(raw)
                if not song and bvid:
                    # 粘链接进来的：后台线程取标题洗歌名（不在 UI 线程联网）
                    try:
                        info = core.video_info(bvid)
                    except Exception:
                        info = None
                    song = (core.clean_video_title(info.title) if info else "") or bvid
                if not song:
                    continue

                # 多P：文件名带 P 号（歌名 P2.m4a），避免各集互相覆盖
                name = f"{song} P{page}" if page > 1 else song
                line = f"{name}+{artist}" if artist else name
                bv = f"{bvid}?p={page}" if (bvid and page > 1) else bvid

                self._emit(Event("start", song=name, index=i, total=total))

                def say(kind: str, msg: str, _song=name) -> None:
                    self._emit(Event("progress", song=_song, msg=str(msg)))

                # §8.1：没确认音源就先试搜，未确认的不下载，交给用户点选
                if not bv and cfg.get("require_confirm", True):
                    try:
                        tr = core.trial_song(line, cfg, say, cancel=cancel)
                    except core.Cancelled:
                        self._emit(Event("stopped", stats=stats()))
                        return
                    except Exception as e:
                        tr = {"stage": "failed", "reason": f"试搜失败：{e}"}
                    if tr.get("stage") == "success":
                        bv = tr.get("bvid") or ""
                        artist = artist or tr.get("artist") or ""
                    else:
                        confirm += 1
                        res = core.SongResult(
                            name, "confirm", tr.get("reason") or "待确认",
                            bvid=tr.get("bvid") or "", artist=tr.get("artist") or "",
                            candidates=list(tr.get("candidates") or []))
                        self._emit(Event("result", song=name, result=res,
                                         index=i, total=total))
                        core.save_resume(raws, i, output_dir=str(out_dir))
                        continue

                try:
                    res = core.process_song(
                        line, cfg, workdir, progress=say, cancel=cancel,
                        bvid_override=bv or None, no_artist=no_artist)
                except core.Cancelled:
                    self._emit(Event("stopped", stats=stats()))
                    return
                except Exception as e:  # 兜底，绝不让线程炸掉
                    self._emit(Event("log", msg=f"{name} 异常：{e}"))
                    fail += 1
                    core.save_resume(raws, i, output_dir=str(out_dir))
                    continue

                if res.status == "ok":
                    ok += 1
                elif res.status == "skip":
                    skip += 1
                elif res.status == "stopped":
                    self._emit(Event("stopped", stats=stats()))
                    return
                else:
                    fail += 1
                self._emit(Event("result", song=name, result=res, index=i, total=total))
                core.save_resume(raws, i, output_dir=str(out_dir))

            core.clear_resume()
            self._emit(Event("done", stats=stats(), msg=str(out_dir)))
        except Exception as e:
            self._emit(Event("log", msg=f"任务异常终止：{e}"))
            self._emit(Event("stopped", stats=stats()))

    # ------------------------------------------------------------ 单曲快速检查
    def trial(self, raw: str, cfg: dict[str, Any], bvid: str = "") -> dict[str, Any]:
        """同步跑 §8 试搜链，返回 trial_song 的 dict（三档 + 候选）。"""
        return core.trial_song(raw, cfg, bvid_override=bvid or None)

    def probe(self, raw: str, cfg: dict[str, Any]) -> str:
        """一行说明（兼容旧调用）：把 trial 结果压成文字。"""
        tr = self.trial(raw, cfg)
        if tr.get("stage") == "failed":
            return f"未选中：{tr.get('reason') or ''}"
        c = (tr.get("candidates") or [{}])[0]
        head = "已命中" if tr.get("stage") == "success" else "待确认"
        return (f"{head}：{str(c.get('title', ''))[:40]} · "
                f"{c.get('play', 0)} 播 · {c.get('duration', 0)}s"
                f"（{tr.get('reason') or ''}）")


def consume(ctrl: Controller, handler: Callable[[Event], None], interval: float = 0.1) -> None:
    """给 UI 用的轮询消费（Kivy 侧包 Clock.schedule_interval）。"""
    while True:
        try:
            ev = ctrl.events.get_nowait()
        except queue.Empty:
            return
        handler(ev)
