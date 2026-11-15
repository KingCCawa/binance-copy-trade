"""币安带单跟单 · 桌面界面（PySide6）。

分层：`feed` / `api_feed` 取数，`copy_engine` 决策，`position_ledger` 重放，
`pnl_tracker` 记账，`state_store` 持久化；本文件只做界面与线程编排，
不含任何跟单判定。

线程模型：网络与引擎全部在后台线程跑，界面线程只消费 `self.events` 队列；
所有跨线程数据以「纯 dict 快照」传递，不共享 Qt 对象。

主题用 QSS 表达：`QApplication.setStyleSheet()` 一次整体替换，Qt 在同一绘制
周期内重建整棵控件树的样式，不存在「一部分先变、一部分后变」的中间态。

运行： py -3.12 gui.py
自测： py -3.12 gui.py --selftest
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

# PySide6 是可选依赖：没装就退出码 3，让 run_all.py 标 SKIP 而不是 FAIL。
try:
    import PySide6  # noqa: F401
except ImportError:      # pragma: no cover
    print("未安装 PySide6，Qt 界面不可用；"
          "py -3.12 -m pip install PySide6 后可启用")
    raise SystemExit(3)

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QLinearGradient, QPainter,
                           QPainterPath, QPen, QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QStatusBar, QTabWidget,
                               QTableWidget, QTableWidgetItem, QTextEdit,
                               QVBoxLayout, QWidget)

try:                       # Windows 标准库，零依赖；其他平台降级为静音
    import winsound
except ImportError:
    winsound = None

try:                       # 同上：非 Windows 上 ctypes.wintypes 会抛 ValueError
    import ctypes.wintypes
except (ImportError, ValueError):        # pragma: no cover
    ctypes.wintypes = None

from account import AccountProbe, load_env
from better_executor import BetterPriceExecutor
from copy_engine import CopyEngine, MemoryBroker, target_position
from feed import (EXAMPLE_PORTFOLIO_ID, WEB_SAMPLE, Fill, Rule,
                  entry_limit_price, parse)
from pnl_tracker import curve_stats
from pnl_tracker import snapshot as pnl_snapshot
from pnl_tracker import trades_from_actions
from sim_portfolio import SimPortfolio
from theme_qss import CURRENT, THEMES, ThemeManager

CONFIG_FILE = ROOT / "gui-config.json"
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / "state.db"
# 界面日志同时落盘一份。只放内存的话，程序重启后「昨晚到底跑没跑、
# 有没有取数失败」就查不到了 —— 错过一次开仓只能靠猜。
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "gui.log"

MONO_FAMILY = "Consolas"
NUM_FONT = QFont(MONO_FAMILY, 10)

# 自绘标题栏高度（逻辑像素）。WM_NCHITTEST 要用它判断「这一下是不是在拖窗口」
TITLEBAR_H = 46
# 无边框窗口的四边缩放热区
RESIZE_MARGIN = 5

# Win32 消息与命中测试码。窗口无边框后，这些部位由我们自己回答，
# 见 App.nativeEvent()。
WM_NCHITTEST = 0x0084
WM_GETMINMAXINFO = 0x0024
WM_NCLBUTTONDBLCLK = 0x00A3
WM_LBUTTONDBLCLK = 0x0203
HTCLIENT, HTCAPTION = 1, 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT = 10, 11, 12, 13
HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 14, 15, 16, 17

# ---------------------------------------------------------------- 配置契约
# 新增配置项：加进 DEFAULTS、在 _build_* 里放控件、登记到 self._inputs /
# _checks / _combos、在 _load_config() 补合法性校验。少一步旧配置文件就会
# 静默缺键。
DEFAULTS = {
    # 跟单目标：币安带单本链接里的纯数字 portfolioId，可在「运行配置」里改。
    "portfolio_id": EXAMPLE_PORTFOLIO_ID,
    # 昵称是 detail 接口的自动缓存，附带它所属的 portfolioId 防止换人后串名。
    "lead_nickname": "",
    "lead_nickname_pid": "",
    "sound_enabled": True,
    "poll_interval_s": "30",
    "log_max_rows": "500",
    "improve_pct": "0.3",
    "slippage_tol_pct": "0.3",
    "better_price_enabled": True,
    "mode": "dry-run",
    # 涨跌配色：binance = 涨绿跌红（币安习惯），cn = 涨红跌绿
    "color_scheme": "binance",
    # 下单杠杆。只影响保证金占用，不参与份额计算 —— 杠杆高只是占的保证金少，
    # 仓位大小永远由「他的数量 × 权益比」决定。有些品种最大杠杆只有 20x，
    # 设得比它高会被交易所拒单（-4028），所以默认保守取 20。
    "leverage": "20",
    "theme": "dark",
    # 程序启动后自动开始轮询。关掉的话每次都要手点「开始轮询」，
    # 忘了就是整夜不跟 —— 2026-11-14 凌晨那次漏单就是这么来的。
    "auto_poll": True,
    # 开机自启：由配置写/删启动文件夹里的快捷方式，不要去手工改那个目录
    "autostart": False,
    # 轮询期间阻止系统睡眠（允许息屏）。系统一睡 socket 全断，醒来信号已过期
    "keep_awake": True,
    # 每轮按账本对账补差：漏掉一条开仓后，仓位靠它自愈补回等比目标
    "rebalance": True,
}
MODES = ("dry-run", "testnet", "live")
PCT_TEXT = {
    True: ("更优幅度 %", "仅当价格优于带单员成交价时挂单。"),
    False: ("可接受滑点 %", "允许更差的进场价，超出容差则漏单。"),
}
MODE_TEXT = {
    "dry-run": "本地模拟，不下单。",
    "testnet": "测试网 Key 与实盘不通用，实盘 Key 会报 -2015。",
    "live": "走币安合约实盘，真实下单。",
}
# 状态栏用的短标签：不能直接拿 MODE_TEXT，那是整句说明
MODE_SHORT = {"dry-run": "本地模拟", "testnet": "测试网", "live": "实盘"}


def as_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 小工具
def label(text: str = "", role: str = "") -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    return lab


def hline() -> QFrame:
    f = QFrame()
    f.setObjectName("hline")
    f.setFixedHeight(1)
    return f


def card(pad: int = 18) -> tuple[QFrame, QVBoxLayout]:
    """圆角卡片：QSS 只认 #card 这个 objectName。"""
    box = QFrame()
    box.setObjectName("card")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(pad, pad, pad, pad)
    lay.setSpacing(0)
    return box, lay


def scroll_host() -> tuple[QScrollArea, QWidget]:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    inner = QWidget()
    area.setWidget(inner)
    return area, inner


