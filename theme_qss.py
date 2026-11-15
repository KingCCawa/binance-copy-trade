"""Qt 主题：全局 QSS + ThemeManager。

为什么不用 QPalette：部分控件在不同平台仍走系统绘制，不遵循调色板，
换主题时会漏掉。QSS 是 Qt 自己的样式引擎，`setStyleSheet()` 是整体替换，
Qt 在下一帧统一重绘所有控件，不存在「一部分先变、一部分后变」的撕裂。

色板与 tkinter 版（`theme.py`）同源，两套界面看起来是同一个东西。

几条写 QSS 时必须守的规矩：
- 只认 `background-color`，`background` 简写会被静默忽略。
- 底色别挂在通配 `QWidget` 上，否则每个布局格子都被刷色，卡片里露深色补丁。
- 圆角、描边宽度走下面的刻度表，不要就地写魔数。
"""
from __future__ import annotations

from string import Template

# 圆角刻度：卡片 14 / 表格 12 / 按钮 9 / 输入框 8。
# 数值来源是 tkinter 版的 RoundedCard(radius=14) 与 RoundedButton(radius=9)，
# 两套界面切过去不应该有「圆角变了」的感觉。
R_CARD, R_TABLE, R_BTN, R_INPUT = 14, 12, 9, 8

DARK = {
    "BG": "#0B0E11", "PANEL": "#181A20", "ROW_ALT": "#1D2028",
    "FIELD": "#2B3139", "HOVER": "#3A4149", "BORDER": "#2E333B",
    "TEXT": "#EAECEF", "MUTED": "#B7BDC6", "FAINT": "#848E9C",
    "ACCENT": "#F0B90B", "ACCENT_HOVER": "#F8D12F", "PRESS": "#C99406",
    "ON_ACCENT": "#0B0E11", "SEL_BG": "#3A3520",
    "POS": "#DC5B50", "NEG": "#2EBD85", "WARN": "#E0AE4E",
    "DANGER": "#B5453C", "DANGER_HOVER": "#C95147", "DANGER_PRESS": "#98362F",
    "ON_DANGER": "#FFF3F1", "WIN_CLOSE": "#E81123",
    "DIM_BG": "#0B0E11", "DIM_FG": "#555C66",
    "DIM_BD": "#23272E", "DIM_CAP": "#525A65",
}
LIGHT = {
    "BG": "#FFFFFF", "PANEL": "#FAFAFA", "ROW_ALT": "#F5F6F8",
    "FIELD": "#F1F2F4", "HOVER": "#E4E7EB", "BORDER": "#E3E6EA",
    "TEXT": "#1E2026", "MUTED": "#5E6672", "FAINT": "#929AA5",
    "ACCENT": "#F0B90B", "ACCENT_HOVER": "#E1AC00", "PRESS": "#C99406",
    "ON_ACCENT": "#1E2026", "SEL_BG": "#FFF4CC",
    "POS": "#C4453E", "NEG": "#0E9F6E", "WARN": "#B7791F",
    "DANGER": "#C4453E", "DANGER_HOVER": "#B03A34", "DANGER_PRESS": "#9A322C",
    "ON_DANGER": "#FFFFFF", "WIN_CLOSE": "#E81123",
    "DIM_BG": "#F7F8F9", "DIM_FG": "#AEB5BE",
    "DIM_BD": "#E8EAED", "DIM_CAP": "#B4BAC2",
}
THEMES = {"dark": DARK, "light": LIGHT}
# 当前生效的色板；自绘控件（如 ComboBox 的箭头）从这里取色
CURRENT: dict = dict(DARK)

