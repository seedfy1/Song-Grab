"""SongGrab Kivy 主程序（Android / 桌面通用）。

结构：
  SongGrabApp          —— 应用入口，持有 Controller 与事件泵
  MainScreen           —— 歌单、设置摘要、结果列表
  SettingsScreen       —— 输出目录 / 播放量 / 优先词
  ResultRow            —— 结果列表的一行
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.text import LabelBase
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.properties import BooleanProperty, ListProperty, NumericProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.modalview import ModalView
from kivy.uix.screenmanager import Screen

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 文件命令通道
# 我（agent）往 sg-cmd.json 里写指令，App 轮询执行，结果写 sg-result.json。
CMD_FILE = Path("/sdcard/dsh/sg-cmd.json")
RES_FILE = Path("/sdcard/dsh/sg-result.json")
STATUS_FILE = Path("/sdcard/dsh/sg-status.json")
sys.path.insert(0, str(HERE / "src"))

import controller as ctl  # noqa: E402
import platform_compat as pc  # noqa: E402
import songgrab_core as core  # noqa: E402


# ------------------------------------------------------------------ 中文字体
def register_cjk() -> str:
    """注册一个含中文的字体；找不到就退回默认（会显示方块）。"""
    candidates = [
        "/system/fonts/NotoSansCJK-Regular.ttc",
        "/system/fonts/NotoSansSC-Regular.otf",
        "/system/fonts/NotoSansHans-Regular.otf",
        "/system/fonts/DroidSansFallback.ttf",
        "/system/fonts/sysfonts.ttf",
        str(HERE / "assets" / "fonts" / "NotoSansSC-Regular.otf"),
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    ]
    for c in candidates:
        if os.path.exists(c):
            try:
                LabelBase.register(name="CJK", fn_regular=c)
                return "CJK"
            except Exception:
                continue
    return "Roboto"


FONT = register_cjk()


# ------------------------------------------------------------------ 权限
def request_basic_permissions() -> None:
    """Android 运行时权限（联网 / 读写外部存储）。"""
    if not pc.IS_ANDROID:
        return
    try:
        from android.permissions import Permission, request_permissions  # type: ignore
        request_permissions([
            Permission.INTERNET,
            Permission.READ_EXTERNAL_STORAGE,
            Permission.WRITE_EXTERNAL_STORAGE,
        ])
    except Exception:
        pass


def open_all_files_settings() -> tuple[bool, str]:
    """跳转到「所有文件访问」授权页（Android 11+ 写 /sdcard 必需）。"""
    if not pc.IS_ANDROID:
        return False, "仅 Android 需要"
    try:
        from jnius import autoclass  # type: ignore
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        Settings = autoclass("android.provider.Settings")
        intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
        intent.setData(Uri.parse("package:" + PythonActivity.mActivity.getPackageName()))
        PythonActivity.mActivity.startActivity(intent)
        return True, "请在设置里允许「所有文件访问」，否则无法写入 Music 目录"
    except Exception as e:
        return False, f"无法跳转授权页：{e}"


# ------------------------------------------------------------------ 组件
class ResultRow(BoxLayout):
    song = StringProperty("")
    status = StringProperty("")
    reason = StringProperty("")
    style = StringProperty("wait")   # wait / ok / fail / skip

    def color_for(self, style: str) -> list[float]:
        return {
            "ok": [0.24, 0.78, 0.45, 1],
            "fail": [0.95, 0.35, 0.35, 1],
            "skip": [0.6, 0.6, 0.65, 1],
        }.get(style, [0.85, 0.85, 0.9, 1])


class SongGrabApp(App):
    # 状态
    font_name = StringProperty(FONT)
    status_text = StringProperty("就绪")
    progress = NumericProperty(0.0)
    running = BooleanProperty(False)
    out_dir = StringProperty("")
    ffmpeg_hint = StringProperty("")
    cookie_hint = StringProperty("")
    failed_songs: list[str] = []
    _cmd_id: str = ""
    results: dict = {}

    def build(self):
        self.title = "肥鱼音乐 SongGrab"
        if not pc.IS_ANDROID:
            # 桌面调试时模拟手机竖屏；Android 上由系统全屏接管
            try:
                from kivy.core.window import Window
                Window.size = (400, 760)
            except Exception:
                pass
        self.ctrl = ctl.Controller()
        request_basic_permissions()
        self.cfg = self.ctrl.load_cfg()
        self.out_dir = self.cfg.get("output_dir") or str(pc.default_output_dir())
        # 这两项都涉及子进程/文件 IO，挪到设置页再检测，启动不为它们等待
        self.ffmpeg_hint = ""
        self.cookie_hint = ""
        root = Builder.load_string("ScreenManager:\n    MainScreen:\n        name: 'main'\n    SettingsScreen:\n        name: 'settings'\n")
        self.sm = root
        Clock.schedule_interval(self._drain, 0.1)
        Clock.schedule_interval(self._watch_cmd, 2.0)
        Clock.schedule_interval(self._write_status, 2.0)
        return root

    # -------------------------------------------------------------- 事件泵
    def _drain(self, _dt):
        for _ in range(24):          # 每帧最多消费 24 条，避免卡顿
            try:
                ev = self.ctrl.events.get_nowait()
            except Exception:
                break
            self._handle(ev)

    def _handle(self, ev: ctl.Event):
        main: MainScreen = self.sm.get_screen("main")
        if ev.kind == "start":
            self.status_text = f"[{ev.index}/{ev.total}] 正在处理 {ev.song}"
            self.progress = (ev.index - 1) / max(1, ev.total)
            main.upsert_row(ev.song, "wait", "排队中…")
        elif ev.kind == "progress":
            self.status_text = f"{ev.song}：{ev.msg}"
            main.upsert_row(ev.song, "wait", ev.msg)
        elif ev.kind == "result":
            res = ev.result
            main.upsert_row(
                res.song,
                res.status,
                f"{res.file or res.reason}",
            )
            self.results[res.song] = {
                "song": res.song, "status": res.status,
                "file": res.file, "reason": res.reason,
                "bvid": getattr(res, "bvid", ""),
            }
            if res.status == "fail":
                if res.song not in self.failed_songs:
                    self.failed_songs.append(res.song)
            self.progress = ev.index / max(1, ev.total)
        elif ev.kind == "log":
            self.status_text = ev.msg
        elif ev.kind == "done":
            s = ev.stats
            self.status_text = f"完成：成功 {s['ok']} / 跳过 {s['skip']} / 失败 {s['fail']}"
            self.progress = 1.0
            self.running = False
            self._notify(f"下载完成：成功{s['ok']} 跳过{s['skip']} 失败{s['fail']}")
        elif ev.kind == "stopped":
            self.status_text = "已停止"
            self.running = False

    def _notify(self, msg: str):
        try:
            if pc.IS_ANDROID:
                from plyer import notification  # type: ignore
                notification.notify(title="SongGrab", message=msg, timeout=5)
        except Exception:
            pass

    # -------------------------------------------------------------- 命令通道
    def _write_json(self, path: Path, obj) -> None:
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            try:
                path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    def _watch_cmd(self, _dt):
        try:
            if not CMD_FILE.exists():
                return
            data = json.loads(CMD_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):      # 顶层不是对象（比如数组）直接忽略
            return
        cid = str(data.get("id") or "")
        if not cid or cid == self._cmd_id:
            return
        self._cmd_id = cid
        action = str(data.get("action") or "")
        ok, msg = True, ""
        try:
            if action == "download":
                raw_songs = data.get("songs")
                if not isinstance(raw_songs, list):
                    ok, msg = False, "songs 必须是数组"
                    raw_songs = []
                songs = [str(x) for x in raw_songs if str(x).strip()]
                if not songs:
                    ok, msg = False, "songs 为空"
                else:
                    if data.get("out"):
                        self.out_dir = str(data["out"])
                    self.start_download("\n".join(songs))
                    msg = f"已开始 {len(songs)} 首"
            elif action == "stop":
                self.stop_download()
                msg = "已请求停止"
            elif action == "open_folder":
                ok, msg = self.ctrl.open_folder(Path(self.out_dir))
            elif action == "clear":
                self.results = {}
                self.sm.get_screen("main").clear_rows()
                msg = "已清空"
            elif action == "echo":
                msg = "pong"
            else:
                ok, msg = False, f"未知动作：{action}"
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {e}"
        self._write_json(RES_FILE, {
            "id": cid, "action": action, "ok": ok,
            "msg": msg, "at": time.strftime("%H:%M:%S"),
        })
        # 标记为已消费：否则 App 重启后 _cmd_id 清空，同一条命令会再跑一遍
        try:
            CMD_FILE.rename(CMD_FILE.with_suffix(".done"))
        except Exception:
            try:
                CMD_FILE.unlink()
            except Exception:
                pass

    def _write_status(self, _dt=None):
        self._write_json(STATUS_FILE, {
            "running": bool(self.running),
            "progress": round(float(self.progress), 3),
            "status": self.status_text,
            "out": self.out_dir,
            "cookie": self.cookie_hint,
            "results": list(self.results.values()),
        })

    # -------------------------------------------------------------- 动作
    def pick_playlist(self):
        start = "/sdcard" if pc.IS_ANDROID else str(Path.home())
        self._picker = PickerView(start, self._on_pick_file)
        self._picker.open()

    def _on_pick_file(self, path: str):
        try:
            songs = self.ctrl.read_playlist(Path(path))
            if not songs:
                self.status_text = "文件里没有解析到歌名"
                return
            self.sm.current = "main"
            self.sm.get_screen("main").ids.song_input.text = "\n".join(songs)
            self.status_text = f"导入 {len(songs)} 首"
        except Exception as e:
            self.status_text = f"导入失败：{e}"

    def try_search(self):
        import threading
        songs = self.ctrl.parse_songs(self.sm.get_screen("main").ids.song_input.text)
        if not songs:
            self.status_text = "先输入歌名"
            return
        song = songs[0]
        self.status_text = f"试搜「{song}」…"
        cfg = dict(self.cfg)

        def _work():
            try:
                msg = self.ctrl.probe(song, cfg)
            except Exception as e:
                msg = f"试搜失败：{e}"
            Clock.schedule_once(lambda dt: setattr(self, "status_text", msg), 0)

        threading.Thread(target=_work, daemon=True).start()

    def run_diag(self):
        import threading as _th
        self.status_text = "诊断中…"

        def _work():
            try:
                msg = core.network_diag()
            except Exception as e:
                msg = f"诊断异常：{e}"
            Clock.schedule_once(lambda dt: self._show_diag(msg), 0)

        _th.Thread(target=_work, daemon=True).start()

    def _show_diag(self, msg: str):
        from kivy.uix.label import Label
        from kivy.uix.scrollview import ScrollView
        from kivy.uix.boxlayout import BoxLayout as _BL
        view = ModalView(size_hint=(0.95, 0.7))
        box = _BL(orientation="vertical", padding=dp(10), spacing=dp(6))
        sv = ScrollView()
        box.add_widget(sv)
        lab = Label(text=msg, font_name=FONT, font_size=sp(11), size_hint_y=None,
                    halign="left", valign="top", text_size=(dp(300), None))
        lab.bind(texture_size=lambda i, v: setattr(i, "height", v[1]))
        sv.add_widget(lab)
        from kivy.uix.button import Button
        box.add_widget(Button(text="关闭", font_name=FONT, size_hint_y=None, height=dp(44),
                              on_release=lambda *_: view.dismiss()))
        view.add_widget(box)
        view.open()
        self.status_text = "诊断完成"

    def open_qr_login(self):
        QRLoginView(self._on_login_done).open()

    def _on_login_done(self, ok: bool, msg: str):
        self.cookie_hint = pc.cookies_hint()
        self.status_text = msg
        core._USER_COOKIES_LOADED = False   # 让下次请求重新加载新 cookie

    def ask_storage_permission(self):
        ok, msg = open_all_files_settings()
        self.status_text = msg

    def ask_add_prio(self):
        PromptView("添加优先词（支持正则）", lambda w: self.sm.get_screen("settings").add_word(w)).open()

    def start_download(self, text: str):
        songs = self.ctrl.parse_songs(text)
        if not songs:
            self.status_text = "没有歌名"
            return
        main: MainScreen = self.sm.get_screen("main")
        main.clear_rows()
        self.failed_songs = []
        cfg = dict(self.cfg)
        cfg["output_dir"] = self.out_dir
        if self.ctrl.start(songs, cfg):
            self.running = True
            self.progress = 0
            self.status_text = f"开始处理 {len(songs)} 首"
        else:
            self.status_text = "已有任务在跑"

    def stop_download(self):
        self.ctrl.stop()
        self.status_text = "正在停止…（当前这首下完就停）"

    def do_open_folder(self):
        ok, msg = self.ctrl.open_folder(Path(self.out_dir))
        self.status_text = msg

    def copy_failed(self):
        if not self.failed_songs:
            self.status_text = "没有失败项"
            return
        Clipboard.copy("\n".join(self.failed_songs))
        self.status_text = f"已复制 {len(self.failed_songs)} 首失败清单"

    def retry_failed(self):
        if not self.failed_songs:
            self.status_text = "没有失败项"
            return
        songs = list(self.failed_songs)
        self.failed_songs = []
        main: MainScreen = self.sm.get_screen("main")
        main.clear_rows()
        cfg = dict(self.cfg)
        cfg["output_dir"] = self.out_dir
        if self.ctrl.start(songs, cfg):
            self.running = True
            self.status_text = f"重试 {len(songs)} 首"


class MainScreen(Screen):
    def rows(self) -> dict:
        if not hasattr(self, "_rows"):
            self._rows = {}
        return self._rows

    def upsert_row(self, song: str, style: str, reason: str):
        box = self.ids.row_box
        rows = self.rows()
        if song in rows:
            row = rows[song]
            row.style = style
            row.reason = reason
            return
        row = ResultRow(song=song, status={"ok": "成功", "fail": "失败", "skip": "跳过"}.get(style, "…"),
                        reason=reason, style=style)
        rows[song] = row
        box.add_widget(row)

    def clear_rows(self):
        self._rows = {}
        self.ids.row_box.clear_widgets()


class SettingsScreen(Screen):
    prio = ListProperty([])

    def on_pre_enter(self):
        app: SongGrabApp = App.get_running_app()  # type: ignore[assignment]
        app.cookie_hint = pc.cookies_hint()
        if not app.ffmpeg_hint:
            app.ffmpeg_hint = "检测中…"
            threading.Thread(target=self._check_ffmpeg, daemon=True).start()

    def _check_ffmpeg(self):
        app: SongGrabApp = App.get_running_app()  # type: ignore[assignment]
        try:
            ok, msg = app.ctrl.ffmpeg_status()
            text = "ffmpeg 就绪" if ok else f"ffmpeg 缺失：{msg}"
        except Exception as e:
            text = f"ffmpeg 检测失败：{e}"
        Clock.schedule_once(lambda dt: setattr(app, "ffmpeg_hint", text), 0)
        self.ids.out_input.text = app.out_dir
        self.ids.play_input.text = str(app.cfg.get("min_play", 10000))
        self.prio = list(app.cfg.get("priority_words") or [])
        self.refresh_prio()

    def refresh_prio(self):
        box = self.ids.prio_box
        box.clear_widgets()
        for i, w in enumerate(self.prio):
            row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(4))
            from kivy.uix.label import Label
            from kivy.uix.button import Button
            row.add_widget(Label(text=w, font_name=FONT, halign="left", valign="middle",
                                 text_size=(None, None)))
            row.add_widget(Button(text="↑", size_hint_x=None, width=dp(40), font_name=FONT,
                                  on_release=lambda *_, i=i: self.move(i, -1)))
            row.add_widget(Button(text="↓", size_hint_x=None, width=dp(40), font_name=FONT,
                                  on_release=lambda *_, i=i: self.move(i, 1)))
            row.add_widget(Button(text="✕", size_hint_x=None, width=dp(40), font_name=FONT,
                                  on_release=lambda *_, i=i: self.delete(i)))
            box.add_widget(row)

    def move(self, i: int, d: int):
        j = i + d
        if 0 <= j < len(self.prio):
            self.prio[i], self.prio[j] = self.prio[j], self.prio[i]
            self.refresh_prio()

    def delete(self, i: int):
        if 0 <= i < len(self.prio):
            self.prio.pop(i)
            self.refresh_prio()

    def add_word(self, word: str):
        word = word.strip()
        if word and word not in self.prio:
            self.prio.append(word)
            self.refresh_prio()

    def reset_words(self):
        self.prio = []
        self.refresh_prio()

    def save(self):
        app: SongGrabApp = App.get_running_app()  # type: ignore[assignment]
        app.out_dir = self.ids.out_input.text.strip() or str(pc.default_output_dir())
        try:
            app.cfg["min_play"] = max(0, int(self.ids.play_input.text.strip() or "10000"))
        except ValueError:
            pass
        app.cfg["output_dir"] = app.out_dir
        app.cfg["priority_words"] = list(self.prio)
        app.cfg["use_cookies"] = False
        app.ctrl.save_cfg(app.cfg)
        app.status_text = "设置已保存"
        app.sm.current = "main"


# ------------------------------------------------------------------ 弹窗
class PromptView(ModalView):
    def __init__(self, title: str, on_ok, **kw):
        super().__init__(size_hint=(0.85, None), height=dp(220), **kw)
        self._on_ok = on_ok
        from kivy.uix.button import Button
        from kivy.uix.label import Label
        from kivy.uix.textinput import TextInput
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        box.add_widget(Label(text=title, font_name=FONT, size_hint_y=None, height=dp(36)))
        self.input = TextInput(multiline=False, font_name=FONT)
        box.add_widget(self.input)
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        row.add_widget(Button(text="取消", font_name=FONT, on_release=lambda *_: self.dismiss()))
        row.add_widget(Button(text="确定", font_name=FONT, on_release=self._ok))
        box.add_widget(row)
        self.add_widget(box)

    def _ok(self, *_):
        self._on_ok(self.input.text)
        self.dismiss()


class QRLoginView(ModalView):
    """B 站扫码登录：二维码走在线接口渲染，轮询拿到 cookie 后落盘。"""

    def __init__(self, on_done, **kw):
        super().__init__(size_hint=(0.92, None), height=dp(500), auto_dismiss=False, **kw)
        from kivy.uix.button import Button
        from kivy.uix.image import AsyncImage
        from kivy.uix.label import Label
        self._on_done = on_done
        self._key = ""
        self._ev = None

        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        box.add_widget(Label(text="扫码登录 B 站", font_name=FONT, font_size=sp(18),
                             size_hint_y=None, height=dp(34)))
        self.img = AsyncImage(source="", size_hint_y=None, height=dp(300))
        box.add_widget(self.img)
        self.status = Label(text="正在获取二维码…", font_name=FONT, font_size=sp(13),
                            size_hint_y=None, height=dp(40), color=(0.8, 0.82, 0.9, 1))
        box.add_widget(self.status)
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        row.add_widget(Button(text="关闭", font_name=FONT,
                              on_release=lambda *_: self.dismiss()))
        row.add_widget(Button(text="刷新二维码", font_name=FONT,
                              on_release=lambda *_: self._reload()))
        box.add_widget(row)
        self.add_widget(box)
        Clock.schedule_once(lambda dt: self._reload(), 0.2)

    def _reload(self, *_):
        if self._ev:
            Clock.unschedule(self._ev)
            self._ev = None
        self.status.text = "正在获取二维码…"
        threading.Thread(target=self._gen_worker, daemon=True).start()

    def _gen_worker(self):
        try:
            res = core.qr_login_generate()
            url, key, err = res if len(res) == 3 else (res[0], res[1], "")
        except Exception as e:
            url, key, err = "", "", f"{type(e).__name__}: {e}"
        Clock.schedule_once(lambda dt: self._on_gen(url, key, err), 0)

    def _on_gen(self, url: str, key: str, err: str = ""):
        if not key:
            self.status.text = f"获取失败：{err}" if err else "获取失败，检查网络"
            self.status.color = (0.95, 0.45, 0.45, 1)
            return
        self._key = key
        q = urllib.parse.quote(url, safe="")
        self.img.source = ("https://api.qrserver.com/v1/create-qr-code/"
                           f"?size=300x300&margin=8&data={q}")
        self.status.text = "用 B 站 App 扫上方二维码"
        self._ev = Clock.schedule_interval(lambda dt: self._poll(), 2.0)

    def _poll(self):
        if not self._key:
            return
        threading.Thread(target=self._poll_worker, daemon=True).start()

    def _poll_worker(self):
        code, msg = core.qr_login_poll(self._key)
        Clock.schedule_once(lambda dt: self._apply(code, msg), 0)

    def _apply(self, code: int, msg: str):
        if code == 0:
            if self._ev:
                Clock.unschedule(self._ev)
                self._ev = None
            self.status.text = msg
            self._on_done(True, msg)
            Clock.schedule_once(lambda dt: self.dismiss(), 1.2)
        elif code == 86090:
            self.status.text = "已扫码，请在手机上点确认"
        elif code == 86038:
            if self._ev:
                Clock.unschedule(self._ev)
                self._ev = None
            self.status.text = "二维码已过期，点「刷新二维码」"
        elif code == -2:
            self.status.text = msg
        else:
            self.status.text = msg or "等待扫码…"

    def on_dismiss(self):
        if self._ev:
            Clock.unschedule(self._ev)
            self._ev = None


class PickerView(ModalView):
    """选 .txt/.m3u 歌单文件。"""

    def __init__(self, start: str, on_pick, **kw):
        super().__init__(size_hint=(0.95, 0.9), **kw)
        self._on_pick = on_pick
        from kivy.uix.button import Button
        box = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        self.fc = FileChooserListView(path=start, filters=["*.txt", "*.m3u", "*.m3u8"])
        box.add_widget(self.fc)
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        row.add_widget(Button(text="取消", font_name=FONT, on_release=lambda *_: self.dismiss()))
        row.add_widget(Button(text="选择", font_name=FONT, on_release=self._pick))
        box.add_widget(row)
        self.add_widget(box)

    def _pick(self, *_):
        if self.fc.selection:
            self._on_pick(self.fc.selection[0])
        self.dismiss()


if __name__ == "__main__":
    SongGrabApp().run()