def repolish(widget: QWidget) -> None:
    """动态属性改完必须重新 polish，否则 QSS 不会重新求值。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


# ---------------------------------------------------------------- 电源
# 需求只有一条：**系统别睡**。系统一睡 socket 全断、定时器全停，醒来时信号
# 已经过了 15 分钟有效期，等于整夜没跟。显示器则是另一回事 —— 挂一夜必须
# 能熄屏，否则整间屋子亮着没法睡觉。
#
# Windows 把这两件事拆成两个独立请求（PowerRequestSystemRequired /
# PowerRequestDisplayRequired），所以走 Power Request API 只申请「系统」，
# 显示器超时完全不受影响。原来的 SetThreadExecutionState(ES_SYSTEM_REQUIRED)
# 文档上也说不管显示器，但实测在部分机器上会连带把屏幕顶住；而且它是线程级
# 的，状态跟着线程生死，语义不如显式句柄干净。
# 验证方法（管理员命令行，轮询中执行）：`powercfg -requests`
#   SYSTEM 段应有本进程，DISPLAY 段必须为空。
POWER_REQUEST_CONTEXT_VERSION = 0
POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
POWER_REQUEST_SYSTEM_REQUIRED = 2

# 兜底用：Power Request 建不出来（极老的系统）才退回执行状态标志，
# 同样只申请系统、**不加** ES_DISPLAY_REQUIRED。
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class _PRDetailed(ctypes.Structure):
    _fields_ = [("LocalizedReasonModule", ctypes.c_void_p),
                ("LocalizedReasonId", ctypes.c_ulong),
                ("ReasonStringCount", ctypes.c_ulong),
                ("ReasonStrings", ctypes.POINTER(ctypes.c_wchar_p))]


class _PRReason(ctypes.Union):
    _fields_ = [("Detailed", _PRDetailed),
                ("SimpleReasonString", ctypes.c_wchar_p)]


class _PowerReasonContext(ctypes.Structure):
    """REASON_CONTEXT。只在 SIMPLE_STRING 模式下用，字符串必须是活着的
    Python 对象 —— 所以下面把整个结构体挂进 _power_req 里一起持有。"""
    _fields_ = [("Version", ctypes.c_ulong),
                ("Flags", ctypes.c_ulong),
                ("Reason", _PRReason)]


_power_req: dict = {"handle": None, "ctx": None}


def _power_request(on: bool) -> bool:
    k32 = ctypes.windll.kernel32
    if on:
        if _power_req["handle"]:
            return True
        ctx = _PowerReasonContext()
        ctx.Version = POWER_REQUEST_CONTEXT_VERSION
        ctx.Flags = POWER_REQUEST_CONTEXT_SIMPLE_STRING
        ctx.Reason.SimpleReasonString = "币安带单跟单：轮询期间保持系统唤醒"
        k32.PowerCreateRequest.restype = ctypes.c_void_p
        h = k32.PowerCreateRequest(ctypes.byref(ctx))
        if not h:
            return False
        if not k32.PowerSetRequest(ctypes.c_void_p(h),
                                   POWER_REQUEST_SYSTEM_REQUIRED):
            k32.CloseHandle(ctypes.c_void_p(h))
            return False
        _power_req["handle"], _power_req["ctx"] = h, ctx
        return True
    h = _power_req["handle"]
    if not h:
        return True
    k32.PowerClearRequest(ctypes.c_void_p(h), POWER_REQUEST_SYSTEM_REQUIRED)
    k32.CloseHandle(ctypes.c_void_p(h))
    _power_req["handle"] = _power_req["ctx"] = None
    return True


def sleep_already_disabled() -> bool:
    """电源方案里「睡眠」已经是「从不」时返回 True（交流/直流都算）。

    这种机器上申请唤醒请求是纯冗余：系统本来就不睡，却平白多一个
    「可能有副作用」的句柄。既然不需要，就一条请求都不申请。

    用 powercfg 而不是 powrprof 的 ctypes 绑定：输出里的十六进制和 GUID 都是
    ASCII，不受系统语言影响；为读一个整数写三四十行结构体不划算。
    `STANDBYIDLE` 输出里最后两个 `0x` 值就是当前交流 / 直流设置。
    """
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(
            ["powercfg", "-query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    vals = re.findall(r"0x([0-9a-fA-F]{8})\b",
                      r.stdout.decode("ascii", "replace"))
    return len(vals) >= 2 and all(int(v, 16) == 0 for v in vals[-2:])


def set_keep_awake(on: bool) -> str:
    """申请 / 释放「保持系统唤醒」。不碰显示器。

    返回 "skip" 表示「不需要申请」：平台不是 Windows，或电源方案里睡眠
    本来就是「从不」。调用方据此决定要不要写日志解释。
    """
    if sys.platform != "win32":
        return "skip"
    if on and sleep_already_disabled():
        return "skip"
    try:
        if _power_request(on):
            return "on" if on else "off"
    except (AttributeError, OSError):
        pass
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
    except (AttributeError, OSError) as e:
        return f"fail {type(e).__name__}"
    return "on" if on else "off"


# ---------------------------------------------------------------- 窗口边框
# 窗口是无边框的，标题栏由我们画。Windows 默认不会给无边框窗口加投影，
# 用 DWM 的「扩展边框」补 1px 把它换回来，否则窗口贴在桌面上没有边界感。
def apply_frame_shadow(hwnd) -> str:
    if sys.platform != "win32" or not hwnd:
        return "skip"

    class MARGINS(ctypes.Structure):
        _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int),
                    ("t", ctypes.c_int), ("b", ctypes.c_int)]

    try:
        hr = ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
            ctypes.c_void_p(int(hwnd)), ctypes.byref(MARGINS(1, 1, 1, 1)))
    except (AttributeError, OSError) as e:
        return f"fail {type(e).__name__}"
    return f"ok hr={hr}"


class ComboBox(QComboBox):
    """带自绘箭头的下拉框。

    QSS 画不出箭头：CSS 那套 border 三角在 Qt 里会塌成一个实心方块，
    塞 SVG 又依赖 qsvg 插件。这里在 super().paintEvent() 之后补一笔折线，
    颜色取自当前色板，所以切主题时箭头跟着变（setStyleSheet 会触发重绘）。
    """

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(CURRENT.get("MUTED", "#B7BDC6"))
        if not self.isEnabled():
            color = QColor(CURRENT.get("DIM_FG", "#555C66"))
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        cx = self.width() - 15.0
        cy = self.height() / 2.0 - 1.5
        path = QPainterPath()
        path.moveTo(cx - 3.5, cy)
        path.lineTo(cx, cy + 3.5)
        path.lineTo(cx + 3.5, cy)
        painter.drawPath(path)
        painter.end()


class WinButton(QPushButton):
    """标题栏的最小化 / 最大化 / 关闭。

    背景和 hover 交给 QSS（它本来就是 QPushButton），只把符号用 QPainter 画：
    「□」「✕」这类字符在中文字体里字宽和基线都不稳，画线更可控。
    """

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(46, 34)
        self.setFocusPolicy(Qt.NoFocus)
        # hover 交给 QSS 上色；WA_Hover 让 underMouse() 在重绘时是准的
        self.setAttribute(Qt.WA_Hover, True)
        self.setProperty("role", "winclose" if kind == "close" else "winbtn")

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(CURRENT.get("TEXT", "#EAECEF"))
        if self.kind == "close" and self.underMouse():
            # 关闭按钮 hover 是红底，深色符号在上面看不清
            color = QColor("#FFFFFF")
        pen = QPen(color, 1.2)
        pen.setCapStyle(Qt.FlatCap)
        painter.setPen(pen)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        s = 4.5
        if self.kind == "min":
            painter.drawLine(QPointF(cx - s, cy + 0.5),
                             QPointF(cx + s, cy + 0.5))
        elif self.kind == "max":
            painter.drawRect(QRectF(cx - s, cy - s, s * 2, s * 2))
        elif self.kind == "restore":
            painter.drawRect(QRectF(cx - s + 1.5, cy - s, s * 1.8, s * 1.8))
            painter.drawRect(QRectF(cx - s, cy - s + 1.5, s * 1.8, s * 1.8))
        else:  # close
            painter.drawLine(QPointF(cx - s, cy - s), QPointF(cx + s, cy + s))
            painter.drawLine(QPointF(cx - s, cy + s), QPointF(cx + s, cy - s))
        painter.end()


class QueueLogHandler(logging.Handler):
    def __init__(self, out: queue.Queue):
        super().__init__(level=logging.INFO)
        self.out = out
        # 级别在表格里是独立一列，正文里再带一次 "INFO " 就是重复
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        self.out.put(("log", record.levelname, self.format(record)))


class EquityCurve(QWidget):
    """净值曲线。QPainter 自绘 —— requirements 里只有 requests + PySide6，
    为一条折线引 matplotlib 不划算，而 QChart 又要单独装 QtCharts 组件。"""

    def __init__(self):
        super().__init__()
        self._pts: list[tuple[float, float]] = []
        self._tok: dict = {}
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, pts, tok) -> None:
        self._pts = list(pts or [])
        self._tok = dict(tok or {})
        self.update()

    def paintEvent(self, _e):                      # noqa: N802 (Qt 命名)
        tok = self._tok or {}
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(tok.get("BORDER", "#333")), 1))
        p.setBrush(QColor(tok.get("PANEL", "#1b1b1f")))
        p.drawRoundedRect(r, 12, 12)
        inner = r.adjusted(12, 12, -12, -12)

        if len(self._pts) < 2:
            p.setPen(QColor(tok.get("FAINT", "#666")))
            p.drawText(inner, Qt.AlignCenter,
                       "暂无曲线 · 开始轮询后按轮记录净值"
                       if not self._pts else "再积累一条快照就出曲线")
            p.end()
            return

        vals = [v for _, v in self._pts]
        lo, hi = min(vals), max(vals)
        span = max(hi - lo, 1e-9)
        # 上下各留 12% 余量，否则线会贴着边框
        lo, hi = lo - span * 0.12, hi + span * 0.12
        n = len(self._pts)

        def xy(i: int, v: float) -> QPointF:
            return QPointF(
                inner.left() + inner.width() * (i / (n - 1)),
                inner.bottom() - inner.height() * ((v - lo) / (hi - lo)))

        p.setPen(QPen(QColor(tok.get("BORDER", "#333")), 1, Qt.DashLine))
        for k in range(1, 4):
            y = inner.top() + inner.height() * k / 4
            p.drawLine(QPointF(inner.left(), y), QPointF(inner.right(), y))

        path = QPainterPath(xy(0, vals[0]))
        for i, v in enumerate(vals[1:], 1):
            path.lineTo(xy(i, v))

        # 涨用 POS、跌用 NEG —— 本项目色板是 A 股习惯（红涨绿跌），
        # 和 theme_qss 的 POS/NEG 定义一致，别在这里反过来。
        up = vals[-1] >= vals[0]
        col = QColor(tok.get("POS" if up else "NEG", "#e05"))

        fill = QPainterPath(path)
        fill.lineTo(QPointF(inner.right(), inner.bottom()))
        fill.lineTo(QPointF(inner.left(), inner.bottom()))
        fill.closeSubpath()
        grad = QLinearGradient(0, inner.top(), 0, inner.bottom())
        c1, c2 = QColor(col), QColor(col)
        c1.setAlpha(64)
        c2.setAlpha(4)
        grad.setColorAt(0.0, c1)
        grad.setColorAt(1.0, c2)
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawPath(fill)

        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(col, 1.8))
        p.drawPath(path)

        p.setPen(QColor(tok.get("MUTED", "#888")))
        p.drawText(inner, Qt.AlignTop | Qt.AlignLeft, f"{hi:.2f}")
        p.drawText(inner, Qt.AlignBottom | Qt.AlignLeft, f"{lo:.2f}")
        p.drawText(inner, Qt.AlignTop | Qt.AlignRight, f"{vals[-1]:.2f}")
        p.end()


class App(QMainWindow):
    def __init__(self, enable_store: bool = True):
        super().__init__()
        self.setWindowTitle("币安带单跟单 · 等比复刻")
        # 无边框 + 自绘标题栏：Windows 的标题栏颜色归 DWM 管，Win10 上
        # 只能切深/浅两档、切主题时还会被系统缓存住，做不到跟 QSS 同步。
        # 自己画就彻底没有这个问题。拖动/双击最大化/四边缩放通过
        # WM_NCHITTEST 交回系统，贴边吸附（Aero Snap）照样有效。
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.resize(1180, 740)
        self.setMinimumSize(980, 600)

        self.theme = ThemeManager(QApplication.instance())
        self.theme.apply("dark")

        self.events: queue.Queue = queue.Queue()
        self.running = threading.Event()
        # running 是「是否轮询中」的标志，轮询期间一直 set；不能用它来 wait，
        # Event.wait() 在已 set 时会立刻返回。等待要用这个独立的唤醒事件。
        self._poll_wake = threading.Event()

        self.env_keys = load_env()
        self.cfg = self._load_config()
        self.log_records: deque[tuple[str, str, str]] = deque(
            maxlen=int(self.cfg["log_max_rows"]))
        self.theme.set_scheme(self.cfg.get("color_scheme", "binance"))
        self.theme.apply(self.cfg["theme"])

        self.store = None
        if enable_store:
            from state_store import StateStore
            self.store = StateStore(STATE_FILE)
        # 上一轮的快照。首轮之前也必须存在 —— 盈亏账本要拿它算「平仓前的
        # 开仓均价」，`_refresh_sim` 也直接取用，不能等到第一轮才赋值。
        self._last_positions: list = []
        self._last_fills: list = []
        self._last_pnl: dict = {}
        self._pnl_avail = 0.0          # 可用余额，只在非 dry-run 有值
        self._pnl_last_write = 0.0     # 快照写库节流
        self._pnl_last_prune = 0.0
        self.last_meta: dict = {}
        self._cfg: dict = {}
        # 真实模式下必须读到账户余额才允许开仓，见 _apply_runtime()。
        self._equity_verified = False
        self._engine_busy = False
        self._dropped_batches = 0
        self.api = None
        self._api_lock = threading.Lock()
        self._detail_cache: dict | None = None
        self._detail_ts = 0.0
        self._wallet_missing: set[str] = set()

        # 表格与播报的数据源：主题切换时要按新色板整份重涂，
        # 所以必须留原始数据，不能只依赖控件里已有的 item。
        self._fills_rows: list[tuple[tuple, str]] = []
        self._pending_rows: list[tuple[tuple, str]] = []
        # 每条操作 / 出入金是一组块。渲染时**组倒序**：最新的组画在最上面，
        # 组内保持原顺序（标题 → 明细 → 时间），否则一条播报会被拆散。
        self._announce_groups: list[list[tuple[str, str]]] = []
        self._announce_block_count = 0
        self._announced: deque[str] = deque(maxlen=200)
        self._last_sound = 0.0
        # (portfolioId, 昵称)：只有接口真的返回过昵称才填，标题据此显示
        self._lead_verified: tuple[str, str] = ("", "")
        self._lead_name_try = 0.0
        # 离屏实例（自检、截图）不发起任何网络请求
        self._net_allowed = bool(enable_store)
        # 自检会灌一整批样本事件，静音掉，免得跑一次回归响一串
        self._mute_sound = False

        self._build_widgets()
        self._wire_logging()
        self._init_runtime()
        self._on_price_mode_change()
        self._on_mode_change()
        self._refresh_cfg()
        self._redraw_log()

        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._drain_events)
        self._timer.start()

        self._log("INFO", "就绪" + ("，已恢复上次运行状态"
                                  if not self.engine.needs_baseline
                                  else "，首轮建立基线，历史仓位不跟"))
        self._log("INFO", f"跟单目标 portfolioId={self.cfg['portfolio_id']}")
        # 开机自启：配置说「开」就保证快捷方式在。正常情况只是读几个文件，
        # 只有缺失 / 指错 / 有历史残留时才起 PowerShell 重建。
        # 离屏实例（自检、截图）不碰启动文件夹。
        if enable_store and self.cfg.get("autostart"):
            state = ensure_autostart()
            if state:
                self._log("WARN" if state.startswith("fail") else "INFO",
                          f"开机自启：{state}")
        self._refresh_lead_name()

    # ---------- 配置 ----------
    def _has_credentials(self) -> bool:
        """界面里填了、或环境/.env 里存了密钥对，才算有凭证。"""
        key = self._inputs["api_key"].text().strip() if hasattr(self, "_inputs") else ""
        secret = self._inputs["api_secret"].text().strip() if hasattr(self, "_inputs") else ""
        if key and secret:
            return True
        if os.getenv("BINANCE_API_KEY") and os.getenv("BINANCE_API_SECRET"):
            return True
        if not ENV_FILE.exists():
            return False
        found = set()
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    found.add(k.strip())
        return {"BINANCE_API_KEY", "BINANCE_API_SECRET"} <= found

    def _load_config(self) -> dict:
        cfg = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                for k in DEFAULTS:
                    if k in saved:
                        cfg[k] = saved[k]
            except (json.JSONDecodeError, OSError):
                pass
        if cfg["mode"] not in MODES:
            cfg["mode"] = DEFAULTS["mode"]
        if cfg["theme"] not in THEMES:
            cfg["theme"] = DEFAULTS["theme"]
        if cfg["mode"] != "dry-run" and not self._has_credentials():
            cfg["mode"] = DEFAULTS["mode"]
        cfg["better_price_enabled"] = as_bool(cfg["better_price_enabled"], True)
        cfg["sound_enabled"] = as_bool(cfg["sound_enabled"], True)
        for k in ("auto_poll", "autostart", "keep_awake", "rebalance"):
            cfg[k] = as_bool(cfg[k], DEFAULTS[k])
        if str(cfg.get("color_scheme")).lower() not in ("binance", "cn"):
            cfg["color_scheme"] = DEFAULTS["color_scheme"]
        try:
            cfg["leverage"] = str(min(125, max(1, int(float(cfg["leverage"])))))
        except (ValueError, TypeError):
            cfg["leverage"] = DEFAULTS["leverage"]
        if not str(cfg["portfolio_id"]).strip().isdigit():
            cfg["portfolio_id"] = DEFAULTS["portfolio_id"]
        try:
            cfg["poll_interval_s"] = str(max(5, int(float(cfg["poll_interval_s"]))))
        except (ValueError, TypeError):
            cfg["poll_interval_s"] = DEFAULTS["poll_interval_s"]
        try:
            cfg["log_max_rows"] = str(max(100, int(float(cfg["log_max_rows"]))))
        except (ValueError, TypeError):
            cfg["log_max_rows"] = DEFAULTS["log_max_rows"]
        return cfg

    def _save_config(self):
        data = {}
        for k in DEFAULTS:
            if k == "theme":
                data[k] = self.theme.current
            elif k in self._checks:
                data[k] = bool(self._checks[k].isChecked())
            elif k in self._combos:
                data[k] = self._combos[k].currentText()
            elif k in self._inputs:
                data[k] = self._inputs[k].text().strip()
            elif k in ("lead_nickname", "lead_nickname_pid"):
                data[k] = self.cfg.get(k, "")
            else:
                data[k] = self.cfg.get(k, DEFAULTS.get(k, ""))
        if data.get("mode") not in MODES:
            data["mode"] = DEFAULTS["mode"]
        if data["mode"] != "dry-run" and not self._has_credentials():
            data["mode"] = "dry-run"
        try:
            data["poll_interval_s"] = str(max(5, int(float(data["poll_interval_s"]))))
        except (ValueError, TypeError):
            data["poll_interval_s"] = DEFAULTS["poll_interval_s"]
        try:
            data["log_max_rows"] = str(max(100, int(float(data["log_max_rows"]))))
        except (ValueError, TypeError):
            data["log_max_rows"] = DEFAULTS["log_max_rows"]
        pid = str(data.get("portfolio_id", "")).strip()
        if pid.isdigit() and len(pid) >= 10 and pid != self.cfg["portfolio_id"]:
            # 在别的卡片点「保存」时也可能改了 ID，别让配置和运行态对不上。
            self._switch_lead(pid, self.cfg["portfolio_id"])
        # 息屏保持：正在轮询时改配置要立刻生效，不用等下次开始
        if self.running.is_set():
            set_keep_awake(as_bool(data.get("keep_awake"), True))
        # 开机自启：只在值变了才去动启动文件夹（要起 PowerShell，别每次都调）
        want_auto = as_bool(data.get("autostart"), False)
        if want_auto != as_bool(self.cfg.get("autostart"), False):
            state = sync_autostart(want_auto)
            self._log("INFO" if not state.startswith("fail") else "WARN",
                      f"开机自启：{state}")
        self.cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
        # 配色是「立即生效」的东西：保存完还要等下一轮才变，用户会以为没生效。
        self.theme.set_scheme(self.cfg.get("color_scheme", "binance"))
        self._recolor()
        # 杠杆要立刻进 rule：仓位对账页的保证金/可用额用的就是它，
        # 等下一轮才更新的话，用户会以为改了没生效。
        try:
            self.rule.leverage = min(
                125, max(1, int(float(self.cfg.get("leverage") or 20))))
        except (TypeError, ValueError):
            self.rule.leverage = 20
        self._refresh_sim()
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(CONFIG_FILE)
        self._refresh_cfg()

    # ---------- 取值适配 ----------
    # ---------- 界面 ----------
    def _lead_display(self) -> str:
        """只显示**本次会话确认过**的带单员。

        配置文件里的 `lead_nickname` 是上次运行留下的缓存，可能是别人的、
        也可能早被改名；拿它填标题等于把旧数据当事实。确认的唯一来源是
        一次成功的接口返回（`_set_lead_nickname`），没确认就明确写「未连接」。
        """
        pid = str(self.cfg.get("portfolio_id") or "")
        v_pid, nick = self._lead_verified
        if nick and v_pid == pid:
            return f"{nick} · {pid[-6:]}"
        return "未连接"

    def _set_lead_nickname(self, nick: str) -> None:
        nick = str(nick or "").strip()
        if not nick or nick == "-":
            return
        pid = str(self.cfg.get("portfolio_id") or "")
        # 先记「本次会话已确认」，标题才有东西可显示
        self._lead_verified = (pid, nick)
        self.lead_title.setText(self._lead_display())
        if (str(self.cfg.get("lead_nickname") or "") == nick
                and str(self.cfg.get("lead_nickname_pid") or "") == pid):
            return
        self.cfg["lead_nickname"] = nick
        self.cfg["lead_nickname_pid"] = pid
        # 只写昵称两个键。绝不能调 _save_config()：它会把界面里的其它配置
        # （尤其 portfolio_id）一并写回，覆盖用户在文件里改过但当前实例还没
        # 重载的值 —— live 模式下会跟错交易员。
        try:
            data = (json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                    if CONFIG_FILE.exists() else {})
            data["lead_nickname"] = nick
            data["lead_nickname_pid"] = pid
            tmp = CONFIG_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(CONFIG_FILE)
        except (OSError, ValueError) as e:
            self._log("WARN", f"昵称缓存写入失败（不影响运行）：{e}")

    def _ensure_lead_name(self) -> None:
        """未确认时补拉昵称，节流 30 秒一次。"""
        # 自检 / 离屏渲染不许联网：那些路径用 enable_store=False 起界面
        if not self._net_allowed:
            return
        if self._lead_verified[0] == str(self.cfg.get("portfolio_id") or ""):
            return
        now = time.monotonic()
        if now - self._lead_name_try < 30:
            return
        self._lead_name_try = now
        self._refresh_lead_name()

    def _refresh_lead_name(self) -> None:
        """启动或换人后主动拉一次 detail，立刻显示名字，不等 30 秒轮询。"""
        self.lead_title.setText(self._lead_display())

        def worker():
            pid = str(self.cfg.get("portfolio_id") or "")
            try:
                from api_feed import LeadApi
                d = LeadApi(pid).detail()
                nick = d.get("nickname")
                if nick and nick != "-":
                    self.events.put(("lead_name", pid, nick))
                else:
                    self.events.put(("lead_name_error", pid, "接口未返回昵称"))
            except Exception as e:
                self.events.put(("lead_name_error", pid,
                                 f"{type(e).__name__}: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 搭界面 ----------
    def _build_widgets(self):
        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 自绘标题栏：窗口无边框，这一条由 QSS 上色，跟着主题走。
        self.titlebar = QWidget()
        self.titlebar.setObjectName("titlebar")
        # 纯 QWidget 要靠这个属性才会画 QSS 里的 background-color
        self.titlebar.setAttribute(Qt.WA_StyledBackground, True)
        self.titlebar.setFixedHeight(TITLEBAR_H)
        head = QHBoxLayout(self.titlebar)
        head.setContentsMargins(18, 0, 0, 0)
        head.setSpacing(10)
        head.addWidget(label("币安带单跟单", "h1"))
        self.lead_title = label(self._lead_display(), "accent")
        head.addWidget(self.lead_title)
        head.addStretch(1)
        self.theme_btn = QPushButton()
        self.theme_btn.setFixedWidth(104)
        self.theme_btn.setFocusPolicy(Qt.NoFocus)
        self.theme_btn.clicked.connect(self._toggle_theme)
        head.addWidget(self.theme_btn)
        head.addSpacing(8)
        # 三个窗口按钮紧贴右上角，彼此之间不留缝
        wins = QHBoxLayout()
        wins.setContentsMargins(0, 0, 0, 0)
        wins.setSpacing(0)
        self.btn_min = WinButton("min")
        self.btn_max = WinButton("max")
        self.btn_close = WinButton("close")
        self.btn_min.clicked.connect(self.showMinimized)
        self.btn_max.clicked.connect(self._toggle_max)
        self.btn_close.clicked.connect(self.close)
        for btn in (self.btn_min, self.btn_max, self.btn_close):
            wins.addWidget(btn)
        head.addLayout(wins)
        root.addWidget(self.titlebar)

        body_wrap = QWidget()
        root.addWidget(body_wrap, 1)
        body_root = QVBoxLayout(body_wrap)
        body_root.setContentsMargins(18, 12, 18, 8)
        body_root.setSpacing(12)

        # 主体：左固定 300，右自适应
        body = QHBoxLayout()
        body.setSpacing(16)
        left_area, left_inner = scroll_host()
        left_area.setFixedWidth(300)
        self._build_left(left_inner)
        body.addWidget(left_area)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(8)
        self.nb = QTabWidget()
        self.nb.setDocumentMode(True)
        right_lay.addWidget(self.nb)
        body.addWidget(right, 1)
        body_root.addLayout(body, 1)

        # 顺序按「先看结果，再看过程，最后才是设置」排：
        # 盈亏 → 播报 → 成交事件 → 挂单 → 日志 → 配置（含凭证）。
        self._build_pnl_tab()
        self._build_announce_tab()
        self._build_fills_tab()
        self._build_pending_tab()
        self._build_log_tab()
        self._build_config_tab()
        self.nb.setCurrentIndex(0)

        sb = QStatusBar()
        sb.setSizeGripEnabled(False)
        self.setStatusBar(sb)
        self.status = label("未运行 · dry-run 本地模拟")
        sb.addWidget(self.status, 1)
        self.load_label = label("")
        sb.addPermanentWidget(self.load_label)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(110)
        self.progress.setRange(0, 0)          # 不确定进度 = 来回滚动
        self.progress.setTextVisible(False)
        self.progress.hide()
        sb.addPermanentWidget(self.progress)

        self._sync_theme_button()

    def _build_left(self, host: QWidget):
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        box, bl = card(18)
        bl.addWidget(label("参数", "section"))

        self._checks = {}
        self._inputs = {}
        self._combos = {}
        self.cb_better = QCheckBox("只在更优价挂单")
        self.cb_better.setChecked(bool(self.cfg["better_price_enabled"]))
        self.cb_better.toggled.connect(self._on_price_mode_change)
        self._checks["better_price_enabled"] = self.cb_better
        bl.addSpacing(14)
        bl.addWidget(self.cb_better)

        # 两个输入框叠在同一位置，只显示当前口径对应的那个。
        # 不用一个框切 textvariable：切回来时要保住另一边的值。
        self._pct_cap = label("", "caption")
        bl.addSpacing(14)
        bl.addWidget(self._pct_cap)
        bl.addSpacing(5)
        self.edit_improve = QLineEdit(str(self.cfg["improve_pct"]))
        self.edit_improve.setFont(NUM_FONT)
        self.edit_improve.setFixedWidth(110)
        self.edit_slip = QLineEdit(str(self.cfg["slippage_tol_pct"]))
        self.edit_slip.setFont(NUM_FONT)
        self.edit_slip.setFixedWidth(110)
        self._inputs["improve_pct"] = self.edit_improve
        self._inputs["slippage_tol_pct"] = self.edit_slip
        bl.addWidget(self.edit_improve)
        bl.addWidget(self.edit_slip)
        bl.addSpacing(6)
        self._pct_help = label("", "help")
        self._pct_help.setWordWrap(True)
        bl.addWidget(self._pct_help)
        bl.addSpacing(12)
        self.mode_badge = label("", "accent")
        bl.addWidget(self.mode_badge)

        bl.addSpacing(22)
        bl.addWidget(label("执行模式", "section"))
        bl.addSpacing(8)
        self.mode_box = ComboBox()
        self.mode_box.setFixedWidth(150)
        self.mode_box.addItems(["dry-run"])
        self.mode_box.setCurrentText(self.cfg["mode"])
        self.mode_box.currentTextChanged.connect(lambda _: self._on_mode_change())
        self._combos["mode"] = self.mode_box
        bl.addWidget(self.mode_box)
        bl.addSpacing(6)
        self._mode_help = label("", "help")
        self._mode_help.setWordWrap(True)
        bl.addWidget(self._mode_help)
        lay.addWidget(box)

        btns = QWidget()
        blay = QVBoxLayout(btns)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(8)
        # 开始/停止合成一个开关按钮：状态唯一，不会误点两次。
        self.poll_btn = QPushButton("开始轮询")
        self.poll_btn.setProperty("role", "accent")
        self.poll_btn.setMinimumHeight(34)
        self.poll_btn.clicked.connect(self.toggle_polling)
        blay.addWidget(self.poll_btn)

        grid = QHBoxLayout()
        grid.setSpacing(8)
        col_a = QVBoxLayout()
        col_a.setSpacing(8)
        col_b = QVBoxLayout()
        col_b.setSpacing(8)
        # 「单次运行」删了：轮询才是正常用法，单次取数属于调试动作，
        # 「强制刷新」已经覆盖。清日志在日志页自己有按钮，左栏不该重复。
        for i, (text, cmd) in enumerate((("强制刷新", self.force_refresh),
                                         ("保存配置", self._save_config))):
            btn = QPushButton(text)
            btn.setMinimumHeight(32)
            btn.clicked.connect(cmd)
            (col_a if i % 2 == 0 else col_b).addWidget(btn)
        grid.addLayout(col_a, 1)
        grid.addLayout(col_b, 1)
        blay.addLayout(grid)
        lay.addWidget(btns)
        lay.addStretch(1)

    def _on_mode_change(self):
        has = self._has_credentials()
        values = list(MODES) if has else ["dry-run"]
        self.mode_box.blockSignals(True)
        current = self.mode_box.currentText()
        self.mode_box.clear()
        self.mode_box.addItems(values)
        if current in values:
            self.mode_box.setCurrentText(current)
        else:
            self.mode_box.setCurrentIndex(0)
        self.mode_box.blockSignals(False)
        mode = self.mode_box.currentText()
        tip = MODE_TEXT.get(mode, "")
        if not has:
            tip += " 填写 API Key 后可切换测试网 / 实盘。"
        self._mode_help.setText(tip)
        # 状态栏原本硬编码「未运行 · dry-run 本地模拟」，只有跑过一轮才会被
        # 覆盖。切到 live 但还没运行时这里会一直显示 dry-run，看着像没生效。
        if hasattr(self, "status") and not self.running.is_set():
            self.status.setText(
                f"未运行 · {mode} {MODE_SHORT.get(mode, '')}".strip())
        # 口径要跟着模式立刻变：切到 live 还显示「模拟资金 1000」正是
        # 之前那个老毛病。切换当下就重算一次，不等下一轮快照。
        if hasattr(self, "_sim_rows"):
            self._refresh_sim()

    def _build_account_into(self, lay):
        """账户状态 + API 凭证。并进「运行配置」页，不再单独占一个 tab ——
        凭证本来就是配置的一部分，单开一页只会让人多切一次。"""

        box, bl = card(16)
        bl.addWidget(label("运行概览", "section"))
        bl.addSpacing(14)
        # 三列两行：五个指标塞一行会把钱包余额挤成两行。
        # 必须用 QGridLayout —— QHBoxLayout.addLayout() 是横向追加，
        # 会把「两行」并成一行。
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        bl.addLayout(grid)
        self.metrics: dict[str, QLabel] = {}
        items = (("conn", "账号连接"), ("equity", "我的权益"),
                 ("balance", "带单员钱包"), ("unreal", "带单员浮盈亏"),
                 ("pending", "我的挂单"))
        for i, (key, cap) in enumerate(items):
            row, col = divmod(i, 3)
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(2)
            cl.addWidget(label(cap, "caption"))
            lab = label("-", "metric")
            cl.addWidget(lab)
            self.metrics[key] = lab
            grid.addWidget(cell, row, col)
        for col in range(3):
            grid.setColumnStretch(col, 1)
        lay.addWidget(box)

        form_box, fl = card(16)
        fl.addWidget(label("添加 API Key", "section"))
        fl.addSpacing(14)
        fl.addWidget(label("API Key", "caption"))
        fl.addSpacing(5)
        self.edit_key = QLineEdit(os.getenv("BINANCE_API_KEY", ""))
        self.edit_key.setEchoMode(QLineEdit.Password)
        self.edit_key.setFont(NUM_FONT)
        self._inputs["api_key"] = self.edit_key
        fl.addWidget(self.edit_key)
        fl.addSpacing(10)
        fl.addWidget(label("API Secret", "caption"))
        fl.addSpacing(5)
        self.edit_secret = QLineEdit(os.getenv("BINANCE_API_SECRET", ""))
        self.edit_secret.setEchoMode(QLineEdit.Password)
        self.edit_secret.setFont(NUM_FONT)
        self._inputs["api_secret"] = self.edit_secret
        fl.addWidget(self.edit_secret)
        fl.addSpacing(14)
        connect_btn = QPushButton("保存并连接")
        connect_btn.setProperty("role", "accent")
        connect_btn.setFixedWidth(120)
        connect_btn.setMinimumHeight(32)
        connect_btn.clicked.connect(self._connect_account)
        wrap = QHBoxLayout()
        wrap.addWidget(connect_btn)
        wrap.addStretch(1)
        fl.addLayout(wrap)
        fl.addSpacing(12)
        self.account_label = label("未连接 — 填入 Key 后点「保存并连接」。")
        self.account_label.setWordWrap(True)
        fl.addWidget(self.account_label)
        fl.addSpacing(12)
        tip = label("币安网页 → 头像 → 账户 → API 管理 → 创建 API Key。"
                    "权限勾「允许读取」+「允许合约」，IP 白名单填本机公网 IP。"
                    "凭证只写入本机 .env。", "help")
        tip.setWordWrap(True)
        fl.addWidget(tip)
        lay.addWidget(form_box)

    def _table(self, heads, widths, aligns,
               stretch_last: bool = True) -> QTableWidget:
        t = QTableWidget(0, len(heads))
        t.setHorizontalHeaderLabels(list(heads))
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(32)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.setWordWrap(False)
        # item:hover 要高亮整行就得开鼠标跟踪，否则只有按住才触发
        t.setMouseTracking(True)
        header = t.horizontalHeader()
        header.setHighlightSections(False)
        header.setFixedHeight(34)
        for i, w in enumerate(widths):
            # Stretch 的列宽由剩余空间决定，用户拖不动它；需要「每列都能拖」
            # 的表格（成交事件）传 stretch_last=False，全部走 Interactive。
            if stretch_last and i == len(widths) - 1:
                header.setSectionResizeMode(i, QHeaderView.Stretch)
            else:
                header.setSectionResizeMode(i, QHeaderView.Interactive)
                t.setColumnWidth(i, w)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # 表头跟着列对齐走：数字列右对齐时表头还靠左，读起来会错位
        for i, align in enumerate(aligns):
            item = t.horizontalHeaderItem(i)
            if item is not None:
                item.setTextAlignment(align)
        t._aligns = aligns
        return t

    def _mk_item(self, text, align, mono=False, color=None,
                 tip=None) -> QTableWidgetItem:
        it = QTableWidgetItem(str(text))
        it.setTextAlignment(align)
        # 长文本在列宽内会被截成「…」，悬停给全文 —— 事件日志的动作行
        # 和信息量大的告警都属于这一类。
        if tip:
            it.setToolTip(tip)
        if mono:
            it.setFont(NUM_FONT)
        if color:
            it.setForeground(QColor(color))
        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return it

    def _build_fills_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)
        self.fills_summary = label("暂无成交事件 · 点击「单次运行」取数")
        lay.addWidget(self.fills_summary)
        lay.addWidget(hline())
        self.tree_fills = self._table(
            ("时间", "开/平", "方向", "品种", "均价", "数量", "名义 USDT",
             "应开 USDT", "实开 USDT"),
            (124, 56, 46, 96, 96, 88, 96, 88, 88),
            (Qt.AlignLeft, Qt.AlignHCenter, Qt.AlignHCenter, Qt.AlignLeft,
             Qt.AlignRight, Qt.AlignRight, Qt.AlignRight,
             Qt.AlignRight, Qt.AlignRight),
            stretch_last=False)
        lay.addWidget(self.tree_fills, 1)
        self.nb.addTab(page, "成交事件")

    def _build_sim_page(self) -> QWidget:
        """仓位管理：用模拟资金把「应开 / 实开」摊开算一遍。

        实盘上验证等比缩放要么真下单、要么把资金调大，都有代价。这里用
        固定假资金离线重放，和引擎调同一个 `target_position()`，所以算出来
        的就是实盘会下的数；真实列取自交易所，差额一眼能看出该开没开。
        """
        # 不用 scroll_host()：外面再套一层滚动区，表格拿到的是滚动区内容宽，
        # 比可见宽度窄一截，列会被挤成省略号。这里和「成交事件」页一样直接铺。
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        box, bl = card(14)
        self.sim_title = label("仓位对账", "h2")
        bl.addWidget(self.sim_title)
        bl.addSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.sim_eq_cap = label("模拟资金", "caption")
        # 固定宽度：「模拟资金」4 字、「本程序权益」5 字，宽度一变输入框就位移
        self.sim_eq_cap.setFixedWidth(78)
        row.addWidget(self.sim_eq_cap)
        self.edit_sim_equity = QLineEdit("1000")
        self.edit_sim_equity.setFixedWidth(96)
        self.edit_sim_equity.setAlignment(Qt.AlignRight)
        self.edit_sim_equity.editingFinished.connect(self._refresh_sim)
        # 手动改数字 = 放弃跟随实盘权益
        self.edit_sim_equity.textEdited.connect(self._sim_unpin)
        row.addWidget(self.edit_sim_equity)
        row.addWidget(label("USDT", "help"))
        row.addSpacing(12)
        btn_sim = QPushButton("重算")
        btn_sim.setFixedWidth(72)
        btn_sim.clicked.connect(self._refresh_sim)
        row.addWidget(btn_sim)
        btn_real = QPushButton("按实盘权益")
        btn_real.setFixedWidth(96)
        btn_real.setToolTip("把模拟资金设成当前实盘权益，这时「差额」列才是"
                            "真正可比的")
        btn_real.clicked.connect(self._sim_use_real_equity)
        row.addWidget(btn_real)
        btn_reset = QPushButton("清空")
        btn_reset.setFixedWidth(72)
        btn_reset.clicked.connect(self._reset_sim)
        row.addWidget(btn_reset)
        row.addStretch(1)
        bl.addLayout(row)
        bl.addSpacing(8)

        self.sim_summary = label("等待数据 · 点「单次运行」或开始轮询", "caption")
        # 摘要一行放不下（要带缩放系数、保证金、权益差异），必须允许换行，
        # 否则卡片右侧会把它直接截掉。
        self.sim_summary.setWordWrap(True)
        # 行数固定：文案在「模拟 / 实盘」之间切换时长度不同，按内容撑高会让
        # 下面整张表跟着上下跳 —— 点一次「重算」跳一次，很难看。
        _fm = self.sim_summary.fontMetrics()
        self.sim_summary.setFixedHeight(_fm.lineSpacing() * 3 + 6)
        bl.addWidget(self.sim_summary)
        bl.addSpacing(4)
        self.sim_help = label("", "help")
        self.sim_help.setWordWrap(True)
        _fmh = self.sim_help.fontMetrics()
        self.sim_help.setFixedHeight(_fmh.lineSpacing() * 3 + 6)
        bl.addWidget(self.sim_help)
        lay.addWidget(box)
        lay.addSpacing(4)

        self.tree_sim = self._table(
            ("品种", "方向", "带单员名义", "带单员占比", "应开名义", "应开数量",
             "模拟数量", "真实名义", "本程序占比", "差额"),
            (82, 34, 88, 72, 78, 76, 76, 78, 72, 76),
            (Qt.AlignLeft, Qt.AlignHCenter, Qt.AlignRight, Qt.AlignRight,
             Qt.AlignRight, Qt.AlignRight, Qt.AlignRight, Qt.AlignRight,
             Qt.AlignRight, Qt.AlignRight),
            stretch_last=False)
        self.tree_sim.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        lay.addWidget(self.tree_sim, 1)
        lay.addStretch(1)

        self._sim_pin_real = False      # 是否锁定为「跟随本程序权益」
        self._sim_fit_width = 0         # 上次自适应列宽时的视口宽度
        self._sim_rows: list = []
        self._last_fills: list = []
        self._sim_ticks: dict = {}
        return page

    # ---------- 模拟仓 ----------
    def _reset_sim(self):
        self._sim_rows = []
        self._sim_ticks.clear()
        self._render_sim()
        self.sim_summary.setText("已清空 · 点「重算」按当前数据重跑")

    def _sim_use_real_equity(self, *_):
        """把资金基数对齐本程序权益，并**锁定跟随**。

        资金不一样时「差额」列没有可比性 —— 1000 U 的模拟仓对 92 U 的实盘，
        差额永远是正的，那是资金差不是漏单。

        锁定而不是只改一次数字：权益每轮都在变（浮盈），写死一个 92.63 之后
        只要权益波动超过 1%，口径判断就会切回「模拟资金」，看起来像按钮失效。
        锁定后每轮直接取最新权益，口径恒定。想手动填数字，编辑输入框即可解锁。
        """
        equity = float(self.rule.my_equity or 0.0)
        if equity <= 0:
            self.sim_summary.setText("读不到本程序权益，请先取一次数")
            return
        self._sim_pin_real = True
        self.edit_sim_equity.setText(f"{equity:.2f}")
        self._refresh_sim()

    def _sim_unpin(self, *_):
        """手动改资金 = 切回模拟口径。"""
        if self._sim_pin_real:
            self._sim_pin_real = False
            self._refresh_sim()

    def _symbol_steps(self, symbol: str) -> tuple[float, float]:
        """(tick, step)。取不到就退回 0.01 / 0.001 —— 宁可粗一点，也别让
        整张表因为一个品种的精度查不到就空白。"""
        if symbol in self._sim_ticks:
            return self._sim_ticks[symbol]
        tick, step = 0.01, 0.001
        try:
            tick = self.executor.price_step_of(symbol) or tick
        except Exception:
            pass
        try:
            _, qp = self.broker.get_symbol_precision(symbol)
            step = 10.0 ** (-qp)
        except Exception:
            pass
        self._sim_ticks[symbol] = (tick, step)
        return tick, step

    # 旧名保留：面板里还有引用
    _sim_step_of = _symbol_steps

    def _refresh_sim(self, *_):
        fills = self._last_fills
        wallet = (self.engine.lead_wallet or self.rule.lead_balance or 0.0)
        real_eq_now = float(self.rule.my_equity or 0.0)
        # 口径不只看「有没有点按实盘权益」：执行模式一旦不是 dry-run，
        # 仓位就是真实仓位，再显示「模拟资金 1000」就是错的。
        live_mode = self._sim_pin_real or self.mode_box.currentText() != "dry-run"
        if live_mode and real_eq_now > 0:
            # 跟随模式：每轮取最新权益，并把输入框同步成当前值
            self.edit_sim_equity.blockSignals(True)
            self.edit_sim_equity.setText(f"{real_eq_now:.2f}")
            self.edit_sim_equity.blockSignals(False)
        try:
            equity = float(self.edit_sim_equity.text().strip() or 1000.0)
        except ValueError:
            equity = 1000.0
        equity = max(0.0, equity)
        if not fills or wallet <= 0 or equity <= 0:
            self._sim_rows = []
            self._render_sim()
            self.sim_summary.setText(
                "等待数据 · 需要一次成功取数（带单员成交与钱包余额）")
            return
        real_eq = float(self.rule.my_equity or 0.0)
        # 口径由上一步算好的 live_mode 决定，不再靠数值接近去猜 —— 猜的后果
        # 是权益波动 1% 就来回切换，用户看到的就是「刚开成实盘又变回模拟」。
        base_name = "本程序权益" if live_mode else "模拟资金"
        self.sim_title.setText("仓位对账 · 实盘口径" if live_mode
                               else "仓位对账 · 模拟资金")
        self.sim_eq_cap.setText("本程序权益" if live_mode else "模拟资金")
        self.sim_help.setText(
            f"应开 = 带单员累计数量 ×（{base_name} ÷ 带单员钱包），按步进向下取整；"
            f"名义 = 应开数量 × 限价。两列占比分别是「名义 ÷ 带单员钱包」与"
            f"「名义 ÷ {base_name}」，可直接对比杠杆水平。"
            f"差额 =（应开数量 − 真实数量）× 应开均价，正数表示应开未开，"
            f"负数表示超额持有。")
        sim = SimPortfolio(equity, self.rule.leverage)
        res = sim.replay(fills, wallet,
                         lambda s: self._sim_step_of(s)[0],
                         lambda s: self._sim_step_of(s)[1],
                         self.rule)
        # 用引擎线程取回来的那份持仓，不在界面线程再打一次网络
        real = {}
        for p in (self._last_positions or []):
            qty = abs(float(p.get("quantity", 0) or 0))
            px = abs(float(p.get("entry_price", 0) or 0))
            real[p["symbol"]] = (qty, px, qty * px, p.get("side"))
        real_eq = float(self.rule.my_equity or 0.0)

        def share(notional: float) -> str:
            """名义占本程序权益的比例，和带单员那列同一个口径。"""
            return f"{notional / real_eq * 100:.1f}%" if real_eq > 0 else "—"

        rows = []
        seen = set()
        for leg in res.legs:
            if leg.lead_notional <= 0 and leg.qty <= 0:
                continue
            seen.add(leg.symbol)
            r_qty, _px, r_notional, _sd = real.get(
                leg.symbol, (0.0, 0.0, 0.0, None))
            # 差额按「数量差 × 应开均价」算，不拿两个名义直接相减 ——
            # 名义还含成交价差异，价格一变就会显示成「少开了」，其实是同一手数。
            gap = (leg.qty - r_qty) * leg.avg if leg.avg else 0.0
            # 少开和多开都是偏差，统一用警告色；靠正负号区分方向。
            # 用 POS（绿）会让「应开未开」看起来像盈利。
            role = "WARN" if abs(gap) > 0.5 else ""
            rows.append((abs(gap), ((
                leg.symbol, "多" if leg.side == "LONG" else "空",
                f"{leg.lead_notional:.2f}", f"{leg.ratio * 100:.1f}%",
                f"{leg.target_notional:.2f}", _qty(leg.target_qty),
                _qty(leg.qty), f"{r_notional:.2f}", share(r_notional),
                f"{gap:+.2f}"), role)))
        # 孤儿仓：本程序持有、带单员账本中没有 —— 差额全负，即超额持有部分
        for symbol, (qty, px, notional, side) in real.items():
            if symbol in seen or notional <= 0:
                continue
            rows.append((abs(notional), ((
                symbol, "多" if side == "LONG" else "空",
                "0.00", "0.0%", "0.00", "0",
                "0", f"{notional:.2f}", share(notional),
                f"{-notional:+.2f}"), "WARN")))
        # 偏差大的排前面：这一页是排查用的，最该看的先出现
        rows.sort(key=lambda x: -x[0])
        self._sim_rows = [r for _g, r in rows]
        self._render_sim()
        used = res.margin_used(self.rule.leverage)
        gap_note = ""
        if not live_mode and real_eq > 0:
            gap_note = (f" · ⚠ 实盘权益 {real_eq:.2f} U，与模拟资金差 "
                        f"{abs(equity - real_eq):.2f} U，「差额」列仅供参照")
        # 两边总敞口放在同一行，比逐个品种看更快判断是否超配
        lead_total = sum(l.lead_notional for l in res.legs)
        mine_total = sum(v[2] for v in real.values())
        lead_lev = f"{lead_total / wallet:.2f}×" if wallet > 0 else "—"
        mine_lev = f"{mine_total / real_eq:.2f}×" if real_eq > 0 else "—"
        self.sim_summary.setText(
            f"{base_name} {equity:.2f} U · 带单员钱包 {wallet:.2f} U · "
            f"缩放 {equity / wallet:.6f} · 总敞口 带单员 {lead_lev} / "
            f"本程序 {mine_lev} · "
            f"{'应开' if live_mode else '模拟'}总名义 "
            f"{res.total_notional:.2f} U · "
            f"占用保证金 {used:.2f} U · 可用 {equity - used:.2f} U{gap_note}")

    def _render_sim(self):
        t = self.theme.tokens()
        self.tree_sim.setRowCount(len(self._sim_rows))
        aligns = self.tree_sim._aligns
        for r, (vals, role) in enumerate(self._sim_rows):
            color = t.get(role) if role else None
            for c, v in enumerate(vals):
                self.tree_sim.setItem(
                    r, c, self._mk_item(v, aligns[c], mono=c >= 2,
                                        color=color))
        # 列宽按内容自适应：数值宽度随品种变化（0.052 vs 10.829），
        # 写死宽度不是截断就是浪费。表头也算进去，否则「应开数量」会被截。
        avail = self.tree_sim.viewport().width()
        # 只在首次或窗口宽度明显变化时重排列宽：每次刷新都按内容重排，
        # 数字位数一变列宽就抖，点「重算」整张表会横向跳动。
        # 列宽用固定基线，只在窗口明显变窄时按比例压缩数字列。
        # 不用 resizeColumnsToContents：数字位数每轮都在变，列宽跟着变，
        # 点一次「重算」整张表横向抖一下。
        if self._sim_rows and abs(avail - self._sim_fit_width) > 40:
            base = [104, 46, 100, 82, 78, 78, 78, 80, 82, 72]
            widths = base[:self.tree_sim.columnCount()]
            total = sum(widths)
            if total > avail > 0:
                # 品种 / 方向不压 —— 压了「NVDAUSDT」会变成「NVDAUS…」
                flex = list(range(2, len(widths)))
                flex_total = sum(widths[i] for i in flex) or 1
                over = total - avail
                for i in flex:
                    widths[i] = max(54, int(widths[i]
                                            - over * widths[i] / flex_total))
            for c, w in enumerate(widths):
                self.tree_sim.setColumnWidth(c, w)
            self._sim_fit_width = avail

    # ---------- 跟单盈亏 ----------
    def _build_pnl_tab(self):
        """盈亏页：净值曲线 + 持仓浮盈 + 平仓明细。

        数据全部来自 state.db 的 `pnl_*` 三张表，按 `mode` 隔离 ——
        dry-run 的持仓是 MemoryBroker 模拟出来的，和实盘混在一条曲线上
        就是假数据，切模式时曲线必须重画而不是接上。
        """
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        box, bl = card(14)
        head = QHBoxLayout()
        head.addWidget(label("跟单盈亏", "h2"))
        head.addStretch(1)
        self.pnl_mode = label("", "caption")
        head.addWidget(self.pnl_mode)
        bl.addLayout(head)
        bl.addSpacing(8)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.pnl_metrics: dict[str, QLabel] = {}
        for i, (key, cap) in enumerate(
                (("net", "净值（钱包 + 浮盈）"), ("unreal", "浮动盈亏"),
                 ("realized", "已实现盈亏"), ("total", "累计盈亏"),
                 ("mdd", "最大回撤"),
                 ("lead", "带单员钱包 / 浮盈亏"))):
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(2)
            cl.addWidget(label(cap, "caption"))
            lab = label("-", "metric")
            cl.addWidget(lab)
            self.pnl_metrics[key] = lab
            grid.addWidget(cell, 0, i)
            grid.setColumnStretch(i, 1)
        bl.addLayout(grid)
        bl.addSpacing(6)
        self.pnl_note = label("", "help")
        self.pnl_note.setWordWrap(True)
        self.pnl_note.setFixedHeight(
            self.pnl_note.fontMetrics().lineSpacing() * 2 + 6)
        bl.addWidget(self.pnl_note)
        lay.addWidget(box)

        self.pnl_curve = EquityCurve()
        lay.addWidget(self.pnl_curve, 3)

        # 两张表上下排，不并排：并排时左右各只有 300 多像素，7 列宽度
        # 放不下就会出横向滚动条，数字全被挤成「…」。
        lay.addWidget(label("当前持仓浮盈", "caption"))
        self.tree_pnl_pos = self._table(
            ("品种", "方向", "数量", "开仓价", "标记价", "浮盈", "名义"),
            (92, 36, 88, 96, 96, 92, 92),
            (Qt.AlignLeft, Qt.AlignHCenter, Qt.AlignRight, Qt.AlignRight,
             Qt.AlignRight, Qt.AlignRight, Qt.AlignRight),
            stretch_last=True)
        self.tree_pnl_pos.setMinimumHeight(96)
        lay.addWidget(self.tree_pnl_pos, 2)

        lay.addWidget(label("平仓 / 撤单明细（盈亏按平仓时刻标记价估算）",
                            "caption"))
        self.tree_pnl_trade = self._table(
            ("时间", "动作", "品种", "方向", "数量", "价格", "盈亏"),
            (108, 54, 92, 36, 82, 96, 92),
            (Qt.AlignLeft, Qt.AlignHCenter, Qt.AlignLeft,
             Qt.AlignHCenter, Qt.AlignRight, Qt.AlignRight, Qt.AlignRight),
            stretch_last=True)
        self.tree_pnl_trade.setMinimumHeight(96)
        lay.addWidget(self.tree_pnl_trade, 2)

        # 概览与「仓位对账」合成一个 tab 的两个子页：
        # 上面看盈亏，切一格看「应开 / 实开」的差值，符合「一个仓位页」
        # 的心智，不再让人在两个 tab 之间来回跳。
        self.pnl_tabs = QTabWidget()
        self.pnl_tabs.setDocumentMode(True)
        self.pnl_tabs.addTab(page, "概览")
        self.pnl_tabs.addTab(self._build_sim_page(), "仓位对账")
        outer = QWidget()
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.addWidget(self.pnl_tabs)
        self.nb.addTab(outer, "跟单盈亏")

    def _render_pnl(self, pnl: dict | None):
        pnl = pnl or {}
        tok = self.theme.tokens()
        curve = pnl.get("curve") or []
        # 曲线画净值：钱包余额 + 浮动盈亏。只画余额的话，一次平仓会让曲线
        # 凭空下蹲（浮盈消失、钱进了钱包），看着像亏了。
        self.pnl_curve.set_data(
            [(p.get("ts") or 0.0,
              (p.get("equity") or 0.0) + (p.get("unreal") or 0.0))
             for p in curve], tok)
        st = curve_stats(curve)
        realized = float(pnl.get("realized") or 0.0)
        total = st["pnl"] + realized
        mode = pnl.get("mode") or "dry-run"
        self.pnl_mode.setText(
            {"dry-run": "模拟盘 · 不入实盘账",
             "testnet": "测试网", "live": "实盘"}.get(mode, mode))

        def put(key: str, text: str, val: float | None = None):
            lab = self.pnl_metrics[key]
            lab.setText(text)
            if val is not None:
                lab.setProperty("role", "pos" if val > 0 else
                                ("neg" if val < 0 else ""))
                repolish(lab)

        put("net", f"{st['last']:.2f}" if st["n"] else "-")
        put("unreal", f"{(pnl.get('unreal') or 0.0):+.2f}",
            float(pnl.get("unreal") or 0.0))
        put("realized", f"{realized:+.2f}", realized)
        put("total", f"{total:+.2f}" if st["n"] else "-",
            total if st["n"] else None)
        put("mdd", f"{st['mdd']:.2f} ({st['mdd_pct']:.1f}%)" if st["n"] else "-")
        # 带单员侧的数字同屏显示：看「他在赚我却在亏」这种背离比看单边有用。
        if curve:
            last = curve[-1]
            lw = float(last.get("lead_wallet") or 0.0)
            lu = float(last.get("lead_unreal") or 0.0)
            le = float(last.get("lead_exposure") or 0.0)
            # 六列并排，宽度有限：主数字给整数位，小数进 tooltip
            put("lead", f"{lw:,.0f} / {lu:+,.0f}" if lw > 0 else "-")
            if lw > 0:
                self.pnl_metrics["lead"].setToolTip(
                    f"带单员钱包 {lw:.2f} USDT（已剔除浮盈）\n"
                    f"未实现盈亏 {lu:+.2f} USDT\n"
                    f"总敞口 {le:.2f} USDT"
                    + (f"（{le / lw:.2f}× 钱包）" if lw > 0 else ""))
        else:
            put("lead", "-")

        if st["n"]:
            self.pnl_note.setText(
                f"记录 {st['n']} 个快照 · 期初 {st['first']:.2f} → 现 "
                f"{st['last']:.2f} · 区间 {st['pnl_pct']:+.2f}% · "
                f"峰值 {st['peak']:.2f}。已实现按本程序自己的平仓累加，"
                f"不含手动划转；浮亏未实现的部分不进已实现。")
        else:
            self.pnl_note.setText(
                "还没有快照。开始轮询后每 30 秒记一条；"
                "dry-run 与实盘分开存，切模式不会串数据。")

        rows = pnl.get("pos_rows") or []
        self.tree_pnl_pos.setRowCount(len(rows))
        aligns = self.tree_pnl_pos._aligns
        for r, p in enumerate(rows):
            v = float(p.get("unreal") or 0.0)
            col = tok["POS"] if v > 0 else (tok["NEG"] if v < 0 else None)
            cells = [p.get("symbol", ""), "多" if p.get("side") == "LONG"
                     else "空", _qty(p.get("qty") or 0.0),
                     f"{(p.get('entry') or 0.0):.5f}",
                     f"{(p.get('mark') or 0.0):.5f}",
                     f"{v:+.2f}", f"{(p.get('notional') or 0.0):.2f}"]
            for c, text in enumerate(cells):
                self.tree_pnl_pos.setItem(r, c, self._mk_item(
                    text, aligns[c], mono=c >= 2,
                    color=col if c == 5 else None))

        trades = pnl.get("trades") or []
        self.tree_pnl_trade.setRowCount(len(trades))
        aligns = self.tree_pnl_trade._aligns
        for r, t in enumerate(trades):
            rz = t.get("realized")
            col = None
            if rz is not None:
                col = tok["POS"] if rz > 0 else (tok["NEG"] if rz < 0 else None)
            cells = [datetime.fromtimestamp(t["ts"]).strftime("%m-%d %H:%M:%S")
                     if t.get("ts") else "",
                     {"OPEN": "开仓", "CLOSE": "平仓",
                      "CANCEL": "撤单"}.get(t.get("kind"), t.get("kind") or ""),
                     t.get("symbol") or "", "多" if t.get("side") == "LONG"
                     else "空", _qty(t.get("qty") or 0.0),
                     f"{(t.get('price') or 0.0):.5f}",
                     "—" if rz is None else f"{rz:+.2f}"]
            for c, text in enumerate(cells):
                self.tree_pnl_trade.setItem(r, c, self._mk_item(
                    text, aligns[c], mono=c >= 4, color=col if c == 6 else None,
                    tip=t.get("note") or None))

    def _build_pending_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)
        self.pending_summary = label("当前无未成交挂单")
        lay.addWidget(self.pending_summary)
        lay.addWidget(hline())
        self.tree_pending = self._table(
            ("品种", "方向", "数量", "挂单价", "杠杆", "已挂秒", "模式"),
            (120, 60, 95, 120, 55, 85, 105),
            (Qt.AlignLeft, Qt.AlignHCenter, Qt.AlignRight, Qt.AlignRight,
             Qt.AlignRight, Qt.AlignRight, Qt.AlignLeft))
        lay.addWidget(self.tree_pending, 1)
        self.nb.addTab(page, "未成交挂单")

    def _build_log_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        filt = QHBoxLayout()
        filt.setSpacing(6)
        filt.addWidget(label("级别", "caption"))
        self.log_level = ComboBox()
        self.log_level.addItems(["ALL", "INFO", "WARN", "ERROR", "ACTION"])
        self.log_level.setFixedWidth(100)
        self.log_level.currentTextChanged.connect(lambda _: self._redraw_log())
        self._combos["log_level"] = self.log_level
        filt.addWidget(self.log_level)
        filt.addSpacing(10)
        filt.addWidget(label("关键字", "caption"))
        self.log_search = QLineEdit()
        self.log_search.setFixedWidth(200)
        self.log_search.textChanged.connect(lambda _: self._redraw_log())
        filt.addWidget(self.log_search)
        filt.addStretch(1)
        clear_btn = QPushButton("清空日志")
        clear_btn.setFixedWidth(92)
        clear_btn.clicked.connect(self.clear_log)
        filt.addWidget(clear_btn)
        lay.addLayout(filt)
        lay.addWidget(hline())

        self.tree_log = self._table(
            ("时间", "级别", "事件"), (85, 82, 620),
            (Qt.AlignLeft, Qt.AlignHCenter, Qt.AlignLeft))
        lay.addWidget(self.tree_log, 1)
        lay.addWidget(hline())
        self.log_status = label("", "caption")
        lay.addWidget(self.log_status)
        self.nb.addTab(page, "事件日志")

    def _build_config_tab(self):
        area, host = scroll_host()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        def make_card(title: str, sub: str) -> QVBoxLayout:
            box, bl = card(14)
            bl.addWidget(label(title, "section"))
            bl.addSpacing(6)
            help_lab = label(sub, "help")
            help_lab.setWordWrap(True)
            bl.addWidget(help_lab)
            bl.addSpacing(12)
            lay.addWidget(box)
            return bl

        self._build_account_into(lay)
        self._config_lay = lay

        lead = make_card(
            "跟单目标",
            "币安带单本链接里的纯数字 portfolioId。")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.edit_pid = QLineEdit(str(self.cfg["portfolio_id"]))
        self.edit_pid.setFont(NUM_FONT)
        self.edit_pid.setFixedWidth(230)
        self._inputs["portfolio_id"] = self.edit_pid
        row.addWidget(self.edit_pid)
        apply_btn = QPushButton("应用并重置水位")
        apply_btn.setProperty("role", "accent")
        apply_btn.setFixedWidth(146)
        apply_btn.clicked.connect(self._apply_lead)
        row.addWidget(apply_btn)
        row.addStretch(1)
        lead.addLayout(row)
        lead.addSpacing(12)
        self._lead_label = label(f"当前：{self.cfg['portfolio_id']}", "caption")
        lead.addWidget(self._lead_label)
        lead.addSpacing(6)
        note = label("换人后清空去重水位并重新建立基线，不会补开新目标的历史单；"
                     "我账户里已有的挂单继续由引擎管理。", "help")
        note.setWordWrap(True)
        lead.addWidget(note)

        freq = make_card(
            "抓数频率",
            "每 N 秒拉一次全量成交历史。币安对 trade-history 有 11012005 限流，"
            "太快反而拿不到数据；接口忙时程序会自动退避重连，事件靠全量历史补回。")
        frow = QHBoxLayout()
        frow.setSpacing(8)
        self.edit_poll = QLineEdit(str(self.cfg["poll_interval_s"]))
        self.edit_poll.setFont(NUM_FONT)
        self.edit_poll.setFixedWidth(72)
        self._inputs["poll_interval_s"] = self.edit_poll
        frow.addWidget(self.edit_poll)
        frow.addWidget(label("秒 · 最小 5 秒 · 保存后立即生效", "caption"))
        frow.addStretch(1)
        save_freq = QPushButton("保存")
        save_freq.setFixedWidth(84)
        save_freq.clicked.connect(self._save_config)
        frow.addWidget(save_freq)
        freq.addLayout(frow)

        logc = make_card(
            "事件日志",
            "日志保留条数，改动在下次启动生效。")
        lrow = QHBoxLayout()
        lrow.setSpacing(8)
        self.edit_rows = QLineEdit(str(self.cfg["log_max_rows"]))
        self.edit_rows.setFont(NUM_FONT)
        self.edit_rows.setFixedWidth(72)
        self._inputs["log_max_rows"] = self.edit_rows
        lrow.addWidget(self.edit_rows)
        lrow.addWidget(label("条 · 最小 100", "caption"))
        lrow.addStretch(1)
        save_rows = QPushButton("保存")
        save_rows.setFixedWidth(84)
        save_rows.clicked.connect(self._save_config)
        lrow.addWidget(save_rows)
        logc.addLayout(lrow)

        run = make_card(
            "运行方式",
            "开机自启写启动文件夹快捷方式，取消勾选即删除；「阻止睡眠」只申请"
            "系统唤醒请求，显示器照常超时熄灭。")
        self.cb_autostart = QCheckBox("开机自启（登录后自动打开并开始轮询）")
        self.cb_autostart.setChecked(bool(self.cfg["autostart"]))
        self.cb_autostart.setToolTip(
            "在「启动」文件夹写 币安带单跟单.lnk，指向 pythonw.exe（不弹控制台）。\n"
            "每次启动都会自检一次：快捷方式缺失、指错解释器、或存在旧版残留，"
            "都会自动重建并写进日志。")
        self._checks["autostart"] = self.cb_autostart
        run.addWidget(self.cb_autostart)
        run.addSpacing(6)
        self.cb_auto_poll = QCheckBox("启动后自动开始轮询")
        self.cb_auto_poll.setChecked(bool(self.cfg["auto_poll"]))
        self._checks["auto_poll"] = self.cb_auto_poll
        run.addWidget(self.cb_auto_poll)
        run.addSpacing(6)
        self.cb_keep_awake = QCheckBox("轮询期间阻止系统睡眠（允许息屏）")
        self.cb_keep_awake.setChecked(bool(self.cfg["keep_awake"]))
        self.cb_keep_awake.setToolTip(
            "只申请 PowerRequestSystemRequired，不申请 DisplayRequired ——\n"
            "系统不进睡眠（否则 socket 全断、定时器全停），显示器按电源方案\n"
            "正常熄灭。验证：管理员命令行跑 powercfg -requests，DISPLAY 段应为空。")
        self._checks["keep_awake"] = self.cb_keep_awake
        run.addWidget(self.cb_keep_awake)
        run.addSpacing(6)
        self.cb_rebalance = QCheckBox("每轮按账本对账补差（漏单自愈）")
        self.cb_rebalance.setChecked(bool(self.cfg["rebalance"]))
        self._checks["rebalance"] = self.cb_rebalance
        run.addWidget(self.cb_rebalance)
        run.addSpacing(10)
        row2 = QHBoxLayout()
        row2.addStretch(1)
        save_run = QPushButton("保存")
        save_run.setFixedWidth(84)
        save_run.clicked.connect(self._save_config)
        row2.addWidget(save_run)
        run.addLayout(row2)

        trade = make_card(
            "交易参数",
            "杠杆只决定占用多少保证金，不改变仓位大小 —— 仓位永远按"
            "「他的数量 × 权益比」算。品种最大杠杆低于设定值时交易所会拒单，"
            "看到 -4028 就把这里调小。")
        trow = QHBoxLayout()
        trow.setSpacing(10)
        trow.addWidget(label("下单杠杆", "caption"))
        self.edit_leverage = QLineEdit(str(self.cfg["leverage"]))
        self.edit_leverage.setFixedWidth(72)
        self.edit_leverage.setAlignment(Qt.AlignRight)
        self.edit_leverage.setFont(NUM_FONT)
        self._inputs["leverage"] = self.edit_leverage
        trow.addWidget(self.edit_leverage)
        trow.addWidget(label("x（1 - 125）", "help"))
        trow.addSpacing(20)
        trow.addWidget(label("涨跌配色", "caption"))
        self.scheme_box = QComboBox()
        self.scheme_box.setFixedWidth(132)
        self.scheme_box.addItems(["binance", "cn"])
        self.scheme_box.setCurrentText(str(self.cfg["color_scheme"]))
        self._combos["color_scheme"] = self.scheme_box
        trow.addWidget(self.scheme_box)
        trow.addWidget(label("币安=涨绿跌红", "help"))
        trow.addStretch(1)
        trade.addLayout(trow)
        trade.addSpacing(10)
        trow2 = QHBoxLayout()
        trow2.addStretch(1)
        save_trade = QPushButton("保存")
        save_trade.setFixedWidth(84)
        save_trade.clicked.connect(self._save_config)
        trow2.addWidget(save_trade)
        trade.addLayout(trow2)

        lay.addStretch(1)
        self.nb.addTab(area, "运行配置")

    def _build_announce_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(label("带单员操作播报", "caption"))
        self.cb_sound = QCheckBox("声音提示")
        self.cb_sound.setChecked(bool(self.cfg["sound_enabled"]))
        self._checks["sound_enabled"] = self.cb_sound
        head.addWidget(self.cb_sound)
        test_sound = QPushButton("试听")
        test_sound.setFixedWidth(56)
        test_sound.clicked.connect(self._play_sound)
        head.addWidget(test_sound)
        head.addStretch(1)
        clear_btn = QPushButton("清空播报")
        clear_btn.setFixedWidth(92)
        clear_btn.clicked.connect(self.clear_announce)
        head.addWidget(clear_btn)
        lay.addLayout(head)
        lay.addWidget(hline())

        self.announce_text = QTextEdit()
        self.announce_text.setReadOnly(True)
        self.announce_text.setFont(QFont(MONO_FAMILY, 9))
        self.announce_text.setLineWrapMode(QTextEdit.WidgetWidth)
        lay.addWidget(self.announce_text, 1)
        self.nb.addTab(page, "操作播报")

    # ---------- 主题 ----------
    def _sync_theme_button(self):
        self.theme_btn.setText("深色模式" if self.theme.current == "light"
                               else "浅色模式")

    def _toggle_theme(self):
        name = "light" if self.theme.current == "dark" else "dark"
        # 一次 setStyleSheet 换完整棵树。QSS 管不到的地方（表格 item 前景色、
        # 播报正文的字符格式）在同一函数里紧接着重涂，中间不让出事件循环，
        # 所以屏幕上不会出现「一半新一半旧」的中间帧。
        self.theme.apply(name)
        self._recolor()
        self._sync_theme_button()
        self.cfg["theme"] = name
        self._save_config()
        self._log("INFO", f"已切换到{'浅色' if name == 'light' else '深色'}模式")

    # ---------- 无边框窗口 ----------
    def _apply_frame_shadow(self):
        """给无边框窗口补回 DWM 投影。

        句柄要等窗口真正创建才有效，所以排到 show 之后调。
        """
        try:
            hwnd = int(self.winId())
        except (TypeError, ValueError):
            return
        state = apply_frame_shadow(hwnd)
        if state.startswith("fail") and state != getattr(self, "_frame_state", None):
            self._log("WARN", f"窗口投影设置失败：{state}")
        self._frame_state = state

    def _sync_max_button(self):
        self.btn_max.set_kind("restore" if self.isMaximized() else "max")

    def _toggle_max(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._sync_max_button()

    def _hit_test(self, msg):
        """返回 Win32 命中码；None 表示不接管，交回 Qt 默认处理（= 客户区）。"""
        user32 = ctypes.windll.user32
        hwnd = ctypes.wintypes.HWND(int(self.winId()))
        dpr = self.devicePixelRatio() or 1.0
        # msg.pt 和 GetWindowRect 都是屏幕物理像素；先换成窗口内物理坐标，
        # 再除以 DPR 得到 Qt 的逻辑坐标（150% 缩放下两者差 1.5 倍）。
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        x = (msg.pt.x - rect.left) / dpr
        y = (msg.pt.y - rect.top) / dpr
        w, h = self.width(), self.height()
        m = RESIZE_MARGIN

        # 四边四角：最大化时不留热区，否则边缘点不到任务栏
        if not self.isMaximized():
            left, right = x < m, x >= w - m
            top, bottom = y < m, y >= h - m
            if top and left:
                return HTTOPLEFT
            if top and right:
                return HTTOPRIGHT
            if bottom and left:
                return HTBOTTOMLEFT
            if bottom and right:
                return HTBOTTOMRIGHT
            if left:
                return HTLEFT
            if right:
                return HTRIGHT
            if top:
                return HTTOP
            if bottom:
                return HTBOTTOM

        # 标题栏：空白处当标题栏用（拖动 / 双击最大化 / 右键系统菜单），
        # 按钮上必须还给 Qt，否则按下会变成拖窗口。
        if y < TITLEBAR_H:
            child = self.titlebar.childAt(int(x), int(y))
            if isinstance(child, QPushButton):
                return HTCLIENT
            return HTCAPTION
        return None

    def _limit_max_size(self, msg):
        """最大化只铺满工作区，别盖住任务栏。

        无边框窗口没有 WS_MAXIMIZEBOX，Windows 会把它最大化到整块屏幕。
        在 WM_GETMINMAXINFO 里按显示器工作区给上限即可。
        """
        class MINMAXINFO(ctypes.Structure):
            _fields_ = [("ptReserved", ctypes.wintypes.POINT),
                        ("ptMaxSize", ctypes.wintypes.POINT),
                        ("ptMaxPosition", ctypes.wintypes.POINT),
                        ("ptMinTrackSize", ctypes.wintypes.POINT),
                        ("ptMaxTrackSize", ctypes.wintypes.POINT)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.wintypes.DWORD),
                        ("rcMonitor", ctypes.wintypes.RECT),
                        ("rcWork", ctypes.wintypes.RECT),
                        ("dwFlags", ctypes.wintypes.DWORD)]

        user32 = ctypes.windll.user32
        hwnd = ctypes.wintypes.HWND(int(self.winId()))
        mon = user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return False
        info = ctypes.cast(ctypes.c_void_p(msg.lParam),
                           ctypes.POINTER(MINMAXINFO)).contents
        work, screen = mi.rcWork, mi.rcMonitor
        info.ptMaxPosition.x = work.left - screen.left
        info.ptMaxPosition.y = work.top - screen.top
        info.ptMaxSize.x = work.right - work.left
        info.ptMaxSize.y = work.bottom - work.top
        # 接管了这条消息，Qt 自己的最小尺寸约束就不再生效，得自己写回去
        dpr = self.devicePixelRatio() or 1.0
        info.ptMinTrackSize.x = int(self.minimumWidth() * dpr)
        info.ptMinTrackSize.y = int(self.minimumHeight() * dpr)
        return True

    def nativeEvent(self, eventType, message):
        """把拖动、双击最大化、四边缩放交回 Windows。

        窗口是无边框的，DWM 不知道哪块是标题栏，所以得自己回答
        WM_NCHITTEST。返回 HTCAPTION 之后拖动、双击最大化、贴边吸附
        （Aero Snap）全部由系统实现，不需要手写鼠标追踪。
        """
        if sys.platform != "win32" or eventType != b"windows_generic_MSG":
            return super().nativeEvent(eventType, message)
        msg = ctypes.wintypes.MSG.from_address(int(message))
        if msg.message == WM_NCHITTEST:
            hit = self._hit_test(msg)
            if hit is not None:
                return True, hit
        elif msg.message in (WM_NCLBUTTONDBLCLK, WM_LBUTTONDBLCLK):
            # 双击标题栏最大化：DefWindowProc 会转成 SC_MAXIMIZE，但它要求
            # 窗口带 WS_MAXIMIZEBOX。无边框窗口没有这个位（加了会多出一圈
            # 原生边框，还得重算坐标），所以自己接。
            # 客户端版一起接：两次点击之间鼠标若有抖动，系统可能把第二下
            # 判成客户区双击，落点仍在标题栏就该按标题栏处理。
            # 状态切换排到消息处理完之后：在 nativeEvent 里直接 showMaximized
            # 会和 Qt 正在处理的窗口状态打架。
            if self._hit_test(msg) == HTCAPTION:
                QTimer.singleShot(0, self._toggle_max)
                return True, 0
        elif msg.message == WM_GETMINMAXINFO:
            if self._limit_max_size(msg):
                return True, 0
        return super().nativeEvent(eventType, message)

    def showEvent(self, event):
        super().showEvent(event)
        # 句柄要等窗口真正创建才有效，排到事件循环末尾再补投影
        QTimer.singleShot(0, self._apply_frame_shadow)
        self._sync_max_button()

    def changeEvent(self, event):
        super().changeEvent(event)
        # 双击标题栏最大化、贴边吸附、Aero Snap 都由系统改状态，
        # 这里只负责把按钮符号跟上去
        if event.type() == QEvent.WindowStateChange:
            self._sync_max_button()

    def _recolor(self):
        self._render_fills()
        self._render_sim()
        self._render_pnl(getattr(self, "_last_pnl", {}))
        self._render_pending()
        self._redraw_log()
        self._announce_render()

    # ---------- 运行时 ----------
    def _wire_logging(self):
        handler = QueueLogHandler(self.events)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        self._handler = handler
        # 再挂一份落盘的。界面上那 500 行是内存里的，重启就没了；
        # 出问题要复盘「几点几分在干什么」只能靠文件。
        try:
            LOG_DIR.mkdir(exist_ok=True)
            fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000,
                                     backupCount=5, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(message)s"))
            logging.getLogger().addHandler(fh)
            self._file_handler = fh
            # 界面自己打的那条（_log）走独立 logger，只挂文件 handler：
            # 否则会经根 logger 再进一次 QueueLogHandler，表格里出现两份。
            # 加这个是因为「引擎执行失败」这类错误原来只进内存表格，
            # 落盘日志里看不到，复盘时等于瞎的。
            self._ui_logger = logging.getLogger("gui.ui")
            self._ui_logger.propagate = False
            self._ui_logger.setLevel(logging.INFO)
            self._ui_logger.addHandler(fh)
        except OSError as e:
            self._file_handler = None
            self._ui_logger = None
            logging.getLogger().warning("日志文件不可写（%s），只保留界面日志", e)

    def _init_runtime(self):
        self.rule = Rule(
            improve_pct=float(self.cfg["improve_pct"]) / 100.0,
            slippage_tol_pct=float(self.cfg["slippage_tol_pct"]) / 100.0,
            better_price_enabled=self.cfg["better_price_enabled"],
            leverage=int(float(self.cfg.get("leverage") or 20)))
        # 超龄开仓信号不跟：避免拿几小时前的价格挂限价。
        self.rule.signal_max_age_s = 900
        # 入场限价单不自动撤：更优价本来就是「等他回到更好的位置」，
        # 撤了就等于主动放弃这一单。只有他平仓才会撤。
        self.rule.entry_timeout_s = 0
        # auto_fill=False：dry-run 也要按真实撮合语义走 ——
        # 限价单挂上去，只有标记价真的穿到限价才成交。
        self._broker_sig: tuple | None = ("dry-run", "", "")
        self.broker = MemoryBroker(unfilled=False, auto_fill=False)
        self.executor = BetterPriceExecutor(
            self.broker, None, None, dry_run=False,
            improve_pct=self.rule.improve_pct,
            slippage_tol_pct=self.rule.slippage_tol_pct,
            better_price_enabled=self.rule.better_price_enabled,
            fill_timeout_s=300, poll_sec=5,
            tp_roe_pct=None, sl_roe_pct=None)
        self.engine = CopyEngine(self.executor, self.rule,
                                 store=self.store,
                                 portfolio_id=self.cfg["portfolio_id"])

    def _on_price_mode_change(self, *_):
        on = bool(self.cb_better.isChecked())
        cap, help_text = PCT_TEXT[on]
        self._pct_cap.setText(cap)
        self._pct_help.setText(help_text)
        self.edit_improve.setVisible(on)
        self.edit_slip.setVisible(not on)
        if hasattr(self, "rule"):
            self._refresh_cfg()

    def _collect_config(self) -> dict | None:
        try:
            pct = float(self.edit_improve.text().strip())
            tol = float(self.edit_slip.text().strip())
            if not (0 <= pct <= 20 and 0 <= tol <= 20):
                raise ValueError("幅度需在 0 - 20 之间")
        except ValueError as e:
            self._log("ERROR", f"参数错误: {e}")
            return None
        return {
            "improve_pct": pct / 100.0,
            "slippage_tol_pct": tol / 100.0,
            "better_price_enabled": bool(self.cb_better.isChecked()),
            "mode": self.mode_box.currentText(),
            "api_key": self.edit_key.text().strip()
                       or os.getenv("BINANCE_API_KEY", ""),
            "api_secret": self.edit_secret.text().strip()
                          or os.getenv("BINANCE_API_SECRET", ""),
            "portfolio_id": self.edit_pid.text().strip(),
            "poll_interval_s": self.edit_poll.text().strip(),
            "log_max_rows": self.edit_rows.text().strip(),
            "leverage": self.edit_leverage.text().strip(),
            "color_scheme": self.scheme_box.currentText(),
        }

    def _refresh_cfg(self) -> bool:
        cfg = self._collect_config()
        if cfg is None:
            return False
        self._cfg = cfg
        mode = ("更优 %.2f%%" % (cfg["improve_pct"] * 100)
                if cfg["better_price_enabled"]
                else "容差 %.2f%%" % (cfg["slippage_tol_pct"] * 100))
        self.mode_badge.setText(f"当前：{mode}")
        return True

    def _switch_broker(self, mode: str, cfg: dict, sig: tuple) -> bool:
        """切模式 / 换凭证时换掉下单端。返回 False 表示缺凭证、本轮跳过。

        换之前必须先把旧账户上的挂单撤掉：不撤就变成没人管理的幽灵单 ——
        带单员平仓时 `_close()` 的 `cancel_all_orders` 会打到新 broker 上，
        旧账户那笔永远留在那里。
        """
        for p in list(self.engine.pending.values()):
            try:
                self.broker.cancel_all_orders(p.symbol)
            except Exception as e:
                self.events.put(("log", "WARN",
                                 f"切换前撤单失败 {p.symbol}："
                                 f"{type(e).__name__}: {e}"))
        self.engine.pending.clear()
        # 「已跟账本」也必须清：dry-run 跑出来的账本会让实盘账户以为已经
        # 按目标开满（gap<=0），从此永久少开甚至不开。
        self.engine.filled.clear()
        self.engine._save_filled()
        if self.store is not None:
            self.store.clear_pending()
        if mode == "dry-run":
            self.broker = MemoryBroker(unfilled=False, auto_fill=False)
            self._broker_sig = sig
            self.events.put(("log", "INFO", "执行模式切换：本地模拟（不下单）"))
            return True
        key, secret = cfg.get("api_key"), cfg.get("api_secret")
        if not key or not secret:
            self.broker = MemoryBroker(unfilled=False, auto_fill=False)
            self._broker_sig = None
            return False
        from chatdsn.binance import BinanceService
        self.broker = BinanceService(key, secret, testnet=(mode == "testnet"))
        self._broker_sig = sig
        self.events.put(("log", "INFO",
                         "执行模式切换："
                         + ("测试网" if mode == "testnet" else "实盘")))
        return True

    def _apply_runtime(self, cfg: dict) -> bool:
        """只碰 Python 对象与网络，不碰任何 Qt 控件（可在后台线程跑）。"""
        self.rule.improve_pct = cfg["improve_pct"]
        self.rule.slippage_tol_pct = cfg["slippage_tol_pct"]
        self.rule.better_price_enabled = cfg["better_price_enabled"]
        self.rule.rebalance = bool(cfg.get("rebalance", True))
        try:
            self.rule.leverage = min(125, max(1, int(float(cfg.get("leverage") or 20))))
        except (TypeError, ValueError):
            self.rule.leverage = 20
        # 权益只从交易所读，绝不从 cfg 覆盖：cfg 是 start_polling 时的快照，
        # 里面那个 my_equity 可能还是默认的 10000，每轮把它写回去等于把
        # 「按 92 USDT 等比」变成「按 10000 USDT 等比」——仓位放大 100 倍，
        # 交易所直接以「保证金不足」拒单，表现就是「有成交事件但从不挂单」。
        self.executor.improve_pct = cfg["improve_pct"]
        self.executor.slippage_tol_pct = cfg["slippage_tol_pct"]
        self.executor.better_price_enabled = cfg["better_price_enabled"]

        mode = cfg["mode"]
        # 换模式或换 Key 都必须重建客户端：`base_url` 在 BinanceService 构造时
        # 定死，复用旧对象会出现「下拉框写 live、实际还在打 testnet」——
        # 权益读的是测试网，仓位按错账户等比，账本却按 live 落库。
        sig = (mode, cfg.get("api_key") or "", cfg.get("api_secret") or "")
        if sig != self._broker_sig:
            if not self._switch_broker(mode, cfg, sig):
                return False
        if mode == "dry-run":
            self._pnl_avail = 0.0
        else:
            # 真实模式下每轮用账户钱包总额刷新权益。不能依赖「保存并连接」
            # 手动触发：没点过按钮时 Rule.my_equity 会留在默认 10000，
            # 小账户按 10000 等比算会把仓位放大几十倍。
            bal = self.broker.get_balance()
            # 用钱包总额而不是可用余额：可用余额会随持仓占用保证金缩水，
            # 逐轮刷新会让累计仓位越跟越小。
            equity = (float(bal.get("total_balance")
                            or bal.get("available_balance") or 0)
                      if bal else 0.0)
            self._pnl_avail = (float(bal.get("available_balance") or 0.0)
                               if bal else 0.0)
            if equity > 0:
                if abs(self.rule.my_equity - equity) > 0.01:
                    self.events.put(("log", "INFO",
                                     f"账户权益已同步：{equity:.2f} USDT，"
                                     "仓位按此等比计算"))
                self.rule.my_equity = equity
                self._equity_verified = True
            else:
                # 读不到权益就不下单：宁可这一轮不跟，也不能拿旧值/默认值
                # 去算仓位。以前只在「从未验证过」时拦，验证过一次之后就
                # 允许用陈旧权益继续，等于给错误留了后门。
                self.events.put(("error",
                                 "读不到账户可用余额，本轮跳过，"
                                 "避免按错误权益放大仓位"))
                return False
        if self.executor.binance is not self.broker:
            self.executor.binance = self.broker
            self.engine.pending.clear()
        return True

    # ---------- 账户连接（网络部分在后台线程） ----------
    def _set_account_state(self, kind: str, text: str):
        role = {"idle": "", "loading": "caption", "ok": "accent",
                "error": "warn"}[kind]
        self.account_label.setProperty("role", role)
        self.account_label.setText(text)
        repolish(self.account_label)

    def _connect_account(self):
        load_env()
        key = self.edit_key.text().strip() or os.getenv("BINANCE_API_KEY", "")
        secret = self.edit_secret.text().strip() or os.getenv("BINANCE_API_SECRET", "")
        if not key or not secret:
            self._set_account_state("error", "未填 API Key / Secret")
            self.metrics["conn"].setText("未连接")
            self._log("ERROR", "未填 API Key / Secret。扫码登录和 Cookie 都不能下单。")
            return

        existing: dict[str, str] = {}
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
        existing["BINANCE_API_KEY"] = key
        existing["BINANCE_API_SECRET"] = secret
        existing.setdefault("BINANCE_TESTNET", "false")
        tmp = ROOT / ".env.tmp"
        tmp.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n",
                       encoding="utf-8")
        tmp.replace(ENV_FILE)
        os.environ["BINANCE_API_KEY"] = key
        os.environ["BINANCE_API_SECRET"] = secret
        self._log("INFO", "凭证已原子写入 .env")

        mode = self.mode_box.currentText()
        self._set_account_state("loading", "连接中… 正在校准时钟并校验权限")

        def probe():
            try:
                result = AccountProbe(key=key, secret=secret,
                                      testnet=(mode == "testnet")).check()
                self.events.put(("account", result))
            except Exception as e:
                self.events.put(("account", {"account_ok": False,
                                             "error": f"{type(e).__name__}: {e}"}))

        threading.Thread(target=probe, daemon=True).start()

    def _render_account(self, result: dict):
        if result.get("account_ok"):
            equity = float(result.get("total_wallet_balance")
                           or result.get("available_balance") or 0)
            line = (f"已连接 {result['fapi_host']} · 可用 "
                    f"{result['available_balance']:.2f} USDT · 持仓 "
                    f"{len(result.get('open_positions', []))} 笔 · 时钟差 "
                    f"{result.get('clock_offset_ms', 0)} ms")
            self._set_account_state("ok", line)
            self.metrics["conn"].setText("已连接")
            self.rule.my_equity = equity
            self._equity_verified = True
            self.metrics["equity"].setText(f"{equity:.2f}")
            self._on_mode_change()
            self._refresh_cfg()
            self._log("INFO", line)
            r = result.get("restrictions")
            if isinstance(r, dict):
                self._log("INFO", f"权限 enableFutures={r.get('enableFutures')} "
                                  f"enableWithdrawals={r.get('enableWithdrawals')} "
                                  f"ipRestrict={r.get('ipRestrict')}")
        else:
            msg = result.get("error") or "账户查询失败"
            if "-2015" in msg or "Invalid API-key" in msg:
                if "testnet" in str(result.get("fapi_host", "")):
                    msg = ("测试网拒绝了这把 Key（-2015）。它是在币安主站创建的，"
                           "测试网要单独建 Key；或把执行模式切回 dry-run / live。"
                           "网络是通的，开 TUN 不影响这个结果。")
                else:
                    msg = ("交易所拒绝了这把 Key（-2015）。检查是否已启用合约权限、"
                           "IP 白名单是否包含当前出口 IP。")
            self._set_account_state("error", f"连接失败：{msg}")
            self.metrics["conn"].setText("失败")
            self._log("ERROR", f"连接失败: {msg}")
        for w in result.get("warnings", []):
            self._log("WARN", w)

    # ---------- 取数 ----------
    def fetch_events(self, cfg: dict):
        """带单本接口；detail 60s 缓存，Session 长期复用。"""
        with self._api_lock:
            pid = str(cfg.get("portfolio_id") or DEFAULTS["portfolio_id"])
            if self.api is None or getattr(self, "_api_pid", "") != pid:
                from api_feed import LeadApi
                self.api = LeadApi(pid)
                self._api_pid = pid
                self._detail_cache = None
                self._detail_ts = 0.0
            now = time.monotonic()
            if self._detail_cache is None or now - self._detail_ts > 60:
                try:
                    d = self.api.detail()
                    self._detail_cache = {
                        "balance": float(d.get("marginBalance") or 0),
                        "nickname": d.get("nickname"),
                        "copiers": f"{d.get('currentCopyCount')}/"
                                   f"{d.get('maxCopyCount')}",
                    }
                except Exception:
                    if self._detail_cache is None:
                        raise
                self._detail_ts = now
            fills = self.api.fills(rows=500)

        detail = self._detail_cache or {}
        meta = {
            "balance": detail.get("balance", 0.0),
            "nickname": detail.get("nickname"),
            "copiers": detail.get("copiers"),
            "balance_age_s": round(time.monotonic() - self._detail_ts, 1)
                             if self._detail_ts else 0,
        }
        return fills, meta

    def _invalidate_cache(self):
        self._detail_cache = None

    def _fetch_worker(self, cfg: dict):
        try:
            fills, meta = self.fetch_events(cfg)
            self.events.put(("batch", fills, cfg, meta))
        except Exception as e:
            self.events.put(("error", f"取数失败: {type(e).__name__}: {e}"))

    # ---------- 引擎（网络部分全部在后台线程） ----------
    def _run_engine(self, fills, cfg: dict, meta: dict) -> dict | None:
        """纯逻辑 + 网络，可在后台线程跑；返回纯数据的 UI 快照。"""
        if not self._apply_runtime(cfg):
            return {"error": "配置无效：live / testnet 需要填 API Key 与 Secret"}
        if meta.get("balance", 0) > 0:
            self.rule.lead_balance = meta["balance"]
        self.last_meta = meta

        # 先铺带单员那一侧；「应开 / 实开」两列要等 process() 之后才有准确
        # 分母（钱包余额）和我方真实持仓，见 _fills_table_rows()。
        base = []
        for f in fills:
            bull = ((f.action == "OPEN" and f.side == "LONG") or
                    (f.action == "CLOSE" and f.side == "SHORT"))
            base.append([
                # 去掉年份：9 列挤在一屏里，全写会截断成「2026-11-13 10:19…」
                f.ts[5:] if len(f.ts) >= 19 else f.ts,
                "开" if f.action == "OPEN" else "平",
                "多" if f.side == "LONG" else "空", f.symbol,
                f"{f.price:.5f}", f"{f.qty:.5f}", f"{f.notional:.2f}",
                "POS" if bull else "NEG"])

        # 基线判断下沉到 CopyEngine：它带 portfolio_id，能识别换人并重置起点，
        # 还能用 follow_start_wall 保证历史事件在 seen TTL 淘汰后不复活。
        # 标记价用来把 marginBalance 拆成「钱包余额 + 未实现盈亏」：
        # 敞口比例的分母必须是钱包余额，否则他浮亏越深我方仓位越大。
        marks = {}
        try:
            from api_feed import mark_prices
            marks = mark_prices()
        except Exception as e:
            self.events.put(("log", "WARN",
                             f"标记价获取失败，本轮按 marginBalance 口径："
                             f"{type(e).__name__}: {e}"))
        # 撮合必须在 process 之前：引擎 _open() 会读 get_positions() 算「还差
        # 多少」，先撮合才能把本轮成交算进已有仓位，否则会重复挂单。
        if isinstance(self.broker, MemoryBroker) and marks:
            got = self.broker.match_orders(marks)
            if got:
                self.events.put(("log", "INFO",
                                 f"模拟撮合成交：{', '.join(got)}"))
        result = self.engine.process(fills,
                                     lead_balance=meta.get("balance"),
                                     marks=marks)
        ws = (self.engine.state().get("wallet") or {})
        metrics = {
            "balance": self.rule.lead_balance,
            "my_equity": self.rule.my_equity,
            "wallet": ws.get("wallet", 0.0),
            "unrealized": ws.get("unrealized", 0.0),
            "margin_balance": ws.get("margin_balance", 0.0),
            "anchored": ws.get("anchored", False),
            "wallet_missing": ws.get("missing") or [],
            "pending": len(self.engine.pending),
            "mode": self.executor.price_mode_text(),
        }
        pending = self.engine.state()["pending"]
        # 持仓只读一次：引擎线程读，界面线程（仓位管理）直接用这份快照。
        # 界面线程里再调一次 get_positions() 就是一次签名 REST，会把界面卡住。
        try:
            positions = self.broker.get_positions() or []
        except Exception:
            positions = []
        rows = self._fills_table_rows(fills, base, positions)
        # 盈亏账本单独兜一层异常：记账失败绝不能把这一轮的跟单结果吞掉。
        try:
            pnl = self._record_pnl(cfg, result["actions"], positions,
                                   marks, ws, fills)
        except Exception as e:
            pnl = self._last_pnl
            self.events.put(("log", "WARN",
                             f"盈亏记录失败（不影响跟单）："
                             f"{type(e).__name__}: {e}"))
        return {"baseline": result.get("baseline", False),
                "baseline_count": result.get("baseline_count", 0),
                "result": result, "rows": rows, "fills": fills,
                "positions": positions,
                "actions": result["actions"],
                "signals": result.get("signals", []),
                "pending": pending, "pid": cfg.get("portfolio_id"),
                "metrics": metrics, "meta": meta, "pnl": pnl}

    def _record_pnl(self, cfg: dict, actions: list, positions: list,
                    marks: dict, ws: dict, fills: list) -> dict:
        """把本轮持仓 / 动作记进 state.db，并回读曲线交给界面。

        跑在引擎线程：StateStore 是 `check_same_thread=False` + 内部锁，
        和引擎的其他调用串行，界面线程不会被 IO 卡住。

        曲线不做每轮全量落库 —— 10 秒一条一天就是 8000 行，而这个页面的
        用途是看趋势，30 秒一条足够；有动作时无条件写，明细一条不漏。
        """
        mode = str(cfg.get("mode") or "dry-run")
        ts = time.time()
        merged = dict(marks or {})
        for f in fills:                     # 缺标记价时用最新成交价兜底
            merged.setdefault(f.symbol, f.price)

        lead_exposure = 0.0
        rep = getattr(self.engine, "_replay", None)
        if rep is not None:
            lead_exposure = sum(float(t.get("notional") or 0.0)
                                for t in rep.totals.values())

        snap, pos_rows = pnl_snapshot(
            positions, merged, equity=self.rule.my_equity,
            avail=self._pnl_avail, leverage=self.rule.leverage,
            lead_wallet=self.engine.lead_wallet or 0.0,
            lead_unreal=float(ws.get("unrealized") or 0.0),
            lead_exposure=lead_exposure, ts=ts)
        trades = trades_from_actions(actions, ts, self._last_positions, merged)

        curve: list = []
        hist: list = []
        realized = 0.0
        if self.store is not None:
            if trades or ts - self._pnl_last_write >= 30.0:
                self.store.save_pnl(mode, snap, pos_rows)
                self._pnl_last_write = ts
            if trades:
                self.store.save_trades(mode, trades)
            if ts - self._pnl_last_prune >= 86400.0:
                self._pnl_last_prune = ts
                self.store.prune_pnl()
            curve = self.store.load_pnl_curve(mode, limit=800)
            hist = self.store.load_pnl_trades(mode, limit=120)
            realized = self.store.pnl_realized(mode)
        return {"mode": mode, "curve": curve, "trades": hist,
                "pos_rows": pos_rows, "unreal": snap["unreal"],
                "realized": realized}

    def _fills_table_rows(self, fills, base, positions=None) -> list:
        """给成交事件表补上「应开 / 实开」两列。

        两列都是**名义 USDT**，不是数量 —— 数量随币价变化，名义才是等比口径。
        应开 = 他**这笔开完之后**的累计名义，按等比折成我该持的名义。
               用的是全量重放给出的累计名义与累计均价（rep.open_context），
               跟引擎 _open() 完全同一套输入 —— 所以他分三笔加仓时，
               三行显示的是 12.5 / 37.5 / 50 这种累计值，不是三笔各 12.5。
               按单笔算会让人以为「三笔加起来 50，怎么只开了 25」。
        实开 = 交易所里该品种我当前真实持仓的名义（数量 × 我的开仓均价）。
        """
        wallet = self.engine.lead_wallet or self.rule.lead_balance
        equity = self.rule.my_equity
        # 我方持仓：数量 + 开仓均价。名义 = 数量 × 均价（entry_price 拿不到时
        # 用这批流水里该品种的最新成交价兜底，dry-run 的模拟持仓就是 0）。
        mine = {}
        last_px = {}
        for f in fills:
            last_px[f.symbol] = f.price
        try:
            held = (positions if positions is not None
                    else self.broker.get_positions() or [])
            for p in held:
                qty = abs(float(p.get("quantity", 0)))
                px = abs(float(p.get("entry_price", 0) or 0)) or \
                    last_px.get(p["symbol"], 0.0)
                mine[p["symbol"]] = qty * px
        except Exception:
            pass
        rep = getattr(self.engine, "_replay", None)
        ctx_map = rep.open_context if rep is not None else {}
        # 最新一条排最前：成交事件是流水，越新越该先看到。
        # 按 Fill.ts（完整时间戳，带年份）排，不按展示用的 "11-13 10:19:42"，
        # 否则跨年时 "12-31" 会排到 "01-01" 前面。
        pairs = sorted(zip(fills, base), key=lambda x: x[0].ts, reverse=True)
        rows = []
        for f, cells in pairs:
            want = "—"
            if f.action == "OPEN" and wallet > 0 and equity > 0:
                ctx = ctx_map.get(f.key, {})
                tick, step = self._symbol_steps(f.symbol)
                t = target_position(
                    float(ctx.get("notional", f.notional)),
                    float(ctx.get("qty", f.qty)), f.side, wallet, equity,
                    tick, step, self.rule)
                want = f"{t['target_notional']:.2f}"
            got = f"{mine.get(f.symbol, 0.0):.2f}"
            rows.append(tuple(cells[:7]) + (want, got, cells[7]))
        return rows

    def _engine_worker(self, fills, cfg: dict, meta: dict):
        try:
            snap = self._run_engine(fills, cfg, meta)
            if snap is None:
                self.events.put(("error", "执行配置无效，本轮跳过"))
            elif snap.get("error"):
                self.events.put(("error", snap["error"]))
            else:
                self.events.put(("snapshot", snap))
        except Exception as e:
            self.events.put(("error", f"引擎执行失败: {type(e).__name__}: {e}"))
        finally:
            self.events.put(("engine_done",))

    def _render_snapshot(self, snap: dict):
        # 排序在 _fills_table_rows() 里按完整时间戳做过，这里只取用
        self._fills_rows = [(row[:9], row[9]) for row in snap["rows"]]
        self._render_fills()
        # 标题还停在「未连接」就补一次确认（30 秒最多一次）：
        # 开机那一刻本地代理常常还没就绪，首次拉昵称会失败，不重试就一直是
        # 「未连接」。确认只能来自接口返回，不能拿配置缓存顶。
        self._ensure_lead_name()
        self._last_fills = snap.get("fills") or []
        self._last_positions = snap.get("positions") or []
        self._last_pnl = snap.get("pnl") or {}
        self._refresh_sim()
        self._render_pnl(self._last_pnl)
        self._pending_rows = [(tuple([p["symbol"], p["side"], p["qty"],
                                      p["limit"], f"{p['leverage']}x",
                                      p["age_s"],
                                      self.executor.price_mode_text()]),
                               "WARN" if p["age_s"] >= 270 else "")
                              for p in snap["pending"]]
        self._render_pending()
        m = snap["metrics"]
        # 展示的是「钱包余额」（已剔除未实现盈亏），敞口比例用的也是它；
        # marginBalance 只在拿不到标记价时兜底，两者相等时说明他空仓。
        wallet = m.get("wallet") or 0.0
        self.metrics["balance"].setText(
            f"{wallet:.2f}" if wallet > 0 else f"{m['balance']:.2f}")
        self.metrics["unreal"].setText(f"{(m.get('unrealized') or 0.0):+.2f}")
        # 我方权益每轮由 _apply_runtime() 从交易所重读（钱包总额，不含浮亏），
        # 这里只负责显示：以前只在变化 >0.01 时打一条日志，界面上看不到，
        # 加钱进去会以为没生效。
        self.metrics["equity"].setText(f"{(m.get('my_equity') or 0.0):.2f}")
        self.metrics["pending"].setText(str(m["pending"]))
        bal_txt = (f"钱包 {wallet:.2f} USDT" if wallet > 0
                   else f"余额 {m['balance']:.2f} USDT")
        missing = m.get("wallet_missing") or []
        if missing and set(missing) != self._wallet_missing:
            self._wallet_missing = set(missing)
            self._log("WARN", "以下品种缺标记价，其浮亏未计入分母"
                              f"（本程序仓位偏保守）：{', '.join(missing[:8])}")
        meta = snap["meta"] or {}
        nick = meta.get("nickname") or "-"
        # 换人瞬间仍在飞行的旧批次不能把上一个人的名字绑到新 pid
        if (nick and nick != "-"
                and snap.get("pid") == self.cfg.get("portfolio_id")):
            self._set_lead_nickname(nick)
        age = meta.get("balance_age_s", 0)
        age_txt = f" · 余额缓存 {age:.0f}s" if age else ""

        if snap["baseline"]:
            self._log("INFO", f"已建立基线：{snap['baseline_count']} 条历史事件，"
                              f"仅对带单员未平的历史仓位按更优价挂单")
            self.status.setText(f"基线已建立 · 目标 {nick} · {bal_txt} · "
                                f"{m['mode']}{age_txt}")
            self._announce(snap.get("signals", []), meta, snap["actions"])
            return

        r = snap["result"]
        cancel_only = r.get("cancel_only", 0)
        self._log("INFO", f"跟进结果 events={r['events']} 开={r['opened']} "
                          f"平={r['closed']} 仅撤单={cancel_only} "
                          f"跳过={r['skipped']} · 目标 {nick} · {m['mode']}")
        for action in snap["actions"]:
            self._log("ACTION", format_action(action))
        self.status.setText(
            f"上次运行 {datetime.now():%H:%M:%S} · 新事件 {r['events']} · "
            f"开 {r['opened']} · 平 {r['closed']} · 仅撤单 {cancel_only} · "
            f"挂单 {r['pending']} · {m['mode']}{age_txt}")
        self._announce(snap.get("signals", []), meta, snap["actions"])

    # ---------- 表格渲染（主题切换时整份重涂） ----------
    def _render_fills(self):
        t = self.theme.tokens()
        self.tree_fills.setRowCount(len(self._fills_rows))
        aligns = self.tree_fills._aligns
        for r, (vals, role) in enumerate(self._fills_rows):
            color = t.get(role) if role else None
            for c, v in enumerate(vals):
                self.tree_fills.setItem(
                    r, c, self._mk_item(v, aligns[c], mono=c >= 4,
                                        color=color))
        n = len(self._fills_rows)
        if n:
            tail = min(30, n)
            self.fills_summary.setText(f"最近 {tail} 条 · 共 {n} 条成交事件")
            self.fills_summary.setProperty("role", "caption")
        else:
            self.fills_summary.setText("暂无成交事件 · 点击「单次运行」取数")
            self.fills_summary.setProperty("role", "")
        repolish(self.fills_summary)

    def _render_pending(self):
        t = self.theme.tokens()
        self.tree_pending.setRowCount(len(self._pending_rows))
        aligns = self.tree_pending._aligns
        for r, (vals, role) in enumerate(self._pending_rows):
            color = t.get(role) if role else None
            for c, v in enumerate(vals):
                self.tree_pending.setItem(
                    r, c, self._mk_item(v, aligns[c], mono=c >= 2,
                                        color=color))
        if self._pending_rows:
            timeout = int(getattr(self.rule, "entry_timeout_s", 0))
            rule_txt = (f"{timeout} 秒自动撤单 · " if timeout > 0 else "不自动撤单 · ")
            self.pending_summary.setText(
                f"挂单 {len(self._pending_rows)} 笔 · {rule_txt}"
                "带单员平仓即撤单")
            self.pending_summary.setProperty("role", "caption")
        else:
            self.pending_summary.setText("当前无未成交挂单")
            self.pending_summary.setProperty("role", "")
        repolish(self.pending_summary)

    # ---------- 动作 ----------
    def run_once(self, *_):
        if not self._refresh_cfg():
            return
        self._log("INFO", "取数交给后台线程")
        threading.Thread(target=self._fetch_worker, args=(self._cfg,),
                         daemon=True).start()

    def _process_sync(self, fills):
        """自检用的同步路径；与后台线程共用同一个 _run_engine。"""
        if not self._refresh_cfg():
            return
        snap = self._run_engine(fills, self._cfg, {})
        if snap and not snap.get("error"):
            self._render_snapshot(snap)

    def force_refresh(self, *_):
        self._invalidate_cache()
        self._log("INFO", "已清除余额缓存")
        self.run_once()

    def toggle_polling(self, *_):
        if self.running.is_set():
            self.stop_polling()
        else:
            self.start_polling()

    def _set_poll_button(self, running: bool) -> None:
        # 两个状态用两种语义色：黄=开始，红=停止。灰底会看起来像禁用，
        # 用户第一眼会以为按钮没生效。
        self.poll_btn.setText("停止轮询" if running else "开始轮询")
        self.poll_btn.setProperty("role", "danger" if running else "accent")
        repolish(self.poll_btn)

    def start_polling(self):
        if self.running.is_set():
            return
        if not self._refresh_cfg():
            return
        self._poll_wake.clear()
        self.running.set()
        self._set_poll_button(True)
        try:
            interval = max(5, int(float(self.edit_poll.text())))
        except (ValueError, TypeError):
            interval = int(DEFAULTS["poll_interval_s"])
        self._log("INFO", f"轮询已启动，间隔 {interval} 秒 · "
                          f"{self.executor.price_mode_text()}")
        # 系统一睡 socket 全断，醒来时信号早过了有效期 —— 整夜白跑。
        # 但只申请「系统唤醒」，显示器该灭就灭；电源方案本来就不睡的话
        # （STANDBYIDLE=0）连请求都不申请，零副作用。
        state = set_keep_awake(as_bool(self.cfg.get("keep_awake"), True))
        if state.startswith("fail"):
            self._log("WARN", f"阻止系统睡眠失败（{state}），"
                              "息屏后若系统休眠，跟单会中断")
        elif state == "skip" and as_bool(self.cfg.get("keep_awake"), True):
            self._log("INFO", "电源方案里睡眠已是「从不」，不申请唤醒请求；"
                              "显示器按系统设置正常熄灭")

        def worker():
            fails = 0
            while self.running.is_set():
                try:
                    fills, meta = self.fetch_events(self._cfg)
                    self.events.put(("batch", fills, self._cfg, meta))
                    fails = 0
                    wait = interval
                except Exception as e:
                    fails += 1
                    wait = min(2 ** fails, interval)
                    msg = str(e)
                    if "11012005" in msg or "当前系统正忙" in msg:
                        reason = "带单本接口限流"
                    else:
                        reason = "取数失败"
                    self.events.put(("error", f"{reason}，{wait} 秒后自动重连"
                                              f"（第 {fails} 次）: "
                                              f"{type(e).__name__}: {msg}"))
                self._poll_wake.wait(wait)

        threading.Thread(target=worker, daemon=True).start()

    def stop_polling(self):
        self.running.clear()
        self._poll_wake.set()          # 立刻唤醒等待，不用等满一个间隔
        self._set_poll_button(False)
        set_keep_awake(False)          # 不跟单了就别拦着系统睡觉
        self._log("INFO", "轮询已停止")

    # ---------- 事件泵（只做 UI 更新，绝不在这里发网络请求） ----------
    def _set_loading(self, on: bool):
        if on:
            self.load_label.setText("取数 / 执行中")
            self.progress.show()
        else:
            self.progress.hide()
            self.load_label.setText("")

    def _drain_events(self):
        try:
            while True:
                item = self.events.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log(item[1], item[2])
                elif kind == "batch":
                    if self._engine_busy:
                        # 忙时丢弃：成交历史是全量窗口，下一轮还会带回来，
                        # Fill.key 幂等去重保证不重复开平，也不会漏事件。
                        self._dropped_batches += 1
                        continue
                    self._engine_busy = True
                    self._set_loading(True)
                    threading.Thread(target=self._engine_worker,
                                     args=item[1:], daemon=True).start()
                elif kind == "snapshot":
                    self._render_snapshot(item[1])
                elif kind == "engine_done":
                    self._engine_busy = False
                    self._set_loading(False)
                elif kind == "account":
                    self._render_account(item[1])
                elif kind == "lead_name":
                    # 请求时带上 pid，换人后旧请求返回不能盖掉新名字
                    if item[1] == str(self.cfg.get("portfolio_id") or ""):
                        self._set_lead_nickname(item[2])
                elif kind == "lead_name_error":
                    if item[1] == str(self.cfg.get("portfolio_id") or ""):
                        self.lead_title.setText(self._lead_display())
                        self._log("WARN", f"带单员昵称获取失败：{item[2]}")
                elif kind == "error":
                    self._log("ERROR", item[1])
        except queue.Empty:
            pass

    # ---------- 播报 ----------
    def _announce_render(self):
        """按当前色板重画播报正文，最新的操作在最上面。

        QTextEdit 里字符的颜色是写进文档格式的，QSS 管不到，所以换主题时
        必须整份重画。

        倒序渲染意味着每来一条就要整份重画（增量追加只能往尾部加），
        600 块的上限下这点开销可以忽略；换来的是不用滚动就能看到最新操作。
        滚动位置按「新内容的实际高度」补偿，正在看旧内容时不会被顶走。
        """
        t = self.theme.tokens()
        colors = {"title": t["ACCENT"], "open": t["POS"], "close": t["NEG"],
                  "na": t["FAINT"], "note": t["MUTED"], "rule": t["BORDER"]}
        bar = self.announce_text.verticalScrollBar()
        keep = bar.value()
        max_before = bar.maximum()
        self.announce_text.clear()
        # 用文档游标插入，不把它设成「可见游标」：可见游标停在文末会让
        # QTextEdit 自动滚到底部，把刚画好的最新一条顶出视野。
        cursor = QTextCursor(self.announce_text.document())
        cursor.movePosition(QTextCursor.Start)
        for group in reversed(self._announce_groups):
            for kind, text in group:
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(colors.get(kind, t["TEXT"])))
                if kind == "title":
                    fmt.setFontWeight(QFont.Bold)
                cursor.insertText(text, fmt)
        view = self.announce_text.textCursor()
        view.movePosition(QTextCursor.Start)
        self.announce_text.setTextCursor(view)
        # 新内容插在顶部会把旧内容整体下推，补上这段高度差才不会「跳段」
        grown = bar.maximum() - max_before
        bar.setValue(0 if keep <= 2 else max(0, keep + max(0, grown)))

    def _announce_append(self, blocks: list[tuple[str, str]]):
        if not blocks:
            return
        self._announce_groups.append(list(blocks))
        self._announce_block_count += len(blocks)
        # 长了就整组砍掉最旧的，避免文档无限增长拖慢重绘
        while (self._announce_block_count > 600
               and len(self._announce_groups) > 1):
            self._announce_block_count -= len(self._announce_groups.pop(0))
        self._announce_render()

    def announce_plain_text(self) -> str:
        return self.announce_text.toPlainText().strip()

    def clear_announce(self, *_):
        self._announce_groups.clear()
        self._announce_block_count = 0
        self.announce_text.clear()
        self._announced.clear()

    def _play_sound(self):
        """两声短提示音。

        用 `winsound.Beep` 而不是 `PlaySound("SystemExclamation")`：后者读的是
        Windows「声音方案」，方案设成「无声」、或提示音被单独关掉时完全不响，
        而且没有任何返回值能看出来失败 —— 表现就是「播报出来了但没声音」。
        Beep 直接驱动音频设备，与声音方案无关；它会阻塞调用线程，所以放后台。
        """
        if winsound is None or self._mute_sound:
            return

        def _run():
            try:
                winsound.Beep(880, 110)
                winsound.Beep(1320, 130)
            except Exception:
                try:
                    winsound.PlaySound(
                        "SystemExclamation",
                        winsound.SND_ALIAS | winsound.SND_ASYNC)
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def _announce(self, signals: list, meta: dict, actions: list) -> None:
        """把带单员本轮新操作渲染成播报卡片，并按节流发声。"""
        fresh = []
        for s in signals or []:
            fp = f"{s['ts']}|{s['symbol']}|{s['side']}|{s['action']}"
            if fp in self._announced:
                continue
            self._announced.append(fp)
            fresh.append(s)
        # 保证金变动和开平仓同等对待：他空仓时提钱不会产生 signals，
        # 只靠 signals 做门禁会把这条吞掉。
        funds = []
        for a in actions or []:
            if a.get("type") != "FUND_CHANGE":
                continue
            fp = (f"FUND|{a.get('wallet_before')}|{a.get('wallet_after')}|"
                  f"{a.get('wall')}")
            if fp in self._announced:
                continue
            self._announced.append(fp)
            funds.append(a)
        if not fresh and not funds:
            return
        # 组内也按时间倒序：播报是「最新在最上面」，同一轮进来多条时
        # 不能靠上游给的顺序（引擎给的是重放用的正序）。
        fresh.sort(key=lambda x: x["ts"], reverse=True)
        funds.sort(key=lambda x: float(x.get("wall") or 0), reverse=True)
        # 昵称只认本次会话确认过的那个；没确认就写「未确认」，
        # 不用配置缓存冒充（缓存可能是上一个带单员的）。
        nick = ((meta or {}).get("nickname")
                or (self._lead_verified[1]
                    if self._lead_verified[0] == str(
                        self.cfg.get("portfolio_id") or "") else "")
                or "未确认")
        # 同轮「开→平→再开」时同品种有多个动作，按动作类型配对，
        # 不能用 dict[symbol] 让后一个覆盖前一个。
        acts = list(actions or [])
        simulated = self.mode_box.currentText() == "dry-run"
        blocks: list[tuple[str, str]] = []

        for s in fresh:
            verb = ("开多" if s["side"] == "LONG" else "开空") \
                if s["action"] == "OPEN" else \
                ("平多" if s["side"] == "LONG" else "平空")
            tag = "open" if s["action"] == "OPEN" else "close"
            blocks.append(("rule", "─" * 46 + "\n"))
            blocks.append((tag, f"{s['symbol']} · {verb}\n"))
            blocks.append(("", f"带单员：{nick}\n"))
            blocks.append(("", f"成交数量：{s['qty']}\n"))
            blocks.append(("", f"成交金额：{s['notional']:.2f} USDT\n"))
            blocks.append(("", f"成交均价：{s['price']:.8g}\n"))
            if s.get("realized_pnl") is not None:
                blocks.append(("", f"已实现盈亏：{s['realized_pnl']:.2f} USDT\n"))
            # 公开 bapi 不给他的杠杆与保证金，这几行直接不写 —— 播报是用来
            # 照着操作的，塞「不可用」只是噪音。
            want = "ENTRY" if s["action"] == "OPEN" else "CLOSE_FOLLOW"
            a = next((x for x in acts
                      if x.get("type") == want
                      and x.get("symbol") == s["symbol"]), None)
            if a and a.get("cancel_only"):
                note = "本程序动作：仅撤单（无仓位可平）"
            elif a and a.get("type") == "ENTRY":
                note = ("本程序动作：历史未平仓位挂更优价限价单"
                        if a.get("historical") else
                        f"本程序动作：挂限价单 {a['qty']} @ {a['limit']}")
            elif a and a.get("type") == "CLOSE_FOLLOW":
                note = f"本程序动作：按 {a['close_ratio'] * 100:.1f}% 比例平仓"
            else:
                note = "本程序动作：按操作处理"
            blocks.append(("note", note + ("（模拟）\n" if simulated else "\n")))
            blocks.append(("", f"时间：{s['ts']}\n"))

        for a in funds:
            amt = float(a.get("amount") or 0.0)
            inflow = amt > 0
            blocks.append(("rule", "─" * 46 + "\n"))
            blocks.append(("open" if inflow else "close",
                           f"保证金{'转入' if inflow else '转出'}\n"))
            blocks.append(("", f"带单员：{nick}\n"))
            blocks.append(("", f"变动金额：{amt:+.8f} USDT\n"))
            blocks.append(("", f"钱包余额："
                               f"{float(a.get('wallet_before') or 0):.8f} → "
                               f"{float(a.get('wallet_after') or 0):.8f} USDT\n"))
            blocks.append(("", "保证金余额："
                               f"{float(a.get('margin_balance') or 0):.8f} USDT\n"))
            blocks.append(("", "未实现盈亏："
                               f"{float(a.get('unrealized') or 0):+.8f} USDT\n"))
            blocks.append(("na", "口径：" + ("空仓精确" if a.get("anchored")
                                            else "持仓推算（含浮亏估算）") + "\n"))
            blocks.append(("note", "影响：后续开仓的敞口比例按新钱包余额计算\n"))
            if a.get("wall"):
                stamp = datetime.fromtimestamp(
                    float(a["wall"])).strftime("%Y-%m-%d %H:%M:%S")
                blocks.append(("", f"时间：{stamp}\n"))

        self._announce_append(blocks)
        if (self.cb_sound.isChecked()
                and time.monotonic() - self._last_sound > 3.0):
            self._last_sound = time.monotonic()
            self._play_sound()

    def _switch_lead(self, new_id: str, old_id: str) -> None:
        """换带单员：重建数据源、清缓存、清去重水位；挂单状态保留。"""
        with self._api_lock:
            self.api = None
            self._api_pid = ""
            self._detail_cache = None
            self._detail_ts = 0.0
        self.cfg["portfolio_id"] = new_id
        self.cfg["lead_nickname"] = ""
        self.cfg["lead_nickname_pid"] = ""
        # 换人即作废：标题必须先回到「未连接」，等新带单员的接口返回再显示，
        # 否则旧名字会顶着新 portfolioId 显示一段时间。
        self._lead_verified = ("", "")
        self.lead_title.setText(self._lead_display())
        # 引擎负责清 seen / 账本、重置跟单起点、下轮重建基线。
        self.engine.begin_following(new_id)
        self._refresh_lead_name()
        self._lead_label.setText(f"当前：{new_id}")
        self._log("INFO", f"跟单目标已切换 {old_id} -> {new_id}，"
                          "起点已重置，带单员原有仓位不跟随，下轮建立基线")

    def _apply_lead(self, *_):
        new_id = self.edit_pid.text().strip()
        if not new_id.isdigit() or len(new_id) < 10:
            self._log("ERROR", "portfolioId 应是带单本链接里的纯数字 ID")
            return
        old = self.cfg["portfolio_id"]
        if new_id != old:
            self._switch_lead(new_id, old)
        self._save_config()
        self._log("INFO", "运行配置已保存")

    # ---------- 日志 ----------
    def clear_log(self, *_):
        self.log_records.clear()
        self._redraw_log()

    @staticmethod
    def _log_role(level: str) -> str:
        return {"ACTION": "ACCENT", "WARN": "WARN", "ERROR": "WARN"}.get(level, "")

    def _update_log_status(self, shown: int | None = None):
        if shown is None:
            shown = self.tree_log.rowCount()
        self.log_status.setText(
            f"共 {len(self.log_records)} 条 · 当前显示 {shown} 条 · "
            f"上限 {self.log_records.maxlen}")

    def _redraw_log(self):
        level = self.log_level.currentText() if hasattr(self, "log_level") else "ALL"
        kw = (self.log_search.text().strip().lower()
              if hasattr(self, "log_search") else "")
        t = self.theme.tokens()
        rows = []
        for rec in self.log_records:
            if level != "ALL" and rec[1] != level:
                continue
            if kw and kw not in rec[2].lower():
                continue
            rows.append(rec)
        aligns = self.tree_log._aligns
        self.tree_log.setRowCount(len(rows))
        for r, rec in enumerate(rows):
            role = self._log_role(rec[1])
            color = t.get(role) if role else None
            for c, v in enumerate(rec):
                self.tree_log.setItem(r, c, self._mk_item(v, aligns[c],
                                                          mono=c == 0,
                                                          color=color,
                                                          tip=v))
        self._update_log_status(len(rows))
        if rows:
            self.tree_log.scrollToBottom()

    def _log(self, level: str, text: str):
        ui_log = getattr(self, "_ui_logger", None)
        if ui_log is not None:
            ui_log.log({"ACTION": logging.INFO, "WARN": logging.WARNING,
                        "ERROR": logging.ERROR}.get(level, logging.INFO), text)
        rec = (datetime.now().strftime("%H:%M:%S"), level, text)
        self.log_records.append(rec)          # deque(maxlen=N) 自动淘汰最旧
        level_filter = self.log_level.currentText()
        kw = self.log_search.text().strip().lower()
        if (level_filter == "ALL" or level_filter == level) and (
                not kw or kw in text.lower()):
            t = self.theme.tokens()
            role = self._log_role(level)
            color = t.get(role) if role else None
            aligns = self.tree_log._aligns
            r = self.tree_log.rowCount()
            self.tree_log.insertRow(r)
            for c, v in enumerate(rec):
                self.tree_log.setItem(r, c, self._mk_item(v, aligns[c],
                                                          mono=c == 0,
                                                          color=color,
                                                          tip=v))
            if r + 1 > self.log_records.maxlen:
                self.tree_log.removeRow(0)
            self.tree_log.scrollToBottom()
        self._update_log_status()

    # ---------- 收尾 ----------
    def closeEvent(self, event):
        self.stop_polling()
        set_keep_awake(False)
        self._timer.stop()
        logging.getLogger().removeHandler(self._handler)
        if self.store:
            self.store.close()
        super().closeEvent(event)


def selftest(app: QApplication) -> dict:
    # 自检会点「切主题」，而 _toggle_theme() 末尾要存盘。不把配置路径挪走的话，
    # 每跑一次回归就把用户的真实 gui-config.json 覆盖一遍（曾把
    # better_price_enabled 写成 false、theme 写成 dark）。指向临时文件，
    # 顺带保证自检从 DEFAULTS 起步、不受本机配置影响。
    global CONFIG_FILE, LOG_FILE
    real_cfg = CONFIG_FILE
    real_log = LOG_FILE
    tmp_cfg = Path(tempfile.gettempdir()) / "leadcopy-selftest-config.json"
    tmp_log = Path(tempfile.gettempdir()) / "leadcopy-selftest.log"
    CONFIG_FILE = tmp_cfg
    # 日志同理：自检会跑一批假的带单员成交，写进真实日志就成了
    # 「11:13 开了 BTCUSDT」这种根本不存在的历史，复盘时误导人。
    LOG_FILE = tmp_log
    try:
        return _selftest_body(app)
    finally:
        CONFIG_FILE = real_cfg
        LOG_FILE = real_log
        for p in (tmp_cfg, tmp_log):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _qty(v: float) -> str:
    """数量去掉尾随零：10.812000 -> 10.812，0.568000 -> 0.568。

    6 位小数在列宽里会把「应开数量」挤成省略号，而多数品种的尾随零没有信息量。
    """
    txt = f"{v:.6f}".rstrip("0").rstrip(".")
    return txt or "0"


def format_action(a: dict) -> str:
    """把引擎的动作字典写成一行可读的中文。

    原来直接 `json.dumps` 整条 dict，事件日志里是
    `{"type": "ENTRY", "symbol": "ZECUSDT", "side": "SHORT", ...}` 这种被
    列宽截断的原始串 —— 复盘时既看不懂也读不全。动作是给人看的，
    字段名和布尔值都该翻成人话；原始 dict 仍完整保留在 `engine.actions` 里，
    需要程序化处理时取那个。
    """
    kind = a.get("type") or "?"
    sym = a.get("symbol") or ""
    if kind == "ENTRY":
        side = "多" if a.get("side") == "LONG" else "空"
        bits = [f"挂单 {sym} {side} {a.get('qty')} 张 @ {a.get('limit')}",
                f"带单员名义 {a.get('lead_notional')} U"]
        ratio = a.get("notional_ratio")
        if isinstance(ratio, (int, float)):
            bits.append(f"占带单员总仓位 {ratio * 100:.2f}%")
        bits.append(f"{a.get('leverage')}x · {a.get('price_mode')}")
        if a.get("historical"):
            bits.append("历史未平仓位")
        return "开仓跟随：" + " · ".join(bits)
    if kind == "CLOSE_FOLLOW":
        if a.get("cancel_only"):
            return (f"平仓跟随 {sym}：本程序无仓位，仅撤单"
                    f"（撤挂单={'是' if a.get('cancelled_pending') else '否'}）")
        pct = a.get("close_ratio")
        pct_txt = f"{pct * 100:.2f}%" if isinstance(pct, (int, float)) else "?"
        return (f"平仓跟随 {sym}：按 {pct_txt} 平 {a.get('close_qty')} 张"
                f"（撤挂单={'是' if a.get('cancelled_pending') else '否'}）")
    if kind == "ENTRY_SETTLED":
        return (f"挂单结果 {sym}：" +
                ("已成交" if a.get("filled") else "未成交（已撤单）"))
    if kind == "ENTRY_TIMEOUT":
        return f"入场挂单 {sym} 超时撤单 —— 这一单算漏了"
    if kind == "RECONCILE_ADOPT":
        syms = a.get("symbols") or []
        return f"挂单对账：接管 {len(syms)} 笔本程序遗留挂单（{', '.join(syms)}）"
    if kind == "FUND_CHANGE":
        amt = float(a.get("amount") or 0.0)
        return (f"带单员资金变动 {amt:+.4f} USDT"
                f"（钱包 {float(a.get('wallet_before') or 0):.4f} → "
                f"{float(a.get('wallet_after') or 0):.4f}，"
                f"{'空仓精确' if a.get('anchored') else '持仓推算'}）")
    return f"{kind} {json.dumps(a, ensure_ascii=False)}"


def sim_live_check(ui) -> dict:
    """点「按实盘权益」之后，这一页就是实盘对账，文案不能还写着「模拟」。

    实测过的坑：资金对齐了，摘要还是「模拟资金 92.63 U」，用户以为按钮没反应。
    """
    saved = ui.edit_sim_equity.text()
    saved_eq = ui.rule.my_equity
    ui._sim_use_real_equity()
    out = {"title": ui.sim_title.text(), "summary": ui.sim_summary.text()}
    # 权益每轮都在变（浮盈），口径不能因为波动 5% 就切回「模拟」
    ui.rule.my_equity = saved_eq * 1.05
    ui._refresh_sim()
    out["after_move_title"] = ui.sim_title.text()
    out["after_move_summary"] = ui.sim_summary.text()
    ui.rule.my_equity = saved_eq
    ui._sim_pin_real = False
    ui.edit_sim_equity.setText(saved or "1000")
    ui._refresh_sim()
    out["unpinned_title"] = ui.sim_title.text()
    return out


def lead_title_check(ui) -> dict:
    """标题里的带单员标识必须「确认后才显示」，不能靠配置缓存。

    未确认时写「未连接」；确认后写成「昵称 · portfolioId 尾 6 位」。
    """
    saved = ui._lead_verified
    # 先清掉确认状态：不管之前跑过什么，未确认就必须是「未连接」
    ui._lead_verified = ("", "")
    ui.lead_title.setText(ui._lead_display())
    before = ui.lead_title.text()
    try:
        ui._set_lead_nickname("自检带单员")
        after = ui.lead_title.text()
        pid = str(ui.cfg.get("portfolio_id") or "")
        tail_ok = pid[-6:] in after if pid else True
    finally:
        ui._lead_verified = saved
        ui.lead_title.setText(ui._lead_display())
    return {"before": before, "after": after,
            "unconfirmed_blank": before == "未连接",
            "tail_ok": tail_ok}


def self_drag_check(ui) -> dict:
    """成交表的列宽必须能手动拖：Stretch 模式的列拖不动。

    直接改列宽，改不动就说明这一列还是 Stretch。
    """
    hdr = ui.tree_fills.horizontalHeader()
    before = ui.tree_fills.columnWidth(0)
    hdr.resizeSection(0, before + 60)
    after = ui.tree_fills.columnWidth(0)
    hdr.resizeSection(0, before)
    return {"before": before, "after": after, "draggable": after == before + 60}


def _selftest_body(app: QApplication) -> dict:
    ui = App(enable_store=False)
    ui._mute_sound = True
    # 自检不依赖磁盘上的用户配置：固定 dry-run + 默认幅度
    ui.mode_box.setCurrentText("dry-run")
    ui.cb_better.setChecked(True)
    ui.edit_improve.setText("0.3")
    ui.edit_slip.setText("0.3")
    ui._on_price_mode_change()
    fills = parse(WEB_SAMPLE)
    ui.engine.needs_baseline = False        # 跳过基线，直接跑事件链路
    # 自检用固定历史时间戳做样本，关掉运行时的超龄信号检查
    ui.rule.signal_max_age_s = 0
    ui._process_sync(fills)
    app.processEvents()

    # 轮询间隔的回归保护：running 在轮询期间恒为 set，用 running.wait(N)
    # 会立刻返回，实际间隔只剩一次网络请求。等待必须用独立的 _poll_wake。
    ui._poll_wake.clear()
    t0 = time.monotonic()
    ui._poll_wake.wait(0.2)
    wake_ms = (time.monotonic() - t0) * 1000
    ui.running.set()
    t1 = time.monotonic()
    ui.running.wait(0.2)
    running_ms = (time.monotonic() - t1) * 1000
    ui.running.clear()
    wait_separated = wake_ms >= 150 and running_ms < 50

    # 开关按钮只切显示，不启动后台线程；自检不联网。
    ui._set_poll_button(True)
    running_text = ui.poll_btn.text()
    ui._set_poll_button(False)
    idle_text = ui.poll_btn.text()

    # 主题：一次 setStyleSheet 换整棵树，QSS 文本必须真的变了；
    # 且表格/播报里手写的颜色要被重涂成新色板。
    ann_before = ui.announce_plain_text()
    start_theme = ui.theme.current
    dark_qss = app.styleSheet()
    ui._toggle_theme()
    app.processEvents()
    light_qss = app.styleSheet()
    fills_color = (ui.tree_fills.item(0, 0).foreground().color().name()
                   if ui.tree_fills.rowCount() else None)
    ui._toggle_theme()
    app.processEvents()
    dark_color = (ui.tree_fills.item(0, 0).foreground().color().name()
                  if ui.tree_fills.rowCount() else None)

    # 盈亏页：喂一份构造好的快照，验证「净值 = 钱包 + 浮盈」这条口径真的
    # 走到了控件上。不联网，也不写库。
    ui._render_pnl({
        "mode": "live",
        "curve": [{"ts": 1.0, "equity": 100.0, "unreal": 0.0},
                  {"ts": 2.0, "equity": 100.0, "unreal": 5.0}],
        "pos_rows": [{"symbol": "BTCUSDT", "side": "LONG", "qty": 0.01,
                      "entry": 68000.0, "mark": 68500.0, "unreal": 5.0,
                      "notional": 685.0}],
        "trades": [{"ts": 2.0, "kind": "CLOSE", "symbol": "BTCUSDT",
                    "side": "LONG", "qty": 0.01, "price": 68500.0,
                    "notional": 685.0, "realized": 5.0, "note": "带单员平仓"}],
        "unreal": 5.0, "realized": 5.0})
    pnl_probe = {
        "cols_pos": ui.tree_pnl_pos.columnCount(),
        "cols_trade": ui.tree_pnl_trade.columnCount(),
        "net": ui.pnl_metrics["net"].text(),
        "total": ui.pnl_metrics["total"].text(),
        "mode": ui.pnl_mode.text(),
        "curve_pts": len(ui.pnl_curve._pts),
        "pos_rows": ui.tree_pnl_pos.rowCount(),
        "trade_rows": ui.tree_pnl_trade.rowCount(),
    }
    ui._last_pnl = {}

    # 配色方案：两套方案必须真的把 POS / NEG 换位，
    # 否则「开多绿、开空红」这个开关就是个摆设。
    tok_b = dict(ui.theme.tokens())
    ui.theme.set_scheme("cn")
    tok_c = dict(ui.theme.tokens())
    ui.theme.set_scheme("binance")
    scheme_probe = {"binance_pos": tok_b["POS"], "cn_pos": tok_c["POS"],
                    "swapped": tok_b["POS"] == tok_c["NEG"]}
    # 杠杆：改输入框后必须进配置并写进 rule
    ui.edit_leverage.setText("50")
    lev_cfg = ui._collect_config() or {}
    ui.edit_leverage.setText(str(ui.cfg["leverage"]))

    result = {
        "scheme": scheme_probe,
        "leverage": {"cfg": lev_cfg.get("leverage"),
                     "rule": ui.rule.leverage},
        "poll_wait": {"wake_ms": round(wake_ms),
                      "running_ms": round(running_ms),
                      "separated": wait_separated},
        "default_tab": {"current": ui.nb.currentIndex(),
                        "title": ui.nb.tabText(ui.nb.currentIndex()),
                        "pages": ui.nb.count()},
        "poll_toggle": {"running": running_text, "idle": idle_text},
        "lead_header": ui.lead_title.text(),
        "lead_title_check": lead_title_check(ui),
        "titlebar": {
            "height": ui.titlebar.height(),
            "expected": TITLEBAR_H,
            "frameless": bool(ui.windowFlags() & Qt.FramelessWindowHint),
            "qss_themed": "QWidget#titlebar" in app.styleSheet(),
            "win_roles": [ui.btn_min.property("role"),
                          ui.btn_max.property("role"),
                          ui.btn_close.property("role")],
        },
        "tabs": [ui.nb.tabText(i) for i in range(ui.nb.count())],
        "events_rows": ui.tree_fills.rowCount(),
        "pnl": pnl_probe,
        "fills_drag": self_drag_check(ui),
        "fills_table": {
            "cols": ui.tree_fills.columnCount(),
            "heads": [ui.tree_fills.horizontalHeaderItem(i).text()
                      for i in range(ui.tree_fills.columnCount())],
            "first_open": next(
                ([ui.tree_fills.item(r, c).text()
                  for c in range(ui.tree_fills.columnCount())]
                 for r in range(ui.tree_fills.rowCount())
                 if ui.tree_fills.item(r, 1).text() == "开"), None),
        },
        "actions": list(ui.engine.actions)[-4:],
        "pending": ui.engine.state()["pending"],
        "calls": [c for c in ui.broker.calls if c[0] in ("LIMIT", "CANCEL_ALL")],
        "announce_text": ui.announce_plain_text(),
        "sim": {
            "cols": ui.tree_sim.columnCount(),
            "rows": ui.tree_sim.rowCount(),
            "equity": ui.edit_sim_equity.text(),
            "first": ([ui.tree_sim.item(0, c).text()
                       for c in range(ui.tree_sim.columnCount())]
                      if ui.tree_sim.rowCount() else None),
            "summary": ui.sim_summary.text(),
            "live": sim_live_check(ui),
        },
        "order": {
            # 成交事件：最新一条必须在第一行
            "fills_desc": (
                [ui.tree_fills.item(r, 0).text()
                 for r in range(ui.tree_fills.rowCount())]
                == sorted([ui.tree_fills.item(r, 0).text()
                           for r in range(ui.tree_fills.rowCount())],
                          reverse=True)),
            # 动作日志必须是中文人话，不是 json.dumps 出来的原始 dict
            "action_log": [r[2] for r in ui.log_records if r[1] == "ACTION"][:4],
            # 操作播报：最新一条必须画在最上面
            "announce_stamps": re.findall(
                r"时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
                ui.announce_plain_text()),
        },
        "price_mode": ui.executor.price_mode_text(),
        "theme": {"start": start_theme, "dark_len": len(dark_qss),
                  "light_len": len(light_qss),
                  "changed": dark_qss != light_qss,
                  "fills_color_light": fills_color,
                  "fills_color_dark": dark_color,
                  "back_to_start": ui.theme.current,
                  "announce_len_before": len(ann_before),
                  "announce_len_after": len(ui.announce_plain_text())},
        "entry_price_compare": {
            "better": entry_limit_price(68000.0, "LONG", 0.00001,
                                        0.003, 0.003, True),
            "tolerance": entry_limit_price(68000.0, "LONG", 0.00001,
                                           0.003, 0.003, False),
        },
        "cfg": ui._cfg,
    }

    # 异步链路：取数批次 -> 后台引擎 -> 快照回主线程渲染。
    # 同时验证关掉更优开关后，挂单价确实变成容差价（多单高于信号价）。
    fresh = Fill(ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 action="OPEN", side="LONG", symbol="BTCUSDT",
                 price=68000.0, qty=1.0, notional=68000.0)
    ui.cb_better.setChecked(False)
    ui._on_price_mode_change()
    ui.events.put(("batch", [fresh], ui._cfg, {}))
    ui._drain_events()
    for _ in range(60):
        app.processEvents()
        ui._drain_events()
        if not ui._engine_busy:
            break
        time.sleep(0.02)
    btc_limits = [c[4] for c in ui.broker.calls
                  if c[0] == "LIMIT" and c[1] == "BTCUSDT"]
    result["async_chain"] = {
        "busy": ui._engine_busy,
        "dropped_batches": ui._dropped_batches,
        "tolerance_limit": btc_limits[-1] if btc_limits else None,
        "price_mode": ui.executor.price_mode_text(),
        "log_rows": ui.tree_log.rowCount(),
        "tree_fills_rows": ui.tree_fills.rowCount(),
    }
    # 换带单员：清 seen 水位、重建数据源、重置基线，且不写盘污染用户配置。
    old_pid = ui.cfg["portfolio_id"]
    seen_before = len(ui.engine.seen)
    ui._switch_lead("5075281354358777857", old_pid)
    result["lead_switch"] = {
        "pid": ui.cfg["portfolio_id"],
        "seen_before": seen_before,
        "seen_after": len(ui.engine.seen),
        "needs_baseline": ui.engine.needs_baseline,
        "api_reset": ui.api is None,
    }
    ui.close()
    return result


STARTUP_LNK = (Path(os.getenv("APPDATA") or str(Path.home()))
               / "Microsoft/Windows/Start Menu/Programs/Startup/币安带单跟单.lnk")


def _lnk_mentions(path: Path, needle: str) -> bool:
    """快捷方式里是否指向某个路径片段。

    不走 WScript.Shell：那要起 COM / PowerShell，只为判断「这个 .lnk 是不是
    本项目的」不值得。.lnk 把目标路径按 ASCII 和 UTF-16LE 各存一份，直接扫
    字节就够准。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    return (needle.encode("ascii", "ignore") in raw
            or needle.encode("utf-16-le") in raw)