# 用 $VAR 而不是 {}：QSS 里全是花括号，str.format 会打架
_QSS = Template("""
* { outline: none; }

QWidget {
    color: $TEXT;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 10pt;
}

/* 底色只给顶层容器。若把 background-color 挂在通配的 QWidget 上，
   每个普通容器（布局格子、滚动视口）都会被刷成窗口底色，放进卡片里
   就是一块块深色补丁。留空则由父级透上来，才是想要的效果。 */
QMainWindow, QDialog, QWidget#root { background-color: $BG; }

/* ---------- 卡片 ----------
   2px 描边 + HOVER 色（比 BORDER 亮一档）。tkinter 版就是这么画的：
   1px BORDER 会让卡片失去轮廓，看起来「薄」且和背景糊在一起。 */
QFrame#card {
    background-color: $PANEL;
    border: 2px solid $HOVER;
    border-radius: 14px;
}
QFrame#hline { background-color: $BORDER; border: none; }

/* ---------- 文字 ---------- */
QLabel { background-color: transparent; color: $TEXT; }
QLabel[role="h1"] { font-size: 15pt; font-weight: 600; letter-spacing: 0.5px; }
QLabel[role="section"] { font-weight: 600; letter-spacing: 0.2px; }
QLabel[role="caption"] { color: $MUTED; font-size: 9pt; }
QLabel[role="help"] { color: $FAINT; font-size: 9pt; }
QLabel[role="metric"] {
    font-family: Consolas, "Courier New", monospace;
    font-size: 14pt; font-weight: 600;
}
QLabel[role="accent"] { color: $ACCENT; font-weight: 600; }
QLabel[role="pos"] { color: $POS; }
QLabel[role="neg"] { color: $NEG; }
QLabel[role="warn"] { color: $WARN; }

/* ---------- 按钮 ----------
   :pressed 靠上下内边距互换模拟「按下去」，QSS 没有 transform。 */
QPushButton {
    background-color: $FIELD;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 9px;
    padding: 6px 14px;
    min-height: 20px;
}
QPushButton:hover {
    background-color: $HOVER;
    border-color: $HOVER;
}
QPushButton:pressed {
    background-color: $HOVER;
    border-color: $ACCENT;
    padding-top: 7px;
    padding-bottom: 5px;
}
QPushButton:disabled {
    background-color: $DIM_BG; color: $DIM_FG; border-color: $DIM_BD;
}

QPushButton[role="accent"] {
    background-color: $ACCENT; color: $ON_ACCENT;
    border: 1px solid $ACCENT; font-weight: 600;
}
QPushButton[role="accent"]:hover {
    background-color: $ACCENT_HOVER; border-color: $ACCENT_HOVER;
}
QPushButton[role="accent"]:pressed {
    background-color: $PRESS; border-color: $PRESS;
}
QPushButton[role="accent"]:disabled {
    background-color: $DIM_BD; color: $DIM_CAP; border-color: $DIM_BD;
}

/* 停止类动作：实心红。和黄色的「开始」形成一对明确的状态色，
   灰底会看起来像禁用。 */
QPushButton[role="danger"] {
    background-color: $DANGER; color: $ON_DANGER;
    border: 1px solid $DANGER; font-weight: 600;
}
QPushButton[role="danger"]:hover {
    background-color: $DANGER_HOVER; border-color: $DANGER_HOVER;
}
QPushButton[role="danger"]:pressed {
    background-color: $DANGER_PRESS; border-color: $DANGER_PRESS;
}

/* ---------- 自绘标题栏 ----------
   窗口是 Qt.FramelessWindowHint 无边框的，这一条就是原来的系统标题栏。
   底色跟窗口同色，只用一条下边框和内容区分。拖动 / 双击最大化 / 四边缩放
   由 gui_qt.App.nativeEvent() 的 WM_NCHITTEST 交回系统。 */
QWidget#titlebar {
    background-color: $BG;
    border-bottom: 1px solid $BORDER;
}
/* 最小化 / 最大化 / 关闭：无底无边，符号由 WinButton.paintEvent 画。
   属性选择器比类型选择器更具体，所以能盖掉上面那条通用 QPushButton。 */
QPushButton[role="winbtn"], QPushButton[role="winclose"] {
    background-color: transparent;
    border: none;
    border-radius: 0;
    padding: 0;
    min-height: 0;
}
QPushButton[role="winbtn"]:hover { background-color: $HOVER; }
QPushButton[role="winbtn"]:pressed { background-color: $BORDER; padding: 0; }
QPushButton[role="winclose"]:hover { background-color: $WIN_CLOSE; }
QPushButton[role="winclose"]:pressed { background-color: $WIN_CLOSE; padding: 0; }

/* ---------- 输入 ---------- */
QLineEdit, QComboBox, QSpinBox {
    background-color: $FIELD;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 8px;
    padding: 6px 10px;
    min-height: 18px;
    selection-background-color: $ACCENT;
    selection-color: $ON_ACCENT;
}
QLineEdit:hover, QComboBox:hover { border-color: $HOVER; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: $ACCENT;
    background-color: $FIELD;
}
QLineEdit:disabled, QComboBox:disabled {
    background-color: $DIM_BG; color: $DIM_FG; border-color: $DIM_BD;
}
QComboBox { padding-right: 26px; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
    background-color: transparent;
}
/* 箭头不走 QSS：Qt 不支持 CSS 的 border 三角，写出来会塌成一个实心方块；
   给 image 塞 SVG 又依赖 qsvg 插件。改由 gui_qt.ComboBox.paintEvent 自绘。 */
QComboBox QAbstractItemView {
    background-color: $PANEL;
    color: $TEXT;
    border: 1px solid $HOVER;
    border-radius: 8px;
    padding: 4px;
    selection-background-color: $SEL_BG;
    selection-color: $TEXT;
    outline: none;
}

/* ---------- 复选 ---------- */
QCheckBox { background-color: transparent; spacing: 8px; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 1px solid $BORDER; border-radius: 4px;
    background-color: $FIELD;
}
QCheckBox::indicator:hover { border-color: $ACCENT; }
QCheckBox::indicator:checked {
    background-color: $ACCENT; border-color: $ACCENT;
}
QCheckBox::indicator:disabled {
    background-color: $DIM_BG; border-color: $DIM_BD;
}

/* ---------- tab ---------- */
QTabWidget::pane {
    border: none;
    border-top: 1px solid $BORDER;
    background-color: transparent;
}
QTabBar { background-color: transparent; }
QTabBar::tab {
    background: transparent;
    color: $MUTED;
    padding: 8px 18px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    min-width: 66px;
}
QTabBar::tab:hover { color: $TEXT; }
QTabBar::tab:selected {
    color: $ACCENT;
    border-bottom: 2px solid $ACCENT;
    font-weight: 600;
}

/* ---------- 表格 ---------- */
QTableWidget {
    background-color: $PANEL;
    alternate-background-color: $ROW_ALT;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 12px;
    gridline-color: $BORDER;
    selection-background-color: $SEL_BG;
    selection-color: $TEXT;
}
QTableWidget::item { padding: 6px 8px; border: none; }
QTableWidget::item:hover { background-color: $HOVER; }
QTableWidget::item:selected { background-color: $SEL_BG; color: $TEXT; }
QHeaderView { background-color: transparent; }
QHeaderView::section {
    background-color: $BG;
    color: $MUTED;
    border: none;
    border-bottom: 1px solid $BORDER;
    padding: 8px 10px;
    font-weight: 600;
    font-size: 9pt;
}
QHeaderView::section:first { border-top-left-radius: 11px; }
QHeaderView::section:last { border-top-right-radius: 11px; }
QTableCornerButton::section { background-color: $BG; border: none; }

/* ---------- 文本区 ---------- */
QTextEdit, QPlainTextEdit {
    background-color: $PANEL;
    color: $TEXT;
    border: 1px solid $BORDER;
    border-radius: 12px;
    padding: 8px;
    selection-background-color: $SEL_BG;
    selection-color: $TEXT;
}

/* ---------- 滚动 ---------- */
QScrollArea { background-color: transparent; border: none; }
QScrollArea > QWidget > QWidget { background-color: transparent; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical {
    background: $HOVER; border-radius: 5px; min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: $FAINT; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background-color: transparent;
}
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal {
    background: $HOVER; border-radius: 5px; min-width: 32px;
}
QScrollBar::handle:horizontal:hover { background: $FAINT; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background-color: transparent;
}

/* ---------- 状态栏 ---------- */
QStatusBar { background-color: $BG; border-top: 1px solid $BORDER; }
QStatusBar::item { border: none; }
QStatusBar QLabel { color: $MUTED; font-size: 9pt; }

QProgressBar {
    background-color: $FIELD;
    border: none;
    border-radius: 3px;
    max-height: 6px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk { background-color: $ACCENT; border-radius: 3px; }

QToolTip {
    background-color: $PANEL;
    color: $TEXT;
    border: 1px solid $HOVER;
    border-radius: 6px;
    padding: 5px 8px;
}
""")


class ThemeManager:
    """全局 QSS 管理器：一次 setStyleSheet，整棵树在同一帧换完。"""

    def __init__(self, app):
        self.app = app
        self.current = "dark"
        # 自绘控件（ComboBox 的箭头）取色用，必须跟着 apply() 一起换
        CURRENT.update(THEMES["dark"])

    def apply(self, name: str) -> str:
        if name not in THEMES:
            name = "dark"
        self.current = name
        CURRENT.update(THEMES[name])
        self.app.setStyleSheet(_QSS.safe_substitute(THEMES[name]))
        return name

    def tokens(self) -> dict:
        return THEMES[self.current]
