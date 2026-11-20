"""主题：深色 / 浅色 / 跟随系统（Kivy 端，自己实现，不照搬 Windows 的 Tkinter 方案）。

设计：
- 所有颜色都是 Kivy 属性（ListProperty），切换时只改属性值，kv 里的绑定
  自动刷新，**不重建窗口**、不 destroy 控件。
- 偏好存 `pc.data_dir()/theme.json`（与 settings 同目录）；文件缺失、JSON 坏、
  取值非法，一律回退深色，绝不让启动失败。
- 「跟随系统」只在启动与用户手动切换时各读一次（Android 读 Configuration.uiMode），
  不注册系统监听——与 Windows 端一致，避免旋转/切换主题把界面刷花。
"""

from __future__ import annotations

import json
from typing import Any

from kivy.event import EventDispatcher
from kivy.properties import ListProperty, StringProperty

import platform_compat as pc

MODES = ("dark", "light", "system")
MODE_TEXT = {"dark": "深色", "light": "浅色", "system": "跟随系统"}
TEXT_MODE = {v: k for k, v in MODE_TEXT.items()}
THEME_FILE_NAME = "theme.json"

# 深色：沿用主界面原来的配色（低饱和灰 + 近白文字）
DARK: dict[str, tuple[float, float, float, float]] = {
    "bg": (0.058, 0.066, 0.090, 1),
    "surface": (0.17, 0.18, 0.23, 1),
    "input_bg": (0.13, 0.14, 0.18, 1),
    "input_fg": (0.92, 0.93, 0.96, 1),
    "text": (0.90, 0.91, 0.95, 1),
    "text_dim": (0.72, 0.74, 0.82, 1),
    "text_sub": (0.55, 0.57, 0.65, 1),
    "text_hint": (0.45, 0.47, 0.55, 1),
    "text_label": (0.65, 0.67, 0.75, 1),
    "btn": (0.25, 0.27, 0.33, 1),
    "btn_text": (0.92, 0.93, 0.97, 1),
    "neutral": (0.28, 0.30, 0.36, 1),
    "neutral2": (0.18, 0.19, 0.24, 1),
    "disabled": (0.22, 0.23, 0.28, 1),
    "ok": (0.20, 0.62, 0.38, 1),
    "ok_soft": (0.20, 0.34, 0.28, 1),
    "warn": (0.45, 0.35, 0.15, 1),
    "warn_soft": (0.34, 0.26, 0.20, 1),
    "info": (0.18, 0.42, 0.68, 1),
    "info_soft": (0.20, 0.34, 0.48, 1),
    "danger": (0.75, 0.30, 0.30, 1),
    "danger_soft": (0.32, 0.20, 0.24, 1),
    "accent": (0.20, 0.35, 0.55, 1),
    "ok_text": (0.45, 0.78, 0.55, 1),
    "warn_text": (0.75, 0.68, 0.45, 1),
    "status_wait": (0.85, 0.85, 0.90, 1),
    "status_ok": (0.24, 0.78, 0.45, 1),
    "status_fail": (0.95, 0.35, 0.35, 1),
    "status_skip": (0.60, 0.60, 0.65, 1),
    "status_confirm": (0.98, 0.76, 0.25, 1),
}

# 浅色：白底、深字，按钮用浅灰底，状态色加深保证对比度
LIGHT: dict[str, tuple[float, float, float, float]] = {
    "bg": (0.95, 0.955, 0.965, 1),
    "surface": (1.0, 1.0, 1.0, 1),
    "input_bg": (0.90, 0.91, 0.93, 1),
    "input_fg": (0.10, 0.11, 0.14, 1),
    "text": (0.09, 0.10, 0.13, 1),
    "text_dim": (0.30, 0.32, 0.38, 1),
    "text_sub": (0.38, 0.40, 0.46, 1),
    "text_hint": (0.46, 0.48, 0.54, 1),
    "text_label": (0.22, 0.24, 0.30, 1),
    "btn": (0.86, 0.88, 0.91, 1),
    "btn_text": (0.10, 0.11, 0.14, 1),
    "neutral": (0.82, 0.84, 0.88, 1),
    "neutral2": (0.88, 0.89, 0.92, 1),
    "disabled": (0.90, 0.91, 0.93, 1),
    "ok": (0.13, 0.55, 0.32, 1),
    "ok_soft": (0.78, 0.91, 0.82, 1),
    "warn": (0.80, 0.55, 0.15, 1),
    "warn_soft": (0.98, 0.91, 0.76, 1),
    "info": (0.13, 0.40, 0.75, 1),
    "info_soft": (0.82, 0.89, 0.98, 1),
    "danger": (0.75, 0.20, 0.20, 1),
    "danger_soft": (0.97, 0.83, 0.83, 1),
    "accent": (0.15, 0.35, 0.70, 1),
    "ok_text": (0.06, 0.48, 0.24, 1),
    "warn_text": (0.62, 0.42, 0.05, 1),
    "status_wait": (0.30, 0.32, 0.38, 1),
    "status_ok": (0.05, 0.52, 0.28, 1),
    "status_fail": (0.78, 0.15, 0.15, 1),
    "status_skip": (0.45, 0.47, 0.52, 1),
    "status_confirm": (0.72, 0.48, 0.02, 1),
}