def _pythonw() -> str:
    """优先用 pythonw.exe：没有控制台窗口。

    用 python.exe 的话登录时会先弹一个黑框（WindowStyle=7 只是把它最小化），
    而且那个控制台是父进程，用户顺手关掉黑框会把跟单一起带走。
    """
    exe = Path(sys.executable)
    pw = exe.with_name("pythonw.exe")
    return str(pw) if pw.exists() else str(exe)


def sync_autostart(enabled: bool) -> str:
    """按配置创建 / 删除开机自启快捷方式。

    配置是唯一真源：勾了就写、取消就删。手工往启动文件夹里丢快捷方式的话，
    配置说「关」但快捷方式还在，行为就和界面显示的对不上。

    走 -EncodedCommand（base64 + UTF-16LE）而不是 -Command：命令里带引号和
    中文路径，-Command 会被 Windows 的命令行转义规则拆坏 —— 实测
    `$s.Arguments` 被写成了字面量 `+ $env:TEN_ARGS +`。
    """
    if sys.platform != "win32":
        return "skip"
    if not enabled:
        try:
            STARTUP_LNK.unlink(missing_ok=True)
            return "已关闭"
        except OSError as e:
            return f"fail {type(e).__name__}"
    script = ROOT / "gui.py"
    if not script.exists():
        return f"fail 找不到 {script}"
    ps = ("$w = New-Object -ComObject WScript.Shell; "
          f"$s = $w.CreateShortcut('{STARTUP_LNK}'); "
          f"$s.TargetPath = '{_pythonw()}'; "
          f"$s.Arguments = '\"{script}\"'; "
          f"$s.WorkingDirectory = '{ROOT}'; "
          "$s.Description = '币安带单跟单'; "
          "$s.WindowStyle = 7; "
          "$s.Save()")
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return f"fail {type(e).__name__}"
    if r.returncode != 0:
        # PowerShell 按系统代码页输出，不能假定 UTF-8
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[:60]
        return f"fail rc={r.returncode} {err}"
    return "已开启"


