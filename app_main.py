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
import unicodedata
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
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.modalview import ModalView
from kivy.uix.screenmanager import FadeTransition, Screen

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- 联网更新
# 只更新代码（不换 APK）：比对 version.json，不同就拉 seed.zip 覆盖 code 目录。
UPDATE_VERSION_URLS = [
    "https://raw.githubusercontent.com/seedfy1/Song-Grab/main/version.json",
    "https://cdn.jsdelivr.net/gh/seedfy1/Song-Grab@main/version.json",
]
UPDATE_SEED_URLS = [
    "https://raw.githubusercontent.com/seedfy1/Song-Grab/main/assets/seed.zip",
    "https://cdn.jsdelivr.net/gh/seedfy1/Song-Grab@main/assets/seed.zip",
]
CODE_DIR = Path("/sdcard/Music/SongGrab/code")


def _http_get(url: str, timeout: int = 15) -> bytes:
    import ssl
    import urllib.request
    ca = pc.ca_bundle()
    try:
        ctx = ssl.create_default_context(cafile=str(ca)) if ca else ssl.create_default_context()
    except Exception:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "SongGrab/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def _fetch_seed() -> bytes | None:
    """下载并校验源码包。

    实测 raw.githubusercontent.com 偶尔返回 HTTP 200 但内容被截断
    （8937B vs 32971B），所以必须校验 zip 结构，不能只看长度。
    """
    import io
    import zipfile
    for url in UPDATE_SEED_URLS:
        for _attempt in range(2):
            try:
                data = _http_get(url, 60)
            except Exception:
                continue
            if not data or len(data) < 1000:
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    names = z.namelist()
                    if "app_main.py" not in names or "songgrab.kv" not in names:
                        continue
                    if z.testzip() is not None:
                        continue
                return data
            except Exception:
                continue
    return None


def check_and_update(code_dir: Path = CODE_DIR) -> tuple[bool, str]:
    """检查并应用代码更新。返回 (是否更新, 说明)。"""
    remote = None
    errs: list[str] = []
    for u in UPDATE_VERSION_URLS:
        try:
            remote = json.loads(_http_get(u, 15).decode("utf-8", "ignore"))
            break
        except Exception as e:
            errs.append(f"{u.split('/')[2]}:{type(e).__name__}")
            continue
    if not isinstance(remote, dict):
        return False, ("检查失败 " + "；".join(errs)) if errs else ""
    rv = str(remote.get("build") or "").strip()
    if not rv:
        return False, ""
    vf = code_dir / ".version"
    local = ""
    try:
        if vf.exists():
            local = vf.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    # 版本号形如 YYYYMMDD-HHMM，字典序即时间序：只接受更新的包，
    # 避免 CDN 缓存了旧 version.json / 旧 seed.zip 时把本地代码回滚。
    if rv == local or (local and rv <= local):
        return False, ""

    data = _fetch_seed()
    if not data:
        return False, "下载更新包失败（可稍后重试）"
    # 先解到临时目录，校验通过再替换，避免半个包把现有代码写坏
    import io
    import shutil
    import tempfile
    import zipfile
    tmp = None
    try:
        tmp = Path(tempfile.mkdtemp(prefix="sg-upd-", dir=str(code_dir.parent)))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(tmp)
        if not (tmp / "app_main.py").exists():
            raise RuntimeError("更新包缺少 app_main.py")
        code_dir.mkdir(parents=True, exist_ok=True)
        for item in tmp.iterdir():
            dest = code_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        vf.write_text(rv, encoding="utf-8")
        return True, str(remote.get("note") or rv)
    except Exception as e:
        return False, f"解压失败：{e}"
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


