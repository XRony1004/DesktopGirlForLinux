#!/usr/bin/env python3
"""
桌面宠物舞者展示

目录结构：
  dancer/
    anna01/   ← 一个角色，内含 frame_*.png + metadata.json
    alyx2/
    ...

用法：
  .venv/bin/python dancer.py
  .venv/bin/python dancer.py --scale 0.8 --sticky
  .venv/bin/python dancer.py --x 100 --y 100

控制：
  左键拖动      移动窗口
  右键          菜单（切换角色 / 暂停 / 速度 / 退出）
  滚轮上/下     放大 / 缩小
  双击左键      切换置顶 / 取消置顶
"""

import argparse
import json
import sys
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gtk, Gdk, GLib

import cairo


# ── ANSI 颜色 ──────────────────────────────────────────────────────────────────

BOLD = "\033[1m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"

# ── 常量 ──────────────────────────────────────────────────────────────────────

SCALE_MIN = 0.3
SCALE_MAX = 3.0
SCALE_STEP = 0.1
SPEED_OPTIONS = [0.5, 0.75, 1.0, 1.5, 2.0]
DOUBLE_CLICK_MS = 400  # 双击判定时间（毫秒）


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="桌面宠物舞者")
    p.add_argument("--dancer-dir", default="dancer", help="角色根目录（含各子目录）")
    p.add_argument("--scale", default=1.0, type=float, help="显示缩放比例（默认 1.0）")
    p.add_argument("--monitor", default=-1, type=int, help="显示器索引（-1 = 主显示器）")
    p.add_argument("--x", default=None, type=int, help="窗口 X 坐标")
    p.add_argument("--y", default=None, type=int, help="窗口 Y 坐标")
    p.add_argument("--sticky", action="store_true", help="在所有工作区显示（默认仅当前工作区）")
    return p.parse_args()


def get_dancer_subdirs(dancer_dir: Path) -> list[Path]:
    """返回 dancer_dir 下所有有效子目录（含 metadata.json），按名称排序"""
    return sorted(
        d for d in dancer_dir.iterdir()
        if d.is_dir() and (d / "metadata.json").exists()
    )


def read_last(dancer_dir: Path) -> str | None:
    """读取上次使用的角色名，无效则返回 None"""
    last_file = dancer_dir / ".last"
    if last_file.exists():
        name = last_file.read_text().strip()
        if (dancer_dir / name).is_dir():
            return name
    return None


def write_last(dancer_dir: Path, name: str):
    (dancer_dir / ".last").write_text(name)


def load_frames(subdir: Path) -> tuple:
    """
    加载子目录下所有 PNG 帧，返回 (surfaces, regions, fps, width, height)
    出错时抛 ValueError
    """
    png_files = sorted(subdir.glob("frame_*.png"))
    if not png_files:
        raise ValueError(f"'{subdir}' 中没有 frame_*.png 文件")

    meta_path = subdir / "metadata.json"
    if not meta_path.exists():
        raise ValueError(f"找不到 '{meta_path}'")

    with open(meta_path) as f:
        meta = json.load(f)

    total = len(png_files)
    print(f"{CYAN}加载 '{subdir.name}'：{total} 帧...{RESET}")

    surfaces, regions = [], []
    for i, path in enumerate(png_files):
        surf = cairo.ImageSurface.create_from_png(str(path))
        surfaces.append(surf)
        regions.append(Gdk.cairo_region_create_from_surface(surf))
        if (i + 1) % 50 == 0 or (i + 1) == total:
            pct = (i + 1) / total * 100
            bar_len = 20
            filled = int(bar_len * (i + 1) / total)
            bar = f"{'█' * filled}{'░' * (bar_len - filled)}"
            sys.stdout.write(f"\r  {GREEN}{bar}{RESET} {pct:5.1f}% [{i+1}/{total}]")
            sys.stdout.flush()
    sys.stdout.write("\n")

    fps = meta["fps"]
    w = int(meta["width"])
    h = int(meta["height"])
    print(f"{GREEN}✓ 完成{RESET} {w}×{h}px，{fps:.0f}fps\n")
    return surfaces, regions, fps, w, h