def ensure_autostart() -> str:
    """启动时对账：配置说「开」，启动文件夹里就必须有一条指向本项目的
    快捷方式，且没有别的残留。

    原来只在配置值变化时才写快捷方式 —— 快捷方式被手工删掉、被安全软件
    清掉、或者指到了别的解释器，界面还照样显示「已勾选」，看起来就是
    「开机自启没用」。现在每次启动都查一遍，正常情况只是读几个文件。

    残留判定按「.lnk 里是否提到本项目目录名」，能连历史上编码坏掉的
    `澶栧満璺熷崟.lnk` 一起清掉 —— 它同样会在登录时拉起一个实例，
    只是被单实例锁挡回去了。
    """
    if sys.platform != "win32":
        return ""
    try:
        stale = [p for p in STARTUP_LNK.parent.glob("*.lnk")
                 if p != STARTUP_LNK and _lnk_mentions(p, ROOT.name)]
    except OSError:
        stale = []
    healthy = (STARTUP_LNK.exists()
               and _lnk_mentions(STARTUP_LNK, ROOT.name))
    if healthy and not stale:
        return ""
    for p in stale:
        try:
            p.unlink()
        except OSError:
            pass
    state = sync_autostart(True)
    note = f"，清理残留 {len(stale)} 个" if stale else ""
    return ("已重建" if state == "已开启" else state) + note