PALETTES = {"dark": DARK, "light": LIGHT}

_COLOR_KEYS = tuple(DARK.keys())


def resolve_mode(mode: str) -> str:
    """把偏好（可能含 system）解析成实际可用的 dark / light；非法一律 dark。"""
    m = str(mode or "").strip().lower()
    if m == "system":
        m = system_mode()
    return m if m in ("dark", "light") else "dark"


def system_mode() -> str:
    """读系统明暗。Android 用 Configuration.uiMode；读不到/桌面平台一律深色。"""
    try:
        from jnius import autoclass  # type: ignore
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        cfg = activity.getResources().getConfiguration()
        conf = autoclass("android.content.res.Configuration")
        night = cfg.uiMode & conf.UI_MODE_NIGHT_MASK
        return "light" if night == conf.UI_MODE_NIGHT_YES else "dark"
    except Exception:
        return "dark"


def theme_file():
    try:
        return pc.data_dir() / THEME_FILE_NAME
    except Exception:
        return None


def load_pref() -> str:
    """读主题偏好；任何异常都回退 dark（启动不能因为主题配置失败）。"""
    try:
        path = theme_file()
        if path is None:
            return "dark"
        raw = json.loads(path.read_text(encoding="utf-8"))
        mode = raw.get("mode") if isinstance(raw, dict) else None
        if mode in MODES:
            return str(mode)
    except Exception:
        pass
    return "dark"


def save_pref(mode: str) -> bool:
    """写主题偏好；目录只读/其它异常静默失败，绝不打断用户。"""
    try:
        path = theme_file()
        if path is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mode": mode}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return True
    except Exception:
        return False


class Theme(EventDispatcher):
    """主题单例：kv 里用 `app.theme.xxx` 绑定颜色。"""

    mode = StringProperty("dark")      # 实际使用的 dark / light
    pref = StringProperty("dark")      # 用户偏好（可能是 system）

    bg = ListProperty([0, 0, 0, 1])
    surface = ListProperty([0, 0, 0, 1])
    input_bg = ListProperty([0, 0, 0, 1])
    input_fg = ListProperty([0, 0, 0, 1])
    text = ListProperty([0, 0, 0, 1])
    text_dim = ListProperty([0, 0, 0, 1])
    text_sub = ListProperty([0, 0, 0, 1])
    text_hint = ListProperty([0, 0, 0, 1])
    text_label = ListProperty([0, 0, 0, 1])
    btn = ListProperty([0, 0, 0, 1])
    btn_text = ListProperty([0, 0, 0, 1])
    neutral = ListProperty([0, 0, 0, 1])
    neutral2 = ListProperty([0, 0, 0, 1])
    disabled = ListProperty([0, 0, 0, 1])
    ok = ListProperty([0, 0, 0, 1])
    ok_soft = ListProperty([0, 0, 0, 1])
    warn = ListProperty([0, 0, 0, 1])
    warn_soft = ListProperty([0, 0, 0, 1])
    info = ListProperty([0, 0, 0, 1])
    info_soft = ListProperty([0, 0, 0, 1])
    danger = ListProperty([0, 0, 0, 1])
    danger_soft = ListProperty([0, 0, 0, 1])
    accent = ListProperty([0, 0, 0, 1])
    ok_text = ListProperty([0, 0, 0, 1])
    warn_text = ListProperty([0, 0, 0, 1])
    status_wait = ListProperty([0, 0, 0, 1])
    status_ok = ListProperty([0, 0, 0, 1])
    status_fail = ListProperty([0, 0, 0, 1])
    status_skip = ListProperty([0, 0, 0, 1])
    status_confirm = ListProperty([0, 0, 0, 1])

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._apply_palette(DARK)

    def _apply_palette(self, pal: dict[str, tuple[float, float, float, float]]) -> None:
        for key in _COLOR_KEYS:
            try:
                setattr(self, key, list(pal[key]))
            except Exception:
                pass

    def set_mode(self, mode: str, save: bool = True) -> str:
        """切换主题。mode 可以是 dark/light/system；返回实际生效的 dark/light。"""
        pref = str(mode or "dark").strip().lower()
        if pref not in MODES:
            pref = "dark"
        resolved = resolve_mode(pref)
        self._apply_palette(PALETTES[resolved])
        self.mode = resolved
        self.pref = pref
        if save:
            save_pref(pref)
        return resolved

    def toggle(self) -> str:
        """在深/浅之间快速切换（设置页「跟随系统」之外的一键切换用）。"""
        return self.set_mode("light" if self.mode == "dark" else "dark")

    def label(self) -> str:
        return MODE_TEXT.get(self.pref, "深色")


THEME = Theme()


def apply_saved() -> Theme:
    """启动时调用：按 theme.json 应用（读失败已在 load_pref 里回退 dark）。"""
    THEME.set_mode(load_pref(), save=False)
    return THEME