HELP_TEXT = """【第一次使用】

1. 打开时允许存储权限
2. 设置 → 点「授予文件访问权限」→ 开启「所有文件访问」
   不开这个，歌曲下载不下来
3. （可选）设置 → 「扫码登录 B 站」
   登录后音质更高、不容易被限流

【下载歌曲】

粘贴歌名，一行一首 → 点「开始下载」
想指定歌手就在歌名后加「+歌手」
成品位置：手机存储 / Music / SongGrab /

【逐首确认】
点「开始下载」后会一首一首来：先试搜第 1 首，弹窗给你挑，
点「就这首，继续」或「跳过这首」，接着自动试搜第 2 首，
全部过完才开始下载。
弹窗里能改曲师（留空 = Unknown），确认过的音源会记住，下次不再问。

想要翻唱 / 现场 / 钢琴版？歌名里直接写出来（如「晴天 翻唱」），
这类版本就不会被当废片剔掉；实在只剩翻唱，也会给你前三条挑。

【直接按链接下载】

也可以粘 B 站链接或 BV 号，一行一个，认出来就不搜索、
直接下那个视频：
  BV1xx411c7mD
  https://www.bilibili.com/video/BV1xx411c7mD
  https://b23.tv/xxxxxxx     （短链也行）
  av170001                   （av 号也行）
歌名从视频标题里自动洗干净；标题太乱的话，
下完在文件管理器里改个名就行。

【写得更准】

歌名+歌手     例：晴天+周杰伦
             （加号是唯一分隔符，不加就当整首歌名）
             歌名自带空格/破折号不用管：
             Evans Gambit、起风了 - 完整版
冷门歌请把「最低播放量」调低（默认 1 万，建议 3000 或 1000）

【常见失败】

搜索无结果        → 加上歌手名再试
播放量不足 1w     → 去设置里调低门槛
下载/转换失败     → 设置 → 网络诊断，看是哪一环
选了不相干的内容   → 加上歌手名，或在优先级词里指定

【优先级词】

控制选片偏好，越靠上越优先。
例：Hi-res        → 优先录音棚/高音质版本
    (Hi-?res|无损) → 支持正则表达式
    完整版         → 避开片段

【关于登录】

不登录也能用，只是码率低一些、偶尔被限流。
登录只存在本机，换手机要重新扫码。

如果网络诊断三项全部失败：
大概率是没给「所有文件访问」权限，或者手机网络连不上 B 站。
"""

# ---------------------------------------------------------------- 文件命令通道
# 我（agent）往 sg-cmd.json 里写指令，App 轮询执行，结果写 sg-result.json。
WORK_DIR = Path("/sdcard/Music/SongGrab")
CMD_FILE = WORK_DIR / "sg-cmd.json"
RES_FILE = WORK_DIR / "sg-result.json"
STATUS_FILE = WORK_DIR / "sg-status.json"
sys.path.insert(0, str(HERE / "src"))

import controller as ctl  # noqa: E402
import platform_compat as pc  # noqa: E402
import songgrab_core as core  # noqa: E402