def acquire_single_instance():
    """同一时间只允许一个实例。

    开机自启 + 手动双击是很容易叠在一起的两条路：两个实例各自轮询、
    各自下单，等于仓位翻倍，而且彼此不知道对方挂过什么。
    用文件锁，进程退出自动释放，不会留死锁。
    """
    if sys.platform != "win32":
        return True
    import msvcrt
    try:
        fh = open(ROOT / "gui.lock", "a+")
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh          # 句柄要一直拿着，关掉就等于解锁
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--offscreen", action="store_true",
                    help="无窗口运行（自检默认开启）")
    ap.add_argument("--autostart", action="store_true",
                    help="强制自动开始轮询（兼容旧快捷方式；现在由配置 "
                         "auto_poll 决定）")
    args = ap.parse_args()
    if args.selftest or args.offscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(sys.argv[:1])
    # Fusion 让 QSS 完全接管绘制；Windows 原生风格会忽略一部分 QSS 规则
    # （尤其 QComboBox 下拉和 QTabBar），换主题时看起来像没换干净。
    app.setStyle("Fusion")

    if args.selftest:
        # 自检绝不允许碰真实配置：跑一次回归就把用户的实盘参数改掉，
        # 下次启动会静默用错口径。这里守一道，改坏了立刻红。
        cfg_before = CONFIG_FILE.read_bytes() if CONFIG_FILE.exists() else None
        out = selftest(app)
        cfg_after = CONFIG_FILE.read_bytes() if CONFIG_FILE.exists() else None
        assert cfg_before == cfg_after, "自检改写了真实的 gui-config.json"
        # 显式断言：失败必须让 run_all.py 变红，不能「不崩就算过」
        assert out["default_tab"]["current"] == 0, "启动默认 tab 应为跟单盈亏"
        assert out["default_tab"]["title"] == "跟单盈亏", "默认 tab 标题不对"
        assert out["default_tab"]["pages"] == 6, "tab 数量变化后需同步检查"
        assert out["tabs"] == ["跟单盈亏", "操作播报", "成交事件",
                               "未成交挂单", "事件日志", "运行配置"], \
            "tab 顺序变了：结果在前、设置在最后"
        assert out["scheme"]["swapped"], "配色方案没有真的对调 POS/NEG"
        assert out["leverage"]["cfg"] == "50", "杠杆输入没有进配置"
        pn = out["pnl"]
        assert pn["cols_pos"] == 7 and pn["cols_trade"] == 7, "盈亏页列数不对"
        assert pn["net"] == "105.00", "净值必须等于钱包余额 + 浮动盈亏"
        assert pn["total"] == "+10.00", "累计盈亏 = 区间净值变化 + 已实现"
        assert pn["curve_pts"] == 2 and pn["pos_rows"] == 1 \
            and pn["trade_rows"] == 1, "盈亏页没有把快照画到控件上"
        assert out["poll_wait"]["separated"], "轮询等待被 running.wait 短路"
        assert out["poll_toggle"]["running"] == "停止轮询", "开关按钮文字未切换"
        assert out["entry_price_compare"]["better"] < 68000, "更优价计算错误"
        assert out["entry_price_compare"]["tolerance"] > 68000, "容差价计算错误"
        tol = out["async_chain"]["tolerance_limit"]
        assert tol is not None and tol > 68000, "异步链路未产出容差挂单价"
        assert out["async_chain"]["dropped_batches"] == 0, "异步链路丢了批次"
        assert out["lead_switch"]["needs_baseline"], "换人后未重建基线"
        assert out["lead_switch"]["seen_after"] == 0, "换人后 seen 未清空"
        th = out["theme"]
        assert th["changed"], "切主题后 QSS 文本未变化"
        assert th["back_to_start"] == th["start"], "二次切主题未回到原主题"
        assert th["fills_color_light"] and th["fills_color_dark"], "表格行颜色未重涂"
        assert th["fills_color_light"] != th["fills_color_dark"], \
            "表格行颜色没跟着主题变"
        assert th["announce_len_before"] == th["announce_len_after"], \
            "切主题后播报正文被重复追加"
        dr = out["fills_drag"]
        assert dr["draggable"], "成交表的列宽拖不动（被设成了 Stretch）"
        ft = out["fills_table"]
        assert ft["cols"] == 9, "成交事件表应有 9 列（含应开/实开）"
        assert ft["heads"][-2:] == ["应开 USDT", "实开 USDT"],             "成交事件表列名不对"
        assert ft["first_open"] and ft["first_open"][7] not in ("", "—"),             "开仓行没算出「应开」数量"
        tb = out["titlebar"]
        assert tb["frameless"], "窗口应为无边框 + 自绘标题栏"
        assert tb["height"] == tb["expected"], "自绘标题栏高度不对"
        assert tb["qss_themed"], "标题栏没有走 QSS，切主题时不会跟着变"
        assert tb["win_roles"] == ["winbtn", "winbtn", "winclose"], \
            "窗口按钮的 role 不对，hover 配色会失效"
        lt = out["lead_title_check"]
        assert lt["unconfirmed_blank"], \
            "未连接时标题应为「未连接」，不能显示配置缓存里的昵称"
        assert "自检带单员" in lt["after"] and lt["tail_ok"], \
            "确认后标题应显示「昵称 · portfolioId 尾号」"
        lv = out["sim"]["live"]
        assert "实盘口径" in lv["title"], \
            "按实盘权益后标题仍写着「模拟资金」"
        assert "本程序权益" in lv["summary"] and "模拟" not in lv["summary"], \
            "按实盘权益后摘要仍在说「模拟」"
        assert "实盘口径" in lv["after_move_title"] \
            and "模拟" not in lv["after_move_summary"], \
            "权益波动后口径被切回「模拟」—— 应按锁定状态判断，不靠数值接近"
        assert "模拟资金" in lv["unpinned_title"], \
            "手动改资金后应切回模拟口径"
        sm = out["sim"]
        assert sm["cols"] == 10, "仓位管理表应有 10 列"
        assert sm["equity"] == "1000", "模拟资金默认值应为 1000"
        assert sm["rows"] >= 1 and sm["first"], "仓位管理表没有算出任何品种"
        assert sm["first"][5] not in ("", "0.000000"), \
            "仓位管理的「应开数量」为 0，等比缩放没跑通"
        assert "缩放" in sm["summary"], "仓位管理摘要没显示缩放系数"
        od = out["order"]
        assert od["fills_desc"], "成交事件没有按「最新在最上面」排序"
        acts = od["action_log"]
        assert acts and not any(t.lstrip().startswith("{") for t in acts),             "事件日志的动作行还是原始 dict，没翻成中文"
        stamps = od["announce_stamps"]
        assert stamps and stamps[0] == max(stamps),             "操作播报没有按「最新在最上面」排序"
        ann = out["announce_text"]
        assert "不可用" not in ann, "播报里不许出现拿不到的字段（杠杆/保证金）"
        assert "带单员：" in ann and "本程序动作：" in ann, "播报字段不完整"
        assert "（模拟）" in ann, "dry-run 播报未标注模拟"
        print("Qt GUI selftest OK (50 asserts)")
        print(f"  tabs={out['tabs']} events_rows={out['events_rows']}")
        print(f"  poll_wait={out['poll_wait']}")
        print(f"  default_tab={out['default_tab']}")
        print(f"  poll_toggle={out['poll_toggle']} lead_header={out['lead_header']}")
        print(f"  titlebar={out['titlebar']}")
        print(f"  lead_switch={out['lead_switch']}")
        print(f"  price_mode={out['price_mode']}")
        print(f"  theme={out['theme']}")
        print(f"  entry_price_compare={out['entry_price_compare']}")
        print(f"  actions={out['actions']}")
        print(f"  calls={out['calls']}")
        print(f"  fills_drag={out['fills_drag']}")
        print(f"  order={out['order']}")
        print(f"  sim={out['sim']}")
        print(f"  lead_title={out['lead_title_check']}")
        print(f"  sim_live={out['sim']['live']}")
        print(f"  fills_table={out['fills_table']['heads']} "
              f"first_open={out['fills_table']['first_open']}")
        print(f"  async_chain={out['async_chain']}")
        return

    lock = acquire_single_instance()
    if lock is None:
        print("已有一个实例在运行（gui.lock 被占用），本次退出。"
              "两个实例会各自下一遍单，仓位会翻倍。")
        raise SystemExit(4)

    ui = App()
    ui.show()
    if args.autostart or as_bool(ui.cfg.get("auto_poll"), True):
        # 开机那一刻网络和本地代理可能还没起来，_refresh_cfg() 会失败并
        # 直接返回，不重试就整夜不跟单。所以这里退避重试，最多 20 次。
        tries = {"n": 0}

        def _auto_start():
            tries["n"] += 1
            try:
                ui.start_polling()
            except Exception as e:                  # 配置还没读出来之类
                ui._log("WARN", f"自动开始轮询失败（第 {tries['n']} 次）：{e}")
            if not ui.running.is_set() and tries["n"] < 20:
                QTimer.singleShot(15000, _auto_start)

        QTimer.singleShot(1500, _auto_start)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
