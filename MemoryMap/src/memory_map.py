"""记忆星图 —— 把一摞 Markdown 记忆文件画成一张会动的辐射图。

它只读你在 config.json 里点名的那些文件，不遍历别的目录，也不联网。
每 5 秒看一次文件的修改时间，谁变了就重画；另外读一个 picks 文件，
那份文件里列到的条目会被标成醒目的颜色（谁标的、怎么标，都是你自己的事）。

用法：
    python src/memory_map.py                 # 用同目录的 config.json
    python src/memory_map.py --config my.json
    python src/memory_map.py --memory-root D:\notes

依赖：Python 3.10+，Windows 下用 tkinter（自带）。打包见 scripts/build.ps1。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import re
import sys
import time
import tkinter as tk
import traceback
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont


def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_dpi_awareness()

DEFAULT_CONFIG = {
    "memory_root": "examples/notes",
    "files": [
        {"file": "01-people.md", "label": "关系"},
        {"file": "02-self.md", "label": "自我"},
        {"file": "03-timeline.md", "label": "经历"},
        {"file": "04-feelings.md", "label": "感受"},
        {"file": "05-promises.md", "label": "约定"},
        {"file": "07-continue.md", "label": "延续"},
        {"file": "08-past.md", "label": "过往"},
        {"file": "09-traits.md", "label": "性格"},
    ],
    "picks_file": "picks.json",
    "error_log": "memory_map_error.log",
    "max_sections": 0,        # 0 = 只画框架（中心 + 分类）；调大就会画出每篇里的分节
    "refresh_ms": 5000,
}


def _find_config(argv):
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            return Path(argv[i + 1])
    here = Path(__file__).resolve().parent
    for cand in (here / "config.json", here.parent / "config.json"):
        if cand.is_file():
            return cand
    return None


def load_config(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = _find_config(argv)
    if path is not None:
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update(user)
        except (OSError, ValueError, UnicodeError):
            pass
    for i, a in enumerate(argv):
        if a == "--memory-root" and i + 1 < len(argv):
            cfg["memory_root"] = argv[i + 1]
    return cfg


CONFIG = load_config()
MEMORY_ROOT = Path(CONFIG["memory_root"]).expanduser()
if not MEMORY_ROOT.is_absolute():
    MEMORY_ROOT = (Path(__file__).resolve().parent.parent / MEMORY_ROOT)
PUBLIC_FILES = tuple((f["file"], f["label"]) for f in CONFIG["files"])


def _cfg_path(key, default):
    p = Path(CONFIG.get(key) or default).expanduser()
    if not p.is_absolute():
        p = (Path(__file__).resolve().parent.parent / p)
    return p


PICKS_FILE = _cfg_path("picks_file", "picks.json")
ERR_LOG = _cfg_path("error_log", "memory_map_error.log")

# —— 浅底这一套：暖白、灰线、草木绿，红只给它标的那几条 ——
BG = "#f7f7f4"
PANEL = "#ffffff"
HAIRLINE = "#e8e8e3"
INK = "#16181a"
SOFT = "#5f6368"
FAINT = "#9aa0a6"
LINE = "#deded9"
LINE_SOFT = "#eaeae5"
CORE = "#141618"
CAT = "#2f4b3c"
DOT = "#5f9e73"
DOT_EMPTY = "#b6c2b8"
MARK = "#d4553f"

FONT = "Microsoft YaHei UI"
FONT_LIGHT = "Microsoft YaHei UI Light"
MAX_SECTIONS = int(CONFIG.get("max_sections", 0))
MIN_SECTION_CHARS = 60
REFRESH_MS = int(CONFIG.get("refresh_ms", 5000))
FLOW_MS = 33              # 30 fps：再高的话 tkinter 每个球都要重画一遍，烧 CPU 换不来肉眼能看的差别
FLOW_SPEED = 1.05
DRIFT = 6.8               # 小球飘多远（逻辑像素）：飘得太慢会卡在像素缝里，看着就僵
BLANK_TEXT = "（这条还空着——等它自己补）"


def _log_exception(exc, val, tb) -> None:
    """回调里出的任何问题都落到文件里——windowed 的 exe 本来什么都不吐。"""
    try:
        with open(ERR_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), "".join(traceback.format_exception(exc, val, tb))))
    except Exception:
        pass


@dataclass
class Node:
    key: str
    label: str
    kind: str
    file: Path | None
    content: str
    x: float
    y: float
    color: str
    radius: int
    important: bool = False
    phase: float = 0.0        # 飘动相位，load 时算好，别每帧去哈希
    period: float = 6.0       # 飘一圈几秒
    group: str = "gcore"      # 十六个飘动组之一
    ball_group: str = "b0"    # 只打在球上的组 tag：每帧推一次整组，字不动
    b_phase: float = 0.0      # 呼吸相位
    b_speed: float = 1.6      # 呼吸快慢


def _fade(c1: str, c2: str, k: float) -> str:
    """两个颜色之间插值，k=0 是 c1，k=1 是 c2。"""
    k = max(0.0, min(1.0, k))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def _display_heading(line: str) -> str:
    title = re.sub(r"^#{1,4}\s+", "", line).strip()
    title = title.replace("**", "").replace("\x60", "")
    return title[:26]


def _body_chars(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def load_picks() -> list[tuple[str, str]]:
    try:
        data = json.loads(PICKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return []
    out: list[tuple[str, str]] = []
    for item in (data.get("picks") or []):
        if isinstance(item, str) and "#" in item:
            filename, title = item.split("#", 1)
            out.append((filename.strip(), title.strip()))
    return out


_PUNCT = re.compile("[\\s*\\x60\u300c\u300d\u201c\u201d\u2018\u2019\"']")


def _norm_key(text: str) -> str:
    return _PUNCT.sub("", text)


def _is_picked(picks: list[tuple[str, str]], filename: str, label: str) -> bool:
    nl = _norm_key(label)
    for f, t in picks:
        if f != filename or not t:
            continue
        nt = _norm_key(t)
        if nt and (nl.startswith(nt) or nt.startswith(nl)):
            return True
    return False


def _phase(key: str) -> float:
    h = int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16)
    return (h % 10000) / 10000.0 * 2 * math.pi


def _period(key: str) -> float:
    h = int(hashlib.sha1((key + "p").encode("utf-8")).hexdigest()[:8], 16)
    return 2.0 + (h % 200) / 100.0        # 2.0 ~ 4.0 秒一圈


def load_nodes(ui: float) -> tuple[list[Node], list[tuple[str, str]], dict[str, float], int]:
    picks = load_picks()
    nodes = [Node("core", "智能体", "core", None, "这张记忆的全貌", 0, 0, CORE, int(16 * ui))]
    edges: list[tuple[str, str]] = []
    mtimes: dict[str, float] = {}
    empty_count = 0
    total = len(PUBLIC_FILES)
    for i, (filename, category) in enumerate(PUBLIC_FILES):
        path = MEMORY_ROOT / filename
        angle = i * 2 * math.pi / total - math.pi / 2
        x, y = 268 * ui * math.cos(angle), 268 * ui * math.sin(angle)
        if not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8")
            mtimes[filename] = path.stat().st_mtime
        except (OSError, UnicodeError):
            continue
        key = "file:" + filename
        nodes.append(Node(key, category, "category", path, body[:12000], x, y, CAT, int(7 * ui)))
        edges.append(("core", key))
        matches = list(re.finditer(r"(?m)^#{1,3}\s+(.+?)\s*$", body))
        sections = []
        for match in matches:
            label = _display_heading(match.group(0))
            if not label:
                continue
            end = next((m.start() for m in matches if m.start() > match.start()), len(body))
            sections.append((label, body[match.start():end].strip(), match.start()))
        substantial = [s for s in sections if _body_chars(s[1]) >= MIN_SECTION_CHARS]
        blanks = [s for s in sections if _body_chars(s[1]) < MIN_SECTION_CHARS]
        chosen = (substantial + blanks)[:MAX_SECTIONS]
        for j, (label, content, start) in enumerate(chosen):
            blank = _body_chars(content) < MIN_SECTION_CHARS
            picked = _is_picked(picks, filename, label)
            if blank and not picked:
                empty_count += 1
            subangle = angle + (j - (len(chosen) - 1) / 2) * 0.105
            distance = (382 + (j % 2) * 30) * ui
            subkey = "section:" + hashlib.sha1((filename + str(start)).encode()).hexdigest()[:12]
            if picked:
                color, radius = MARK, int(6.5 * ui)
            elif blank:
                color, radius = DOT_EMPTY, int(3.4 * ui)
            else:
                color, radius = DOT, int(3.4 * ui)
            nodes.append(Node(subkey, label, "empty" if blank else "section", path,
                              BLANK_TEXT if blank else content[:5000],
                              distance * math.cos(subangle), distance * math.sin(subangle),
                              color, radius, picked))
            edges.append((key, subkey))
    for i, n in enumerate(nodes):
        n.phase = _phase(n.key)
        n.period = _period(n.key)
        n.group = "gcore" if n.kind == "core" else "g%d" % (i % 16)
        n.ball_group = "b" + n.group[1:]
        n.b_phase = (int(hashlib.sha1((n.key + "b").encode()).hexdigest()[:8], 16) % 1000) / 1000.0 * 2 * math.pi
        n.b_speed = 1.6 + (int(hashlib.sha1((n.key + "s").encode()).hexdigest()[:8], 16) % 180) / 100.0
    return nodes, edges, mtimes, empty_count


class MemoryMap:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.ui = self._ui_scale()
        self.root.title("智能体 · 记忆星图")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = int(min(1480 * self.ui, sw * 0.94))
        h = int(min(900 * self.ui, sh * 0.94))
        self.root.geometry("%dx%d" % (w, h))
        self.root.minsize(int(980 * self.ui), int(620 * self.ui))
        self.root.configure(bg=BG)
        self.root.report_callback_exception = _log_exception
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.drag_from: tuple[int, int] | None = None
        self.nodes, self.edges, self.mtimes, self.empty_count = load_nodes(self.ui)
        self.node_by_key = {n.key: n for n in self.nodes}
        self.selected = "core"
        self.flow_specs: list[tuple[int, float, float, float, float]] = []
        self.offsets: dict[str, tuple[float, float]] = {}
        self.ripples: list[list] = []
        self.breath_frame = 0
        self.hover_key: str | None = None
        self.hover_ring: int | None = None
        self.font_light = self._pick_light_font()

        u = self.ui
        self.header = tk.Frame(self.root, bg=BG, height=int(84 * u))
        self.header.pack(fill="x")
        tk.Label(self.header, text="智能体 · 记忆星图", bg=BG, fg=INK,
                 font=(self.font_light, -int(26 * u))).pack(side="left", padx=int(34 * u), pady=int(24 * u))
        tk.Frame(self.header, bg=HAIRLINE, width=1).pack(side="left", fill="y",
                                                          pady=int(34 * u), padx=int(4 * u))
        self.status = tk.Label(self.header, text="", bg=BG, fg=FAINT, font=(FONT, -int(11 * u)))
        self.status.pack(side="right", padx=int(34 * u))
        self._refresh_status()

        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.body, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.sidebar = tk.Frame(self.body, bg=PANEL, width=int(330 * u))
        self.sidebar.pack(side="right", fill="y")
        self.sidebar.pack_propagate(False)
        tk.Frame(self.sidebar, bg=HAIRLINE, height=1).pack(fill="x", padx=int(30 * u), pady=(int(30 * u), 0))
        tk.Label(self.sidebar, text="记 忆 详 情", bg=PANEL, fg=SOFT,
                 font=(FONT, -int(11 * u))).pack(anchor="w", padx=int(30 * u), pady=(int(22 * u), int(20 * u)))
        self.title = tk.Label(self.sidebar, text="智能体", bg=PANEL, fg=INK,
                              font=(self.font_light, -int(22 * u)), anchor="w",
                              wraplength=int(270 * u), justify="left")
        self.title.pack(fill="x", padx=int(30 * u))
        self.source = tk.Label(self.sidebar, text="公开记忆", bg=PANEL, fg=FAINT,
                               font=(FONT, -int(10 * u)), anchor="w")
        self.source.pack(fill="x", padx=int(30 * u), pady=(int(8 * u), int(14 * u)))
        self.detail = tk.Text(self.sidebar, bg=PANEL, fg=SOFT, bd=0, relief="flat", wrap="word",
                              font=(FONT, -int(12 * u)), padx=int(30 * u), pady=int(18 * u),
                              spacing1=int(3 * u), spacing3=int(9 * u), insertbackground=INK,
                              selectbackground="#dfe6ea", cursor="arrow")
        self.detail.pack(fill="both", expand=True)
        self.detail.insert("1.0", "点击星图中的节点，查看这段公开记忆。")
        self.detail.configure(state="disabled")

        self.canvas.bind("<Configure>", lambda _e: self.draw())
        self.canvas.bind("<ButtonPress-1>", self.on_down)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_up)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self.zoom(e.x, e.y, 1.12))
        self.canvas.bind("<Button-5>", lambda e: self.zoom(e.x, e.y, 1 / 1.12))
        self.root.bind("<F5>", lambda _e: self.reload(force=True))
        self.root.after(REFRESH_MS, self.refresh)
        self.root.after(FLOW_MS, self._animate)

    def _pick_light_font(self) -> str:
        try:
            families = set(tkfont.families(self.root))
        except Exception:
            return FONT
        for name in (FONT_LIGHT, "Microsoft YaHei UI Light", "Microsoft YaHei Light"):
            if name in families:
                return name
        return FONT

    def _ui_scale(self) -> float:
        try:
            dpi = float(self.root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        return max(1.0, min(3.0, dpi / 96.0))

    def _refresh_status(self, changed: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S")
        filled = sum(1 for n in self.nodes if n.kind in ("core", "category", "section"))
        parts = ["节点 %d" % filled, "刷新 %s" % stamp]
        if self.empty_count:
            parts.append("空着 %d" % self.empty_count)
        picked = sum(1 for n in self.nodes if n.important)
        if picked:
            parts.append("标红 %d" % picked)
        if changed:
            parts.append("刚更新")
        self.status.configure(text="    ".join(parts))

    def coords(self, node: Node) -> tuple[float, float]:
        cx = self.canvas.winfo_width() / 2 + self.offset_x
        cy = self.canvas.winfo_height() / 2 + self.offset_y
        return cx + node.x * self.scale, cy + node.y * self.scale

    def node_now(self, node: Node) -> tuple[float, float]:
        """节点现在在哪儿（含漂移）。"""
        x, y = self.coords(node)
        ox, oy = self.offsets.get(node.key, (0.0, 0.0))
        return x + ox, y + oy

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 2 or h < 2:
            return
        u = self.ui
        self.offsets = {}
        self.flow_specs = []
        self.ripples = []
        self.hover_ring = None
        cx, cy = self.coords(self.node_by_key["core"])

        for r in (170, 268, 382):
            rr = r * u * self.scale
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=LINE_SOFT, width=1)

        trunk_specs: list[tuple[float, float, float, float]] = []
        for source, target in self.edges:
            a, b = self.node_by_key[source], self.node_by_key[target]
            x1, y1 = self.coords(a)
            x2, y2 = self.coords(b)
            if source == "core":
                c.create_line(x1, y1, x2, y2, fill=LINE, width=1)
                trunk_specs.append((x1, y1, x2, y2))
            elif b.important:
                c.create_line(x1, y1, x2, y2, fill="#ecc9c2", width=1)
            elif b.kind == "empty":
                c.create_line(x1, y1, x2, y2, fill="#eeeee9", width=1, dash=(3, 5))
            else:
                c.create_line(x1, y1, x2, y2, fill="#e9e9e4", width=1)

        for node in self.nodes:
            x, y = self.coords(node)
            radius = max(3.0, node.radius * self.scale)
            tag = "n:" + node.key
            ball = "d:" + node.key
            if node.kind == "core":
                c.create_oval(x - radius, y - radius, x + radius, y + radius,
                              fill=CORE, outline="", tags=(tag, ball, "gcore"))
                c.create_text(x, y + radius + int(18 * u), text=node.label, fill=INK,
                              font=(self.font_light, -int(18 * u)), anchor="n", tags=(tag,))
            elif node.kind == "category":
                c.create_oval(x - radius, y - radius, x + radius, y + radius,
                              fill=CAT, outline="", tags=(tag, ball, node.ball_group))
                c.create_text(x, y + radius + int(14 * u), text=node.label, fill=SOFT,
                              font=(FONT, -int(11 * u)), width=int(150 * u), anchor="n", tags=(tag,))
            elif node.kind == "empty":
                c.create_oval(x - radius, y - radius, x + radius, y + radius,
                              fill="", outline=DOT_EMPTY, width=1, tags=(tag, ball, node.ball_group))
                if self.scale > 0.75:
                    c.create_text(x, y + radius + int(11 * u), text=node.label, fill=FAINT,
                                  font=(FONT, -int(9 * u)), width=int(150 * u), anchor="n", tags=(tag,))
            else:
                c.create_oval(x - radius, y - radius, x + radius, y + radius,
                              fill=node.color, outline="", tags=(tag, ball, node.ball_group))
                if self.scale > 0.85:
                    c.create_text(x, y + radius + int(11 * u), text=node.label,
                                  fill=MARK if node.important else SOFT,
                                  font=(FONT, -int(9 * u)), width=int(150 * u), anchor="n", tags=(tag,))
            if node.key == self.selected:
                c.create_oval(x - radius - 12, y - radius - 12, x + radius + 12, y + radius + 12,
                              outline="#c9c9c4", width=1, tags=(tag, ball, node.ball_group))

        for x1, y1, x2, y2 in trunk_specs:
            dot = c.create_oval(0, 0, 0, 0, fill="#8a8a86", outline="")
            self.flow_specs.append((dot, x1, y1, x2, y2))

        self.hover_ring = c.create_oval(0, 0, 0, 0, outline="#8f9a92", width=2, state="hidden")
        if self.hover_key:
            self._place_hover()

    def _place_hover(self) -> None:
        node = self.node_by_key.get(self.hover_key or "")
        if node is None or self.hover_ring is None:
            return
        x, y = self.node_now(node)
        r = max(6.0, node.radius * self.scale) + 7
        self.canvas.coords(self.hover_ring, x - r, y - r, x + r, y + r)
        self.canvas.itemconfig(self.hover_ring, state="normal")

    def on_motion(self, event: tk.Event) -> None:
        """鼠标扫过画布：自己判断压在哪颗球上。不用 Enter/Leave——那两个事件会自己咬自己。"""
        if self.drag_from is not None:
            return
        node = self.pick(event.x, event.y)
        self._set_hover(node.key if node else None)

    def _set_hover(self, key: str | None) -> None:
        if key == self.hover_key:
            return
        self.hover_key = key
        if key is None:
            self.canvas.configure(cursor="")
            if self.hover_ring is not None:
                try:
                    self.canvas.itemconfig(self.hover_ring, state="hidden")
                except tk.TclError:
                    pass
        else:
            self.canvas.configure(cursor="hand2")
            self._place_hover()

    def _ripple(self, x: float, y: float, color: str = "#b9c2bb") -> None:
        if self.canvas is None:
            return
        item = self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3, outline=color, width=2)
        self.ripples.append([item, x, y, time.time()])

    def _animate(self) -> None:
        try:
            self._animate_once()
        except Exception:
            _log_exception(*sys.exc_info())
        self.root.after(FLOW_MS, self._animate)

    def _animate_once(self) -> None:
        if self.root.winfo_viewable() and self.nodes:
            c = self.canvas
            now = time.time()
            amp = DRIFT * self.ui * self.scale
            # 十六个组各自飘——每组一次 move，比六十多个球一个个推省得多
            for gi in range(16):
                gkey = "b%d" % gi
                ang = 2 * math.pi * (now / (3.0 + (gi % 5) * 0.45)) + gi * 0.7
                ox = amp * math.cos(ang)
                oy = amp * math.sin(ang * 0.93 + 0.6)
                px, py = self.offsets.get(gkey, (0.0, 0.0))
                dx, dy = ox - px, oy - py
                if abs(dx) > 0.06 or abs(dy) > 0.06:
                    c.move(gkey, dx, dy)
                    self.offsets[gkey] = (ox, oy)
            for node in self.nodes:
                if node.kind == "core":
                    continue
                g = self.offsets.get(node.group, (0.0, 0.0))
                self.offsets[node.key] = g
            self._breathe(now)
            if self.flow_specs:
                base = (now * FLOW_SPEED) % 1.0
                r = 1.9 * self.ui * self.scale
                for i, (dot, x1, y1, x2, y2) in enumerate(self.flow_specs):
                    t = (base + i * 0.125) % 1.0
                    e = t * t * (3 - 2 * t)
                    x = x1 + (x2 - x1) * e
                    y = y1 + (y2 - y1) * e
                    c.coords(dot, x - r, y - r, x + r, y + r)
            for rp in list(self.ripples):
                item, x, y, t0 = rp
                age = now - t0
                if age >= 0.62:
                    c.delete(item)
                    self.ripples.remove(rp)
                    continue
                k = age / 0.62
                rr = 5 + 48 * k
                c.coords(item, x - rr, y - rr, x + rr, y + rr)
                c.itemconfig(item, outline=_fade("#a8b4ac", BG, k), width=max(1, int(3 - 2.4 * k)))
            if self.hover_key:
                self._place_hover()

    def _breathe(self, now: float) -> None:
        """粒子那点活气：每颗球按自己的节奏涨落，重点那几颗连颜色一起动。"""
        c = self.canvas
        tint = (self.breath_frame % 4) == 0      # 颜色淡入淡出看不出帧率，隔几帧改一次就够
        self.breath_frame += 1
        for node in self.nodes:
            key = "d:" + node.key
            if node.important:
                amp = 0.44
            elif node.kind == "category":
                amp = 0.34
            elif node.kind == "core":
                amp = 0.26
            else:
                amp = 0.30
            k = math.sin(now * node.b_speed + node.b_phase)
            r = max(2.2, node.radius * self.scale * (1.0 + amp * k))
            x, y = self.node_now(node)
            try:
                c.coords(key, x - r, y - r, x + r, y + r)
                if tint and node.kind in ("section", "empty", "category", "core"):
                    base = MARK if node.important else (DOT_EMPTY if node.kind == "empty" else (CAT if node.kind == "category" else DOT))
                    deep = _fade(base, "#22392c", 0.34 * (1 - k))
                    c.itemconfig(key, fill=deep)
            except tk.TclError:
                continue

    def pick(self, x: int, y: int) -> Node | None:
        for node in reversed(self.nodes):
            px, py = self.node_now(node)
            if (px - x) ** 2 + (py - y) ** 2 <= (max(8, node.radius * self.scale) + 12) ** 2:
                return node
        return None

    def show(self, node: Node) -> None:
        self.selected = node.key
        self.title.configure(text=node.label)
        self.source.configure(text=(node.file.name if node.file else "公开记忆") + "　·　点击别的节点继续")
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", node.content)
        self.detail.configure(state="disabled")
        x, y = self.node_now(node)
        self._ripple(x, y, "#8fbf9f" if node.important else "#b9c2bb")
        self.draw()

    def on_down(self, event: tk.Event) -> None:
        self.drag_from = (event.x, event.y)

    def on_drag(self, event: tk.Event) -> None:
        if self.drag_from:
            x, y = self.drag_from
            dx, dy = event.x - x, event.y - y
            self.offset_x += dx
            self.offset_y += dy
            self.drag_from = (event.x, event.y)
            if dx or dy:
                # 整块推过去，比每帧重建七十多个图元便宜太多
                self.canvas.move("all", dx, dy)
                self.flow_specs = [(d, a + dx, b + dy, cc + dx, dd + dy)
                                   for (d, a, b, cc, dd) in self.flow_specs]
                for rp in self.ripples:
                    rp[1] += dx
                    rp[2] += dy
                self._set_hover(None)

    def on_up(self, event: tk.Event) -> None:
        if self.drag_from:
            x, y = self.drag_from
            if abs(event.x - x) + abs(event.y - y) < 5:
                node = self.pick(event.x, event.y)
                if node:
                    self.show(node)
                else:
                    self._ripple(event.x, event.y, "#cfcfc9")
        self.drag_from = None

    def zoom(self, x: int, y: int, factor: float) -> None:
        old = self.scale
        self.scale = max(0.55, min(1.8, self.scale * factor))
        change = self.scale / old
        cx = self.canvas.winfo_width() / 2 + self.offset_x
        cy = self.canvas.winfo_height() / 2 + self.offset_y
        self.offset_x += (x - cx) * (1 - change)
        self.offset_y += (y - cy) * (1 - change)
        self.draw()

    def on_wheel(self, event: tk.Event) -> None:
        self.zoom(event.x, event.y, 1.12 if event.delta > 0 else 1 / 1.12)

    def reload(self, force: bool = False) -> bool:
        changed = force
        try:
            picks_mtime = PICKS_FILE.stat().st_mtime
        except OSError:
            picks_mtime = 0.0
        if picks_mtime != self.mtimes.get("__picks__", 0.0):
            changed = True
        for filename, _category in PUBLIC_FILES:
            path = MEMORY_ROOT / filename
            try:
                current = path.stat().st_mtime
            except OSError:
                current = 0.0
            if current != self.mtimes.get(filename, 0.0):
                changed = True
                break
        if changed:
            self.nodes, self.edges, self.mtimes, self.empty_count = load_nodes(self.ui)
            self.node_by_key = {n.key: n for n in self.nodes}
            self.selected = "core"
            self.mtimes["__picks__"] = picks_mtime
            self.draw()
        self._refresh_status(changed)
        return changed

    def refresh(self) -> None:
        self.reload()
        self.root.after(REFRESH_MS, self.refresh)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    MemoryMap().run()