def get_monitor_workarea(monitor_index: int):
    display = Gdk.Display.get_default()
    if monitor_index < 0:
        mon = display.get_primary_monitor() or display.get_monitor(0)
    else:
        mon = display.get_monitor(monitor_index)
    wa = mon.get_workarea()
    return wa.x, wa.y, wa.width, wa.height


def print_controls():
    """打印控制说明"""
    print(f"\n{CYAN}{'─' * 40}{RESET}")
    print(f"{BOLD}控制说明{RESET}")
    print(f"  左键拖动      移动窗口")
    print(f"  右键          弹出菜单")
    print(f"  滚轮上/下     放大 / 缩小")
    print(f"  双击左键      切换置顶")
    print(f"{CYAN}{'─' * 40}{RESET}\n")


# ── 主窗口 ────────────────────────────────────────────────────────────────────

class DancerWindow(Gtk.Window):

    def __init__(self, dancer_dir: Path, initial_name: str, scale: float,
                 start_x: int, start_y: int, sticky: bool):
        super().__init__()

        self._dancer_dir = dancer_dir
        self._current_name = initial_name
        self._scale = scale
        self._base_scale = scale  # 记住初始缩放，滚轮在此基础上调整
        self._timer_id = None
        self._paused = False
        self._speed = 1.0
        self._keep_above = True
        self._last_click_time = 0  # 双击检测

        # 加载初始角色
        surfaces, regions, fps, w, h = load_frames(dancer_dir / initial_name)
        self._surfaces = surfaces
        self._regions = regions
        self._frame_idx = 0
        self._n_frames = len(surfaces)
        self._base_fps = fps
        self._frame_w = w
        self._frame_h = h
        self._win_w = int(w * scale)
        self._win_h = int(h * scale)
        self._interval_ms = max(16, int(1000 / fps))

        # --- 窗口属性 ---
        self.set_title("desktop-dancer")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_app_paintable(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)

        if sticky:
            self.stick()

        # --- RGBA 透明 visual ---
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)
        else:
            print(f"{YELLOW}⚠ RGBA visual 不可用，请确认合成器已启用{RESET}")

        # --- 尺寸与位置 ---
        self.set_default_size(self._win_w, self._win_h)
        self.move(start_x, start_y)

        # --- 绘制区域 ---
        self._da = Gtk.DrawingArea()
        self._da.set_size_request(self._win_w, self._win_h)
        self.add(self._da)

        # --- 事件 ---
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.SCROLL_MASK
        )
        self._da.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_button_press)
        self.connect("scroll-event", self._on_scroll)
        self.connect("delete-event", Gtk.main_quit)

        # --- 动画定时器 ---
        self._timer_id = GLib.timeout_add(self._interval_ms, self._on_timer)

    # ── 绘制 ──────────────────────────────────────────────────────────────────

    def _on_draw(self, widget, ctx):
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.paint()

        ctx.set_operator(cairo.OPERATOR_OVER)
        surf = self._surfaces[self._frame_idx]
        sw, sh = surf.get_width(), surf.get_height()
        if sw != self._win_w or sh != self._win_h:
            ctx.scale(self._win_w / sw, self._win_h / sh)
        ctx.set_source_surface(surf, 0, 0)
        ctx.paint()
        return False

    # ── 动画定时器 ────────────────────────────────────────────────────────────

    def _on_timer(self):
        if self._paused:
            return True

        self._frame_idx = (self._frame_idx + 1) % self._n_frames
        gdk_win = self.get_window()
        if gdk_win:
            gdk_win.input_shape_combine_region(
                self._regions[self._frame_idx], 0, 0
            )
        self._da.queue_draw()
        return True

    # ── 鼠标事件 ──────────────────────────────────────────────────────────────

    def _on_button_press(self, widget, event):
        if event.button == 1:
            now = event.time
            # 双击检测
            if (now - self._last_click_time) < DOUBLE_CLICK_MS:
                self._toggle_keep_above()
                self._last_click_time = 0  # 重置，防止连续触发
            else:
                self._last_click_time = now
                self.begin_move_drag(
                    event.button, int(event.x_root), int(event.y_root), event.time
                )
        elif event.button == 3:
            menu = self._build_menu()
            menu.popup_at_pointer(event)
        return True

    # ── 滚轮缩放 ──────────────────────────────────────────────────────────────

    def _on_scroll(self, widget, event):
        if event.direction == Gdk.ScrollDirection.UP:
            self._scale = min(SCALE_MAX, self._scale + SCALE_STEP)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self._scale = max(SCALE_MIN, self._scale - SCALE_STEP)
        else:
            return False

        self._apply_scale()
        return True

    def _apply_scale(self):
        """重新计算窗口大小并应用"""
        new_w = int(self._frame_w * self._scale)
        new_h = int(self._frame_h * self._scale)
        if new_w != self._win_w or new_h != self._win_h:
            self._win_w = new_w
            self._win_h = new_h
            self._da.set_size_request(new_w, new_h)
            self.resize(new_w, new_h)
            self._da.queue_draw()

    # ── 置顶切换 ──────────────────────────────────────────────────────────────

    def _toggle_keep_above(self):
        self._keep_above = not self._keep_above
        self.set_keep_above(self._keep_above)
        state = "置顶" if self._keep_above else "取消置顶"
        print(f"{CYAN}{'📌' if self._keep_above else '📍'} {state}{RESET}")

    # ── 暂停/继续 ─────────────────────────────────────────────────────────────

    def _toggle_pause(self):
        self._paused = not self._paused
        state = "已暂停 ⏸" if self._paused else "继续播放 ▶"
        print(f"{CYAN}{state}{RESET}")

    # ── 播放速度 ──────────────────────────────────────────────────────────────

    def _set_speed(self, speed: float):
        if speed == self._speed:
            return
        self._speed = speed
        new_interval = max(16, int(1000 / (self._base_fps * speed)))

        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
        self._interval_ms = new_interval
        self._timer_id = GLib.timeout_add(self._interval_ms, self._on_timer)
        print(f"{CYAN}播放速度：{speed}x{RESET}")

    # ── 右键菜单 ──────────────────────────────────────────────────────────────

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        # ── 暂停 / 继续 ──
        pause_label = "▶ 继续" if self._paused else "⏸ 暂停"
        pause_item = Gtk.MenuItem(label=pause_label)
        pause_item.connect("activate", lambda _: self._toggle_pause())
        menu.append(pause_item)

        # ── 播放速度子菜单 ──
        speed_item = Gtk.MenuItem(label=f"⏩ 速度 ({self._speed}x)")
        speed_menu = Gtk.Menu()
        for s in SPEED_OPTIONS:
            label = f"{'● ' if s == self._speed else '  '}{s}x"
            item = Gtk.MenuItem(label=label)
            if s != self._speed:
                item.connect("activate", lambda _, sp=s: self._set_speed(sp))
            else:
                item.set_sensitive(False)
            speed_menu.append(item)
        speed_item.set_submenu(speed_menu)
        menu.append(speed_item)

        # ── 置顶切换 ──
        pin_label = "📍 取消置顶" if self._keep_above else "📌 始终置顶"
        pin_item = Gtk.MenuItem(label=pin_label)
        pin_item.connect("activate", lambda _: self._toggle_keep_above())
        menu.append(pin_item)

        menu.append(Gtk.SeparatorMenuItem())

        # ── 角色列表 ──
        subdirs = get_dancer_subdirs(self._dancer_dir)
        if not subdirs:
            item = Gtk.MenuItem(label="（无可用角色）")
            item.set_sensitive(False)
            menu.append(item)
        else:
            for d in subdirs:
                name = d.name
                label = f"{'▸ ' if name == self._current_name else '  '}{name}"
                item = Gtk.MenuItem(label=label)
                if name == self._current_name:
                    item.set_sensitive(False)
                else:
                    item.connect("activate", lambda _, n=name: self.switch_to(n))
                menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="✕ 退出")
        quit_item.connect("activate", lambda _: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()
        return menu

    # ── 切换角色 ──────────────────────────────────────────────────────────────

    def switch_to(self, name: str):
        if name == self._current_name:
            return

        print(f"\n{CYAN}切换到：{name}{RESET}")
        try:
            surfaces, regions, fps, w, h = load_frames(self._dancer_dir / name)
        except ValueError as e:
            print(f"{YELLOW}切换失败：{e}{RESET}")
            return

        # 更新帧数据
        self._surfaces = surfaces
        self._regions = regions
        self._frame_idx = 0
        self._n_frames = len(surfaces)
        self._current_name = name
        self._base_fps = fps
        self._frame_w = w
        self._frame_h = h

        # 重建定时器（考虑速度）
        new_interval = max(16, int(1000 / (fps * self._speed)))
        if new_interval != self._interval_ms:
            if self._timer_id is not None:
                GLib.source_remove(self._timer_id)
            self._interval_ms = new_interval
            self._timer_id = GLib.timeout_add(self._interval_ms, self._on_timer)

        # 调整窗口
        self._apply_scale()

        # 更新 input shape 并重绘
        gdk_win = self.get_window()
        if gdk_win and self._regions:
            gdk_win.input_shape_combine_region(self._regions[0], 0, 0)
        self._da.queue_draw()

        # 记住这次选择
        write_last(self._dancer_dir, name)

    # ── realize 后初始化 input shape ─────────────────────────────────────────

    def do_realize(self):
        Gtk.Window.do_realize(self)
        gdk_win = self.get_window()
        if gdk_win and self._regions:
            gdk_win.input_shape_combine_region(self._regions[0], 0, 0)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    dancer_dir = Path(args.dancer_dir)

    if not dancer_dir.is_dir():
        sys.exit(f"错误：找不到角色根目录 '{dancer_dir}'，请先创建并放入角色子目录")

    subdirs = get_dancer_subdirs(dancer_dir)
    if not subdirs:
        sys.exit(f"错误：'{dancer_dir}' 下没有有效的角色子目录（需含 metadata.json）")

    # 决定初始角色
    last = read_last(dancer_dir)
    initial_name = last if last else subdirs[0].name
    tag = f"{DIM}（上次）{RESET}" if last else f"{DIM}（首个）{RESET}"
    print(f"\n{BOLD}初始角色：{RESET}{GREEN}{initial_name}{RESET} {tag}")

    # 默认位置：主显示器右下角
    mon_x, mon_y, mon_w, mon_h = get_monitor_workarea(args.monitor)
    MARGIN = 20

    # 读取初始尺寸以计算位置
    try:
        _, _, _, fw, fh = load_frames(dancer_dir / initial_name)
    except ValueError as e:
        sys.exit(f"错误：{e}")

    win_w = int(fw * args.scale)
    win_h = int(fh * args.scale)
    start_x = args.x if args.x is not None else (mon_x + mon_w - win_w - MARGIN)
    start_y = args.y if args.y is not None else (mon_y + mon_h - win_h - MARGIN)

    print(f"位置：({start_x}, {start_y})，{win_w}×{win_h}px")
    print_controls()

    win = DancerWindow(
        dancer_dir=dancer_dir,
        initial_name=initial_name,
        scale=args.scale,
        start_x=start_x,
        start_y=start_y,
        sticky=args.sticky,
    )
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