# ------------------------------------------------------------------ 中文字体
def register_cjk() -> str:
    """注册一个含中文的字体；找不到就退回默认（会显示方块）。"""
    candidates = [
        str(HERE / "fonts" / "KaiTi-SC-Regular.ttf"),              # 热更下发的字体
        str(HERE / "assets" / "fonts" / "KaiTi-SC-Regular.ttf"),   # APK 内置
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
class SplashScreen(Screen):
    """启动画面：App 起来先显示这个，再淡入主界面。

    Android 12+ 的系统 SplashScreen 会覆盖掉 presplash，所以在 Kivy 里做一份。
    """

    def on_enter(self):
        pass


def _pretty(text: str) -> str:
    """B 站标题爱用数学粗体（𝐇𝐢-𝐑𝐞𝐬），中文字体没有这些字形，显示前归一化。"""
    try:
        return unicodedata.normalize("NFKC", str(text or ""))
    except Exception:
        return str(text or "")


def _song_of_raw(raw: str) -> str:
    try:
        return core.parse_song_entry(raw)[0]
    except Exception:
        return raw


class ResultRow(BoxLayout):
    song = StringProperty("")
    status = StringProperty("")
    reason = StringProperty("")
    style = StringProperty("wait")   # wait / ok / fail / skip / confirm

    def color_for(self, style: str) -> list[float]:
        return {
            "ok": [0.24, 0.78, 0.45, 1],
            "fail": [0.95, 0.35, 0.35, 1],
            "skip": [0.6, 0.6, 0.65, 1],
            "confirm": [0.98, 0.76, 0.25, 1],   # 待确认：琥珀色
        }.get(style, [0.85, 0.85, 0.9, 1])

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            app = App.get_running_app()
            if app is not None and getattr(self, "style", "") == "confirm":
                app.show_candidates(self.song)
                return True
        return super().on_touch_down(touch)


class SongGrabApp(App):
    # 状态
    font_name = StringProperty(FONT)
    status_text = StringProperty("就绪")
    progress = NumericProperty(0.0)
    running = BooleanProperty(False)
    out_dir = StringProperty("")
    ffmpeg_hint = StringProperty("")
    cookie_hint = StringProperty("")
    update_hint = StringProperty("")
    failed_songs: list[str] = []
    # 用户确认过的音源：raw 行 -> {"bvid","artist","title"}
    confirmed: dict[str, dict] = {}
    # 待确认曲目的候选：歌名 -> [候选 dict]
    pending_cands: dict[str, list] = {}
    # 用户手动改过的曲师：raw 行 -> 作者名（空字符串 = 主动标「未命名」）
    trial_artist: dict[str, str] = {}
    # 试搜时曲库给的曲师：raw 行 -> 作者名（用户没改就用它）
    trial_src_artist: dict[str, str] = {}
    _trial_view = None
    # True = 未确认的曲目不自动下载（冻结文档 §8.5「跳过未确定项」）
    require_confirm = BooleanProperty(True)
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
        self._load_confirm()     # §8.6：上次确认过的音源/改过的曲师，重启还在
        request_basic_permissions()
        self.cfg = self.ctrl.load_cfg()
        self.out_dir = self.cfg.get("output_dir") or str(pc.default_output_dir())
        # 这两项都涉及子进程/文件 IO，挪到设置页再检测，启动不为它们等待
        self.ffmpeg_hint = ""
        self.cookie_hint = ""
        root = Builder.load_string(
            "ScreenManager:\n"
            "    SplashScreen:\n        name: 'splash'\n"
            "    MainScreen:\n        name: 'main'\n"
            "    SettingsScreen:\n        name: 'settings'\n")
        self.sm = root
        Clock.schedule_interval(self._drain, 0.1)
        Clock.schedule_interval(self._watch_cmd, 2.0)
        Clock.schedule_interval(self._write_status, 2.0)
        threading.Thread(target=self._auto_update, daemon=True).start()
        # 启动画面停 0.9 秒，然后淡入主界面
        Clock.schedule_once(self._leave_splash, 1.8)
        return root

    def _auto_update(self):
        """启动时检查代码更新。全程写 sg-update.log —— 以前异常被静默吞掉，
        用户只看到"没更新"，却查不出是网络、证书还是解包的问题。"""
        import time as _t
        import traceback
        log = Path("/sdcard/Music/SongGrab/sg-update.log")

        def _w(msg: str) -> None:
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a", encoding="utf-8") as f:
                    f.write(f"[{_t.strftime('%m-%d %H:%M:%S')}] {msg}\n")
            except Exception:
                pass

        try:
            local = ""
            try:
                local = (CODE_DIR / ".version").read_text(encoding="utf-8").strip()
            except Exception:
                pass
            _w(f"检查更新：本地 {local or '(无)'}")
            ok, note = check_and_update()
            _w(f"结果：{'已更新，' + note if ok else '无更新/' + (note or '已是最新')}")
        except Exception as e:
            _w("更新异常：" + type(e).__name__ + ": " + str(e) + "\n"
               + traceback.format_exc())
            return
        if ok:
            Clock.schedule_once(lambda dt: self._notify_updated(note), 0)

    def _notify_updated(self, note: str) -> None:
        """更新完成后给个不容易漏掉的提示（状态栏会被后续事件覆盖）。"""
        self.status_text = f"代码已更新（{note}），重开 App 生效"
        try:
            PromptView("代码已更新，重开 App 后生效", lambda: None).open()
        except Exception:
            pass

    def _leave_splash(self, _dt):
        try:
            self.sm.transition = FadeTransition(duration=0.45)
            self.sm.current = "main"
        except Exception:
            pass

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
            if getattr(res, "status", "") == "confirm":
                self.pending_cands[res.song] = list(getattr(res, "candidates", []) or [])
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
                tr = self.ctrl.trial(song, cfg)
            except Exception as e:
                tr = {"stage": "fail", "reason": f"试搜失败：{e}", "candidates": []}
            Clock.schedule_once(lambda dt: self._show_trial(song, tr), 0)

        threading.Thread(target=_work, daemon=True).start()

    def _load_confirm(self):
        """把 confirm.json 读进三张表（confirmed / trial_artist / trial_src_artist）。"""
        rows = core.load_confirm()
        self.confirmed = {}
        self.trial_artist = {}
        self.trial_src_artist = {}
        for raw, r in rows.items():
            art = str(r.get("artist") or "")
            bvid = str(r.get("bvid") or "")
            if bvid or art:
                self.confirmed[raw] = {"bvid": bvid, "artist": art,
                                       "title": str(r.get("title") or "")}
            if r.get("lock"):
                self.trial_artist[raw] = art
            if r.get("src_artist"):
                self.trial_src_artist[raw] = str(r["src_artist"])
        if self.confirmed:
            self.status_text = f"已记住 {len(self.confirmed)} 首上次确认的音源"

    def _save_confirm(self):
        """三张表合并写回 confirm.json（云端不存，纯本地状态）。"""
        import time as _t
        now = _t.time()
        rows = core.load_confirm()
        raws = (set(rows) | set(self.confirmed)
                | set(self.trial_artist) | set(self.trial_src_artist))
        for raw in raws:
            rec = dict(rows.get(raw) or {})
            got = self.confirmed.get(raw) or {}
            if got:
                rec.update(bvid=got.get("bvid", ""), artist=got.get("artist", ""),
                           title=got.get("title", ""))
            if raw in self.trial_artist:
                rec["artist"] = self.trial_artist[raw]
                rec["lock"] = True
            if raw in self.trial_src_artist:
                rec["src_artist"] = self.trial_src_artist[raw]
            rec["t"] = now
            rows[raw] = rec
        core.save_confirm(rows)

    def check_update_now(self):
        """设置页按钮：立刻检查代码更新（和启动时那次同一套逻辑）。"""
        if self.update_hint.startswith("正在"):
            return
        self.update_hint = "正在检查更新…"

        def work() -> None:
            ok, note, err = False, "", ""
            try:
                ok, note = check_and_update()
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            Clock.schedule_once(lambda dt: self._update_done(ok, note, err), 0)

        threading.Thread(target=work, daemon=True).start()

    def _update_done(self, ok: bool, note: str, err: str) -> None:
        local = ""
        try:
            local = (CODE_DIR / ".version").read_text(encoding="utf-8").strip()
        except Exception:
            pass
        if err:
            self.update_hint = f"检查失败：{err}"
        elif ok:
            self.update_hint = f"已更新到 {local}，重开 App 生效"
            self._notify_updated(note)
        else:
            self.update_hint = f"已是最新版本（{local or '未知'}）"

    def forget_confirm(self):
        """设置页按钮：清掉 confirm.json，下次重新试搜。"""
        self.confirmed = {}
        self.trial_artist = {}
        self.trial_src_artist = {}
        core.clear_confirm()
        self.status_text = "已忘掉记住的音源，下次会重新试搜"

    def _artist_row(self, raw: str, tr: dict):
        """曲师行：点一下就能改（冻结文档 §8.6「有作者可改」）。"""
        from kivy.uix.button import Button
        art = self.trial_artist.get(raw, tr.get("artist", ""))
        src = tr.get("source", "")
        if raw in self.trial_artist:
            src = "手动"
        text = (f"曲师：{art}（{src}）     改" if art
                else f"曲师：{core.UNNAMED}     点此填写")
        b = Button(text=text, font_name=FONT, font_size=sp(12),
                   size_hint_y=None, height=dp(32), halign="left",
                   background_color=(0.20, 0.26, 0.38, 1))
        b.bind(on_release=lambda *_: self._ask_artist(raw, tr))
        return b

    def _ask_artist(self, raw: str, tr: dict):
        cur = self.trial_artist.get(raw, tr.get("artist", ""))

        def ok(v):
            v = (v or "").strip()
            self.trial_artist[raw] = v
            if self._trial_view is not None:
                self._trial_view.dismiss()
                self._trial_view = None
            self.status_text = (
                f"{core.parse_song_entry(raw)[0]}：曲师 {v or core.UNNAMED}")
            self._save_confirm()
            self._show_trial(raw, dict(tr, artist=v, source="手动" if v else ""))

        PromptView(
            f"改曲师（留空 = {core.UNNAMED}）\n当前：{cur or core.UNNAMED}", ok).open()

    def _show_trial(self, raw: str, tr: dict, cb: dict | None = None):
        """§8.5 三档结果框：完全命中 / 待确认（带候选卡）/ 失败原因。

        cb 不为空 = 逐首确认模式：
          {"pick": fn(cand), "skip": fn(), "stop": fn(), "n": 序号, "m": 总数}
        """
        self.trial_src_artist[raw] = tr.get("artist", "")
        from kivy.uix.button import Button
        stage = tr.get("stage", "fail")
        head = {"ok": "【完全命中】", "confirm": "【待确认】",
                "fail": "【没有选中】"}.get(stage, "【待确认】")
        self.status_text = f"{head}：{tr.get('title') or tr.get('reason') or ''}"[:60]

        from kivy.uix.scrollview import ScrollView
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(6))
        view = ModalView(size_hint=(0.94, 0.8))

        def lab(text, color=(0.86, 0.88, 0.94, 1), size=12, h=None):
            w = Label(text=text, font_name=FONT, font_size=sp(size), halign="left",
                      valign="top", color=color, size_hint_y=None, height=h or dp(22))
            w.bind(width=lambda i, v: setattr(i, "text_size", (v - dp(8), None)))
            return w

        self._trial_view = view
        head_color = {"ok": (0.45, 0.86, 0.55, 1), "confirm": (0.95, 0.78, 0.35, 1),
                      "fail": (0.95, 0.50, 0.50, 1)}.get(stage, (0.9, 0.9, 0.9, 1))
        if cb:
            head = f"{head}  第 {cb.get('n', 1)}/{cb.get('m', 1)} 首"
        box.add_widget(lab(head, size=16, h=dp(30), color=head_color))
        box.add_widget(self._artist_row(raw, tr))
        box.add_widget(lab(f"原因：{tr.get('reason', '')}", size=11))

        sv = ScrollView()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        inner.bind(minimum_height=inner.setter("height"))
        cands = tr.get("candidates") or []
        if not cands:
            inner.add_widget(lab("没有候选", size=11))
        for c in cands:
            row = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(6))
            row.add_widget(lab(
                f"{_pretty(c.get('title', ''))[:40]}\n"
                f"{c.get('play', 0)} 播 · {c.get('duration', 0)}s · {c.get('author', '')[:12]}",
                size=10, h=dp(52)))
            row.add_widget(Button(
                text="用这首", font_name=FONT, font_size=sp(11),
                size_hint_x=None, width=dp(74),
                on_release=lambda *_ , c=c: self._trial_choose(raw, c, cb, view)))
            inner.add_widget(row)
        sv.add_widget(inner)
        box.add_widget(sv)

        btns = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        if cb:
            btns.add_widget(Button(
                text="跳过这首", font_name=FONT,
                on_release=lambda *_: (view.dismiss(), cb["skip"]())))
            if tr.get("bvid"):
                btns.add_widget(Button(
                    text="就这首，继续", font_name=FONT,
                    on_release=lambda *_ , t=tr: (view.dismiss(), cb["pick"]({
                        "bvid": t["bvid"], "artist": t.get("artist", ""),
                        "title": t.get("title", "")}))))
            btns.add_widget(Button(
                text="不确认了", font_name=FONT,
                on_release=lambda *_: (view.dismiss(), cb["stop"]())))
        else:
            btns.add_widget(Button(text="关闭", font_name=FONT,
                                   on_release=lambda *_: view.dismiss()))
            if tr.get("bvid"):
                btns.add_widget(Button(
                    text="下载这首", font_name=FONT,
                    on_release=lambda *_ , t=tr: (self._confirm_source(
                        raw, {"bvid": t["bvid"], "artist": t.get("artist", ""),
                              "title": t.get("title", "")}), view.dismiss())))
        box.add_widget(btns)
        view.add_widget(box)
        view.open()

    def _trial_choose(self, raw: str, cand: dict, cb: dict | None, view) -> None:
        """候选行「用这首」。逐首确认模式走回调，否则按老路写进 confirmed。"""
        view.dismiss()
        if self._trial_view is view:
            self._trial_view = None
        if cb:
            self.trial_src_artist[raw] = cand.get("artist", "") or self.trial_src_artist.get(raw, "")
            cb["pick"](cand)
        else:
            self._confirm_source(raw, cand)

    def _confirm_source(self, raw: str, cand: dict):
        """记住用户选定的音源，并把该曲目加入「待下载」。"""
        if raw in self.trial_artist:
            art = self.trial_artist[raw]
        else:
            art = cand.get("artist") or self.trial_src_artist.get(raw, "")
        self.confirmed[raw] = {
            "bvid": cand.get("bvid", ""),
            "artist": art,
            "title": cand.get("title", ""),
        }
        self._save_confirm()
        self.status_text = f"已选定：{cand.get('title', '')[:30]}"

    def show_candidates(self, song: str):
        """点「待确认」行：列出候选让用户点选。"""
        cands = self.pending_cands.get(song) or []
        if not cands:
            self.status_text = f"{song}：没有可选的候选"
            return
        raw = song
        for k in list(self.confirmed) + list(self.pending_cands):
            if _song_of_raw(k) == song:
                raw = k
                break
        self._show_trial(raw, {"stage": "confirm", "reason": "点「用这首」设为已确认音源",
                               "candidates": cands,
                               "artist": self.trial_artist.get(raw, "")})

    def toggle_require_confirm(self):
        self.require_confirm = not self.require_confirm
        self.status_text = ("未确认的曲目不下载（已确认的才下）"
                            if self.require_confirm else "未确认的曲目按加权第一直接下载")

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

    def show_help(self):
        from kivy.uix.scrollview import ScrollView
        from kivy.uix.button import Button
        view = ModalView(size_hint=(0.95, 0.88))
        box = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(8))
        scroll = ScrollView()
        lab = Label(text=HELP_TEXT, font_name=FONT, font_size=sp(12),
                    size_hint_y=None, halign="left", valign="top",
                    text_size=(dp(320), None), color=(0.86, 0.88, 0.94, 1))
        lab.bind(texture_size=lambda i, v: setattr(i, "height", v[1]))
        scroll.add_widget(lab)
        box.add_widget(scroll)
        box.add_widget(Button(text="知道了", font_name=FONT, size_hint_y=None,
                              height=dp(46), on_release=lambda *_: view.dismiss()))
        view.add_widget(box)
        view.open()

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

    def _items_from_text(self, text: str) -> list:
        items: list = []
        for raw in self.ctrl.parse_songs(text):
            got = self.confirmed.get(raw) or {}
            # 粘链接或 BV 号 → 直接按那个视频下，不走搜索
            # 这里不联网（否则点「开始下载」会卡住），歌名由后台线程取标题洗出来
            bvid = core.scan_bvid(raw) or got.get("bvid", "")
            if bvid and not got.get("bvid"):
                items.append({"raw": raw, "song": "", "hint": "",
                              "bvid": bvid, "artist": "", "no_artist": False})
                continue
            song, hint = core.parse_song_entry(raw)
            # 用户主动标「未命名」（改曲师时留空）→ 下载阶段不查也不写作者
            lock = raw in self.trial_artist and not self.trial_artist[raw]
            items.append({"raw": raw, "song": song, "hint": hint,
                          "bvid": got.get("bvid", ""),
                          "artist": got.get("artist", ""),
                          "no_artist": bool(lock)})
        return items

    def start_download(self, text: str):
        items = self._items_from_text(text)
        if not items:
            self.status_text = "没有歌名"
            return
        self.failed_songs = []
        self.pending_cands = {}
        if self.require_confirm:
            # 逐首试搜 → 弹窗确认 → 下一首；确认完才批量下载
            self._begin_confirm_flow(items)
            return
        self._run_items(items)

    def _run_items(self, items: list) -> None:
        """启动下载；先问多P视频要下哪一集（P1/P2/全部）。"""
        self._pages_items = list(items)
        self._pages_ok = []
        Clock.schedule_once(lambda dt: self._next_pages(), 0)

    def _start_run(self, items: list) -> None:
        main: MainScreen = self.sm.get_screen("main")
        main.clear_rows()
        cfg = dict(self.cfg)
        cfg["output_dir"] = self.out_dir
        cfg["require_confirm"] = False          # 人工确认已经做完了
        if self.ctrl.start(items, cfg):
            self.running = True
            self.progress = 0
            self.status_text = f"开始处理 {len(items)} 首"
        else:
            self.status_text = "已有任务在跑"

    # ---------- 多P视频：选下哪一集 ----------
    def _next_pages(self) -> None:
        if getattr(self, "_pages_cancel", False):
            self._pages_cancel = False
            return
        todo = getattr(self, "_pages_items", None)
        if not todo:
            items = getattr(self, "_pages_ok", None) or []
            if not items:
                self.status_text = "没有要下载的曲目"
                return
            self._start_run(items)
            return
        it = todo.pop(0)
        bvid = str(it.get("bvid") or "").strip()
        # 已经指定过分P（cid 有值）或本来就不知道 bvid，直接放行
        if not bvid or it.get("cid"):
            self._pages_ok.append(it)
            Clock.schedule_once(lambda dt: self._next_pages(), 0)
            return

        def work() -> None:
            try:
                pages = core.video_pages(bvid)
            except Exception:
                pages = []
            Clock.schedule_once(lambda dt: self._ask_pages(it, pages), 0)

        threading.Thread(target=work, daemon=True).start()

    def _ask_pages(self, it: dict, pages: list) -> None:
        """多P弹窗：每集一行「下这集」，底部「全部下载 / 跳过这首」。"""
        if len(pages) <= 1:
            self._pages_ok.append(it)
            self._next_pages()
            return

        song = str(it.get("song") or it.get("raw") or "")
        view = ModalView(size_hint=(0.94, 0.9), background_color=(0.07, 0.08, 0.1, 1))
        box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(6))
        def lab(text, color=(0.86, 0.88, 0.94, 1), size=12, h=None):
            w = Label(text=text, font_name=FONT, font_size=sp(size), halign="left",
                      valign="top", color=color, size_hint_y=None, height=h or dp(22))
            w.bind(width=lambda i, v: setattr(i, "text_size", (v - dp(8), None)))
            return w

        box.add_widget(lab(f"【这个视频有 {len(pages)} 集】", size=16, h=dp(28),
                           color=(0.95, 0.78, 0.35, 1)))
        box.add_widget(lab(song or "（未命名）", size=11, h=dp(22)))

        sv = ScrollView()
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        inner.bind(minimum_height=inner.setter("height"))
        acted = [False]

        def take(pgs: list) -> None:
            acted[0] = True
            view.dismiss()
            for pg in pgs:
                n = int(pg.get("page") or 1)
                new = dict(it)
                new["cid"] = int(pg.get("cid") or 0)
                new["page"] = n
                new["suffix"] = f" P{n}"
                new["_no_pages"] = True
                self._pages_ok.append(new)
            self.status_text = (f"已选 {len(pgs)} 集" if len(pgs) > 1
                               else f"已选 P{pgs[0].get('page')}")
            self._next_pages()

        for pg in pages:
            n = int(pg.get("page") or 1)
            part = str(pg.get("part") or "").strip()
            dur = int(pg.get("duration") or 0)
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
            row.add_widget(lab(f"P{n}  {part[:20]}\n{dur}s", size=10, h=dp(46)))
            row.add_widget(Button(
                text="下这集", font_name=FONT, font_size=sp(11),
                size_hint_x=None, width=dp(68),
                on_release=lambda *_, pg=pg: take([pg])))
            inner.add_widget(row)
        sv.add_widget(inner)
        box.add_widget(sv)

        def take_all(*_a) -> None:
            view.dismiss()
            acted[0] = True
            for pg in pages:
                n = int(pg.get("page") or 1)
                new = dict(it)
                new["cid"] = int(pg.get("cid") or 0)
                new["page"] = n
                new["suffix"] = f" P{n}"
                new["_no_pages"] = True
                self._pages_ok.append(new)
            self.status_text = f"已选全部 {len(pages)} 集"
            self._next_pages()

        def skip_one(*_a) -> None:
            view.dismiss()
            acted[0] = True
            self._next_pages()

        btns = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        btns.add_widget(Button(
            text="全部下载", font_name=FONT, font_size=sp(12),
            on_release=take_all))
        btns.add_widget(Button(
            text="跳过这首", font_name=FONT, font_size=sp(12),
            on_release=skip_one))
        box.add_widget(btns)

        def on_dismiss(*_a) -> None:
            if self._pages_view is view:
                self._pages_view = None
            if not acted[0]:            # 按系统返回键 = 跳过这首
                acted[0] = True
                self._next_pages()

        self._pages_view = view
        view.bind(on_dismiss=on_dismiss)
        view.add_widget(box)
        view.open()

    # ---------- 逐首确认（require_confirm=True 时走这条） ----------
    def _begin_confirm_flow(self, items: list) -> None:
        self._flow_items = list(items)
        self._flow_ok: list = []
        self._flow_idx = 0
        self._flow_stop = False
        self.status_text = f"逐首确认：共 {len(items)} 首，先试搜第 1 首"
        Clock.schedule_once(lambda dt: self._next_trial(), 0)

    def _next_trial(self) -> None:
        if self._flow_stop:
            self._finish_confirm_flow()
            return
        if self._flow_idx >= len(self._flow_items):
            self._finish_confirm_flow()
            return
        item = self._flow_items[self._flow_idx]
        raw = item["raw"]
        got = self.confirmed.get(raw) or {}
        # 已经确认过的（上次记住的音源 / 粘的链接）不再试搜
        if got.get("bvid") or item.get("bvid"):
            self._flow_ok.append(dict(item,
                                      bvid=item.get("bvid") or got.get("bvid", ""),
                                      artist=item.get("artist") or got.get("artist", "")))
            self._flow_idx += 1
            Clock.schedule_once(lambda dt: self._next_trial(), 0)
            return
        song = item.get("song") or core.parse_song_entry(raw)[0] or raw
        self.status_text = (f"试搜 {self._flow_idx + 1}/{len(self._flow_items)}：{song}"
                            "（确认后继续下一首）")
        cfg = dict(self.cfg)
        cfg["output_dir"] = self.out_dir

        def work():
            try:
                tr = core.trial_song(raw, cfg)
            except Exception as e:
                tr = {"stage": "fail", "reason": f"试搜失败：{e}", "song": song}
            Clock.schedule_once(lambda dt: self._ask_one(item, tr), 0)

        threading.Thread(target=work, daemon=True).start()

    def _ask_one(self, item: dict, tr: dict) -> None:
        """弹窗让用户确认这一首；确认/跳过后自动试搜下一首。"""
        raw = item["raw"]

        def _next() -> None:
            self._flow_idx += 1
            Clock.schedule_once(lambda dt: self._next_trial(), 0)

        def pick(cand: dict) -> None:
            art = self.trial_artist.get(raw, None)
            if art is None:
                art = cand.get("artist") or self.trial_src_artist.get(raw, "")
            self.confirmed[raw] = {"bvid": cand.get("bvid", ""), "artist": art,
                                   "title": cand.get("title", "")}
            self._save_confirm()
            self._flow_ok.append(dict(item, bvid=str(cand.get("bvid", "")),
                                      artist=str(art or "")))
            _next()

        def skip() -> None:
            _next()

        def stop() -> None:
            self._flow_stop = True
            Clock.schedule_once(lambda dt: self._finish_confirm_flow(), 0)

        self._show_trial(raw, tr, cb={"pick": pick, "skip": skip, "stop": stop,
                                      "n": self._flow_idx + 1,
                                      "m": len(self._flow_items)})

    def _finish_confirm_flow(self) -> None:
        items, self._flow_ok = self._flow_ok, []
        n_all = len(self._flow_items or [])
        if not items:
            self.status_text = f"没有确认的曲目（共 {n_all} 首，全部跳过）"
            return
        self.status_text = f"已确认 {len(items)}/{n_all} 首，开始下载"
        self._run_items(items)

    def stop_download(self):
        pv = getattr(self, "_pages_view", None)
        if pv is not None:
            self._pages_cancel = True
            self._pages_items, self._pages_ok = [], []
            self._pages_view = None
            pv.dismiss()
            self.status_text = "已取消分集选择"
            return
        if getattr(self, "_flow_stop", None) is False:
            self._flow_stop = True
            if self._trial_view is not None:
                self._trial_view.dismiss()
                self._trial_view = None
            self.status_text = "已停止逐首确认"
            return
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
        row = ResultRow(song=song, status={"ok": "成功", "fail": "失败", "skip": "跳过",
                            "confirm": "待确认"}.get(style, "…"),
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
        tot = ""
        try:
            tot = (CODE_DIR / ".version").read_text(encoding="utf-8").strip()
        except Exception:
            pass
        if not app.update_hint.startswith(("正在", "已更新", "检查失败", "已是最新")):
            app.update_hint = f"当前代码版本 {tot}" if tot else ""
        # 这些必须在主线程做，之前被误放进 _check_ffmpeg 线程里，进设置页就报错
        self.ids.out_input.text = app.out_dir
        self.ids.play_input.text = str(app.cfg.get("min_play", 1))
        self.prio = list(app.cfg.get("priority_words") or [])
        self.refresh_prio()
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

    def refresh_prio(self):
        box = self.ids.prio_box
        box.clear_widgets()
        for i, w in enumerate(self.prio):
            row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(4))
            from kivy.uix.button import Button
            row.add_widget(Label(text=w, font_name=FONT, halign="left", valign="middle",
                                 text_size=(None, None)))
            row.add_widget(Button(text="上", size_hint_x=None, width=dp(44), font_name=FONT,
                                  on_release=lambda *_, i=i: self.move(i, -1)))
            row.add_widget(Button(text="下", size_hint_x=None, width=dp(44), font_name=FONT,
                                  on_release=lambda *_, i=i: self.move(i, 1)))
            row.add_widget(Button(text="删", size_hint_x=None, width=dp(44), font_name=FONT,
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
            app.cfg["min_play"] = max(0, int(self.ids.play_input.text.strip() or "1"))
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
