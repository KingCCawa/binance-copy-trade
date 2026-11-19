"""轻量化运行时中英翻译层。

设计取舍：
- 不改业务代码里的中文字符串（ gui.py 有 800+ 处，其中大量是 f-string 片段和
  动态日志，逐处改 t() 风险高且违背最小改动）。
- 改为遍历已构建的控件，把文本按词典映射成英文；首次翻译前把原文存进 Qt
  property，切回中文时从 property 恢复。
- 动态表格（成交/持仓/挂单/播报）在渲染后由事件泵重新应用翻译。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QLabel, QLineEdit, QCheckBox, QPushButton, QTabWidget,
    QTableWidget, QWidget,
)

# 当前语言：zh / en
_lang = "zh"

# 精确整句/标签映射（优先匹配）。键是界面上的中文原文。
TEXT: dict[str, str] = {
    # ---- 主导航 / 页签 ----
    "跟单盈亏": "Copy P&L",
    "操作播报": "Activity Feed",
    "成交事件": "Trade Events",
    "未成交挂单": "Open Orders",
    "事件日志": "Event Log",
    "运行配置": "Settings",
    "概览": "Overview",
    "交易员仓位": "Trader Positions",
    "仓位对账": "Position Recon",
    "币安跟单": "Binance Copy Trade",
    "语言": "Language",
    "中文": "Chinese",
    "English": "English",

    # ---- 指标 ----
    "净值": "Net Value",
    "浮动盈亏": "Unrealized P&L",
    "已实现盈亏": "Realized P&L",
    "累计盈亏": "Total P&L",
    "收益率": "Return Rate",
    "最大回撤": "Max Drawdown",
    "带单员余额": "Trader Balance",
    "带单员盈亏": "Trader P&L",
    "带单员收益率": "Trader Return",
    "当前持仓浮盈": "Open Positions (Unrealized)",
    "本程序权益": "My Equity",
    "模拟资金": "Sim Capital",

    # ---- 表头：成交事件 ----
    "时间": "Time",
    "开/平": "Act",
    "方向": "Side",
    "品种": "Symbol",
    "均价": "Avg Price",
    "数量": "Qty",
    "名义 USDT": "Notional USDT",
    "应开 USDT": "Expected USDT",
    "实开 USDT": "Actual USDT",

    # ---- 表头：持仓 ----
    "开仓价": "Entry Price",
    "标记价": "Mark Price",
    "爆仓价": "Liq Price",
    "距爆仓": "To Liq",
    "爆仓亏损": "Liq Loss",
    "浮盈": "Unreal. P&L",

    # ---- 表头：历史开单 / 交易员仓位 ----
    "动作": "Action",
    "价格": "Price",
    "盈亏": "P&L",
    "开仓均价": "Avg Entry",
    "持仓名义": "Position Notional",
    "占余额": "% of Balance",

    # ---- 表头：挂单 ----
    "杠杆": "Leverage",
    "挂单时长": "Age",
    "价格模式": "Price Mode",
    "开仓": "Open",
    "平仓": "Close",
    "撤单": "Cancel",
    "多": "Long",
    "空": "Short",

    # ---- 按钮 ----
    "平选中": "Close Selected",
    "一键全平": "Close All",
    "部分平仓…": "Partial Close…",
    "市价追单": "Market Chase",
    "撤销": "Cancel Order",
    "重算": "Recalculate",
    "清空": "Reset",
    "按实盘权益": "Use Live Equity",
    "保存并连接": "Save & Connect",
    "开始轮询": "Start Polling",
    "停止轮询": "Stop Polling",
    "开始跟单": "Start Following",
    "停止跟单": "Stop Following",
    "单次运行": "Run Once",
    "刷新": "Refresh",

    # ---- 设置卡片标题 / 复选 ----
    "运行方式": "Runtime",
    "执行模式": "Execution Mode",
    "带单员": "Trader",
    "额度设置": "Capital",
    "交易参数": "Trading",
    "外观": "Appearance",
    "开机自启": "Auto-start on login",
    "启动后自动开始轮询": "Auto start polling",
    "轮询期间阻止系统睡眠（允许息屏）": "Prevent sleep while polling (screen may turn off)",
    "每轮对账补差（漏单自愈）": "Reconcile each cycle (self-heal missed orders)",
    "绿涨红跌": "Green Up / Red Down",
    "红涨绿跌": "Red Up / Green Down",
    "深色模式": "Dark Mode",
    "浅色模式": "Light Mode",

    # ---- 周期下拉 ----
    "最近 7 天": "Last 7 Days",
    "最近 30 天": "Last 30 Days",
    "最近 90 天": "Last 90 Days",
    "最近 1 年": "Last Year",
    "全部": "All",
    "近 7 天": "7D",
    "近 30 天": "30D",
    "近 90 天": "90D",
    "近 1 年": "1Y",

    # ---- 状态 ----
    "未连接": "Not Connected",
    "未运行 · dry-run 本地模拟": "Idle · dry-run (local sim)",
    "已连接": "Connected",
    "连接中": "Connecting",
    "等待数据 · 点「单次运行」或开始轮询": "Waiting for data · Run Once or Start Polling",
    "等待数据 · 需要一次成功取数（带单员成交与余额）": "Waiting for data · need one successful fetch (trader trades + balance)",
    "等待数据 · 请先连接并检查账户": "Waiting for data · connect and verify account first",
    "已重算 · 净值与回撤从零重新记录": "Recalculated · net value and drawdown restarted from zero",
    "已清空 · 从零重新记录": "Cleared · recording from zero",

    # ---- 提示 / help ----
    "按成交历史重放得出（该交易员未公开实时持仓接口）。":
        "Derived from trade history (this trader does not expose a live positions API).",
    "平仓只影响本程序这一笔，不会影响带单员":
        "Closing affects only your own position, not the trader's",
    "改为按当前市价把同样数量追进去":
        "Chase: buy/sell the same quantity at the current market price",
    "先在挂单表里选一行再追单": "Select an order in the table first",
    "先在挂单表里选一行再撤": "Select an order in the table first",
    "先在持仓表里选一个品种再平": "Select a symbol in the positions table first",
    "当前没有持仓": "No open positions",
    "模拟盘 · 不入实盘账": "Simulated · not recorded to live account",
    "测试网": "Testnet",
    "实盘": "Live",

    # ---- 日志常见短语（作为整句的一部分，运行时按词替换兜底）----
    "取数失败": "fetch failed",
    "轮询已启动": "polling started",
    "轮询已停止": "polling stopped",
    "执行模式切换": "execution mode switched",
    "本地模拟（不下单）": "local simulation (no real orders)",
    "市价追单": "market chase",
    "模拟撮合成交": "simulated fill",
    "跟进结果": "follow result",
    "已跟账本": "followed ledger",
    "对账补差": "reconcile gap",
    "挂单对账": "order reconciliation",
    "开仓跟随": "follow open",
    "平仓跟随": "follow close",
    "跳过超龄开仓信号": "skipped stale open signal",
    "无法切到逐仓": "cannot switch to isolated margin",
    "逐仓预算不足": "isolated margin budget insufficient",
    "跟单目标": "follow target",
    "模拟本金改为": "sim capital changed to",
    "已切换": "switched to",
    "模式": "mode",

    # ---- 设置页：全部 UI 可见文案（整句精确翻译，避免词级切碎）----
    "参数": "Parameters",
    "下单杠杆": "Order Leverage",
    "挂单价": "Order Price",
    "止盈价": "Take Profit",
    "止损价": "Stop Loss",
    "抓数频率": "Fetch Interval",
    "声音提示": "Sound Alert",
    "事件日志": "Event Log",
    "本地模拟": "Local Simulation",
    "涨跌配色": "Price Colors",
    "界面语言": "Interface Language",
    "入场挂单 ": "Entry order ",
    "试听": "Test Sound",
    "显示": "Show",
    "保存": "Save",
    "保存配置": "Save Config",
    "强制刷新": "Force Refresh",
    "清空日志": "Clear Log",
    "清空播报": "Clear Feed",
    "添加 API Key": "Add API Key",
    "连接中… 正在校准时钟并校验权限": "Connecting… syncing clock and verifying permissions",
    "未连接 — 填入 Key 后点「保存并连接」。": "Not Connected — enter Key then click Save & Connect",
    "请先在「运行配置 → 添加 API Key」绑定 API Key / Secret 并点「保存并连接」":
        "First bind API Key / Secret in Settings → Add API Key, then click Save & Connect",
    "请先在「运行配置」填写带单员 ID（带单本链接里的纯数字）":
        "First enter the trader ID in Settings (the numeric ID from the copy-trade link)",
    "请先完成初始配置（API Key 绑定 + 带单员 ID），配置好后即可点「开始轮询」":
        "Complete initial setup (API Key + trader ID) first, then click Start Polling",
    "配置就绪 · 点「开始轮询」即可开始跟单":
        "Config ready · click Start Polling to begin copying",
    "未就绪 · ": "Not ready · ",
    "配置未就绪：切到「运行配置」页，状态栏和日志都给出指引。":
        "Config not ready: go to Settings; the status bar and log show guidance.",
    "配置刚变就绪时提示一次，告诉用户现在可以点「开始轮询」。":
        "Prompt once when config becomes ready, telling the user they can now click Start Polling.",
    "未填 API Key / Secret": "API Key / Secret not filled",
    "未填 API Key / Secret。扫码登录和 Cookie 都不能下单。":
        "API Key / Secret not filled. QR login and cookies cannot place orders.",
    "币安网页 → 头像 → 账户 → API 管理 → 创建 API Key。权限勾「允许读取」+「允许合约」，IP 白名单填本机公网 IP。凭证只写入本机 .env。":
        "Binance web → avatar → Account → API Management → Create API Key. Enable Read + Futures permissions, set IP whitelist to your public IP. Credentials are written only to the local .env.",
    "连接失败：": "Connection failed: ",
    "连接失败: ": "Connection failed: ",
    "账户查询失败": "Account query failed",
    "权限 enableFutures=": "Permission enableFutures=",
    "交易所拒绝了这把 Key（-2015）。检查是否已启用合约权限、IP 白名单是否包含当前出口 IP。":
        "The exchange rejected this Key (-2015). Check Futures permission is enabled and your public IP is in the whitelist.",
    "测试网拒绝了这把 Key（-2015）。它是在币安主站创建的，测试网要单独建 Key；或把执行模式切回 dry-run / live。网络是通的，开 TUN 不影响这个结果。":
        "Testnet rejected this Key (-2015). It was created on the main site; testnet needs its own Key, or switch execution mode back to dry-run / live. Network is fine; TUN does not affect this.",
    "测试网 Key 与实盘不通用，实盘 Key 会报 -2015。":
        "Testnet and live Keys are not interchangeable; a live Key returns -2015.",
    "凭证已原子写入 .env": "Credentials written atomically to .env",
    "接口未返回昵称": "API did not return a nickname",
    "带单员昵称获取失败：": "Failed to fetch trader nickname: ",
    "未确认时补拉昵称，节流 30 秒一次。": "Retry nickname pull when unconfirmed, throttled to once per 30s.",
    "启动或换人后主动拉一次 detail，立刻显示名字，不等 30 秒轮询。":
        "Pull detail once at startup or after switching traders to show the name immediately, without waiting for the 30s poll.",
    "换人即作废：标题必须先回到「未连接」，等新带单员的接口返回再显示，否则旧名字会挂在新人头上。":
        "Invalidated on trader switch: the title must reset to Not Connected until the new trader's API responds, otherwise the old name sticks to the new one.",
    "只显示**本次会话确认过**的带单员。":
        "Only show traders confirmed in this session.",
    "配置里的 `lead_nickname` 是上次运行留下的缓存，可能是别人的、也可能早被改名；拿它填标题等于把旧数据当事实。确认的唯一来源是一次成功的接口返回。":
        "lead_nickname in config is a cache from the last run; it may belong to someone else or be outdated. The only valid source is a successful API response.",
    "未确认时写「未连接」；确认后写成「昵称 · portfolioId 尾 6 位」。":
        "Show Not Connected until confirmed; after confirmation show Nickname · last 6 digits of portfolioId.",
    "跟单目标 portfolioId=": "Follow target portfolioId=",
    "币安带单本链接里的纯数字 portfolioId。": "The numeric portfolioId from the Binance copy-trade link.",
    "portfolioId 应是带单本链接里的纯数字 ID": "portfolioId should be the numeric ID from the copy-trade link",
    "coin": "coin",

    # ---- 设置页说明（help / caption）----
    "开机自启": "Auto-start on login",
    "开机自启：登录后自动打开并开始轮询。": "Auto-start: launch and begin polling after login.",
    "启动后自动开始轮询": "Start polling on launch",
    "轮询期间阻止系统睡眠（允许息屏）": "Prevent sleep while polling (screen may turn off)",
    "每轮对账补差（漏单自愈）": "Reconcile every cycle (self-heal missed orders)",
    "运行方式": "Runtime",
    "轮询前置门禁：带单员 ID 已填、API Key 已绑定才允许开始。":
        "Pre-flight gate: polling requires a trader ID and a bound API Key.",
    "首次打开程序时 AE 的 portfolioId 还是示例值、也没有 Key，直接轮询只会一轮轮取数失败 / 空跑。":
        "On first launch the portfolioId is a placeholder and no Key exists; polling would just fail or idle each cycle.",
    "这里给出明确原因，让界面把用户带到「运行配置」页先把两件事做好。":
        "A clear reason is shown and the UI guides the user to Settings to set both first.",
    "用 cfg 里的值判断而不是输入框：main() 在控件建好后调用，配置已经 load 完毕。":
        "Judge by values in cfg, not input boxes: main() runs after widgets are built and config is loaded.",
    "同一时间只允许一个实例。": "Only one instance is allowed at a time.",
    "开机自启 + 手动双击是很容易叠在一起的两条路：两个实例各自轮询、各自下单，等于仓位翻倍，而且彼此不知道对方挂过什么。":
        "Auto-start and manual double-click can stack: two instances poll and place orders independently, doubling positions without knowing about each other.",
    "用文件锁，进程退出自动释放，不会留死锁。": "A file lock is released automatically on exit; no stale locks.",
    "已有一个实例在运行（gui.lock 被占用），本次退出。两个实例会各自下一遍单，仓位会翻倍。":
        "Another instance is already running (gui.lock held); exiting. Two instances would each place orders, doubling positions.",

    # ---- 跟单盈亏 / 概览 ----
    "暂无曲线 · 开始轮询后按轮记录净值": "No curve yet · net value is recorded each cycle after polling starts",
    "还没有快照。开始轮询后按「成交 / 权益变化 / 15 分钟锚点」记录；dry-run 与实盘分开存，切模式不会串数据。":
        "No snapshots yet. After polling starts, records are written on trades / equity changes / 15-minute anchors; dry-run and live are stored separately and never mix.",
    "只筛显示区间，不删数据。区间内快照不足时会在下方注明「程序只记录了 X 天」，不会拿更早的点冒充。":
        "Filters the display range only, never deletes data. If the range has too few snapshots, a note shows how many days were recorded; older points are never substituted.",
    " · 该区间内没有成交或权益变化，曲线不足两点": " · no trades or equity changes in this range; fewer than two curve points",
    "再积累一条快照就出曲线": "one more snapshot will draw the curve",
    "暂无成交事件 · 点击「单次运行」取数": "No trade events · click Run Once to fetch",
    "当前无未成交挂单": "No open orders",
    "当前没有持仓": "No open positions",
    "暂无成交": "No trades yet",
    "总敞口 = 仓位名义 ÷ 余额，用来对比你和带单员的杠杆水平。":
        "Total exposure = position notional ÷ balance, for comparing your leverage with the trader's.",
    "按成交历史重放得出（该交易员未公开实时持仓接口）。":
        "Derived from trade history (this trader does not expose a live positions API).",
    "交易员当前仓位：由成交历史重放得出（他公开接口不显示仓位）。":
        "Current trader positions: derived from trade history (their public API does not show positions).",
    "平仓只影响本程序这一笔，不会影响带单员": "Closing affects only your own position, not the trader's",
    "已重算 · 净值与回撤从零重新记录": "Recalculated · net value and drawdown restarted from zero",
    "已清空 · 从零重新记录": "Cleared · recording from zero",

    # ---- 播报 ----
    "操作播报": "Activity Feed",
    "带单员操作播报": "Trader Activity Feed",
    "本程序动作：": "My action: ",
    "本程序动作：挂限价单 ": "My action: placed limit order ",
    "本程序动作：按操作处理": "My action: processed per signal",
    "本程序动作：仅撤单（无仓位可平）": "My action: cancel only (no position to close)",
    "本程序动作：历史未平仓位挂更优价限价单": "My action: placed better-price limit order for historical open position",
    "带单员：": "Trader: ",
    "带单员平仓": "trader closed",
    "带单员平仓即撤单": "cancel order when trader closes",
    "变动金额：": "Change amount: ",
    "成交均价：": "Avg fill price: ",
    "成交数量：": "Fill qty: ",
    "成交金额：": "Fill amount: ",
    "保证金余额：": "Margin balance: ",
    "未实现盈亏：": "Unrealized P&L: ",
    "已实现盈亏：": "Realized P&L: ",
    "口径：": "Basis: ",
    "空仓精确": "exact (flat)",
    "持仓推算（含浮亏估算）": "estimated (with unrealized)",
    "影响：后续开仓的敞口比例按新余额计算": "Impact: future position sizing uses the new balance",

    # ---- 状态栏 / 日志常见整句 ----
    "未运行 · dry-run 本地模拟": "Idle · dry-run (local sim)",
    "取数 / 执行中": "Fetching / executing",
    "跟随中 · ": "Following · ",
    "已停止跟单，仅监控不下单": "Following stopped · monitoring only, no orders",
    "轮询已启动，间隔 ": "Polling started, interval ",
    "轮询已停止": "Polling stopped",
    "执行模式切换：本地模拟（不下单）": "Execution mode: local simulation (no real orders)",
    "执行模式切换：": "Execution mode switched: ",
    "已切换到": "Switched to",
    "跟单目标已切换 ": "Follow target switched ",
    "运行配置已保存": "Settings saved",
    "取数交给后台线程": "Fetch dispatched to background thread",
    "账户权益已同步：": "Account equity synced: ",
    "已清除余额缓存": "Balance cache cleared",
    "已建立基线：": "Baseline established: ",
    "已按当前快照重建基线，从现在开始重新跟单": "Baseline rebuilt from current snapshot; copying from now on",
    "基线已建立 · 目标 ": "Baseline established · target ",
    "换人后清空去重水位并重新建立基线，不会补开新目标的历史单；我账户里已有的挂单继续由引擎管理。":
        "On trader switch, dedup state and baseline are reset; no historical orders of the new target are opened; existing orders in my account stay managed by the engine.",
    "跟单目标 %s -> %s：重置起点，历史仓位不跟": "Follow target %s -> %s: reset origin, historical positions not followed",
    "轮询期间保持系统唤醒": "Keep system awake during polling",
    "当前系统正忙": "System is busy",
    "取数失败": "fetch failed",
    "取数失败: ": "fetch failed: ",
    "带单本接口限流": "copy-trade API rate limited",
    "接口忙时程序会自动退避重连": "the client auto-backs-off and reconnects when the API is busy",
    "带单本接口；detail 60s 缓存，Session 长期复用。": "Copy-trade API; detail cached 60s, session reused.",
    "爆仓保护 · 止损线": "Liquidation Guard · Stop Line",
    "只有在更优价挂单": "Only place orders at better prices",
    "仅当价格优于带单员成交价时挂单。": "Only place orders when the price is better than the trader's fill price.",
    "本地模拟，不下单。": "Local simulation, no real orders.",
    "填写 API Key 后可切换测试网 / 实盘。": "After entering an API Key you can switch to testnet / live.",
    "本地模拟，不下单。 填写 API Key 后可切换测试网 / 实盘。":
        "Local simulation, no real orders. After entering an API Key you can switch to testnet / live.",
    "只影响保证金占用，不改变仓位大小 —— 仓位永远按「他的数量 × 权益比」算。品种最大杠杆低于设定值时会被拒单（-4028），调小即可。":
        "Affects only margin usage, not position size — size is always his qty × equity ratio. If a symbol's max leverage is lower than set, the order is rejected (-4028); lower it.",
    "每 N 秒拉一次全量成交历史。币安对 trade-history 有 11012005 限流，太快反而拿不到数据；接口忙时程序会自动退避重连，事件靠全量历史补回。":
        "Pulls full trade history every N seconds. Binance rate-limits trade-history (error 11012005); too fast means no data. The client auto-backs-off on busy API and recovers events from full history.",
    "日志保留 条数，改动在下次启动生效。": "Log retention (rows); changes take effect on next launch.",
    "日志保留 rows数，改动在下次 生效。": "Log retention (rows); changes take effect on next launch.",
    "最小 5 秒 · 保存后立即生效": "Min 5 s · effective immediately after save",
    "收到后需要下一次轮询才显示": "takes effect on the next poll",
    # 截图复查：这些此前漏掉，导致界面仍有中文
    "只在更优价挂单": "Only place orders at better prices",
    "更优幅度 %": "Improve %",
    "关键字": "Keyword",
    "级别": "Level",
    "0 = 关闭": "0 = off",
    "当前：": "Current: ",
    "未开始轮询：": "Polling not started: ",
    "状态已恢复": "State restored",
    "首次运行": "first run",
    "累计亏损（回撤）达到份额的这个比例，就一键平掉本程序所有仓位，不影响带单员。":
        "When cumulative loss (drawdown) reaches this share percentage, close all my positions with one click; the trader is unaffected.",
    "累计亏损（回撤）达到本程序份额的这个百分比，就一键平掉本程序全部仓位；0 = 关闭。":
        "When cumulative loss (drawdown) reaches this percentage of my share, close all my positions; 0 = off.",
    "就一键平掉本程序全部仓位；0 = 关闭。": "close all my positions; 0 = off.",
}

# 词级兜底：长句里逐词替换，处理日志/播报等动态文案。
# 独立词表：这些词多出现在动态拼接的日志里，不适合做整句精确匹配。
EXTRA_WORDS: dict[str, str] = {
    "就绪": "Ready", "已恢复上次运行状态": "resumed previous state",
    "首轮建立基线": "baseline on first cycle", "历史仓位不跟": "historical positions not followed",
    "间隔": "interval", "秒": "s", "张": " contracts", "笔": " orders",
    "次": " times", "条": " rows", "笔": " orders", "更优": "Improve",
    "容差": "Tolerance",
    "失败": "failed", "成功": "success", "异常": "error", "超时": "timeout",
    "重试": "retry", "跳过": "skipped", "已暂停": "paused", "已恢复": "resumed",
    "已连接": "connected", "已断开": "disconnected", "已保存": "saved",
    "已清除": "cleared", "已撤销": "cancelled", "已成交": "filled",
    "挂单": "order", "订单": "order", "仓位": "position", "持仓": "position",
    "成交": "trade", "信号": "signal", "事件": "event", "动作": "action",
    "余额": "balance", "权益": "equity", "本金": "principal", "额度": "budget",
    "收益": "profit", "亏损": "loss", "回撤": "drawdown", "爆仓": "liquidation",
    "标记价": "mark price", "限价": "limit price", "现价": "market price",
    # 单字词（多/空/开/平/个）不进词表：会误伤「很多」「开始」等正常词。
    # 它们只作为整句 TEXT 的一部分被精确匹配。
    "原因": "reason", "金额": "amount", "时间": "time", "品种": "symbol",
    "方向": "side", "价格": "price", "数量": "qty", "杠杆": "leverage",
    "逐仓": "isolated", "全仓": "cross", "保证金": "margin", "预算": "budget",
    "不足": "insufficient", "未确认": "unconfirmed", "未返回": "not returned",
    "获取": "fetch", "更新": "update", "读取": "read", "写入": "write",
    "账户": "account", "连接": "connection", "网络": "network", "代理": "proxy",
    "直连": "direct", "通道": "channel", "切换": "switch", "模式": "mode",
    "模拟": "sim", "实盘": "live", "测试网": "testnet",
    "开始": "start", "停止": "stop", "暂停": "pause", "完成": "done",
    "配置": "config", "参数": "param", "目标": "target", "来源": "source",
    "警告": "warn", "错误": "error", "信息": "info", "提示": "hint",
}

_SAFE_SINGLE = {"秒", "张", "笔", "次", "条"}  # 不会组成其它词的量词单字
WORDS: list[tuple[str, str]] = sorted(
    [(k, v) for k, v in {**EXTRA_WORDS, **TEXT}.items()
     if len(k) >= 2 or k in _SAFE_SINGLE],
    key=lambda kv: -len(kv[0]),
)

# 动态日志的固定标签（含 = 或 · 的完整短语）。按长度降序，先长后短。
# 这些是完整短语，替换后不会留下半截中文。
LOG_PHRASES: list[tuple[str, str]] = sorted([
    ("跟进结果", "Follow result"),
    ("仅撤单=", "cancel-only="),
    ("跳过=", "skipped="),
    ("开=", "open="), ("平=", "close="),
    ("开仓跟随：挂单", "Follow open: placed order"),
    ("平仓跟随 ", "Follow close "),
    ("对账补差 ", "Reconcile gap "),
    ("：目标 ", " · target "),
    ("，已跟 ", ", followed "),
    ("，补 ", ", adding "),
    ("张，平掉多少：", " contracts, closing: "),
    ("张 @ ", " contracts @ "),
    (" 张", " contracts"),
    (" · 更优 ", " · Improve "),
    (" · 目标 ", " · target "),
    ("（撤挂单=", " (cancel orders="),
    ("本程序无仓位，仅撤单", "no position in my account, cancel only"),
    ("带单员名义 ", "trader notional "),
    (" 空 ", " Short "), (" 多 ", " Long "),
    ("开仓跟随", "follow open"), ("平仓跟随", "follow close"),
    ("状态已恢复：", "State restored: "),
    ("首次运行", "first run"),
    (" 条 · 当前显示 ", " rows · showing "),
    (" 条 · 上限 ", " rows · limit "),
    (" 条成交事件", " trade events"),
    ("共 ", "Total "),
], key=lambda kv: -len(kv[0]))


def set_language(lang: str) -> None:
    global _lang
    _lang = "en" if str(lang).lower() in ("en", "english") else "zh"


def get_language() -> str:
    return _lang


_WORD_RE = None


def _word_regex():
    global _WORD_RE
    if _WORD_RE is None:
        import re
        alt = "|".join(re.escape(k) for k, _ in WORDS)
        _WORD_RE = re.compile(alt) if alt else None
    return _WORD_RE


def translate(text: str) -> str:
    """把中文文本译为英文；非中文或已英文则原样返回。"""
    if _lang != "en" or not text:
        return text
    if text in TEXT:
        return TEXT[text]
    rx = _word_regex()
    if rx is None:
        return text
    # 动态日志：先按完整短语替换（安全，不会切碎词）。
    out = text
    for zh, en in LOG_PHRASES:
        if zh in out:
            out = out.replace(zh, en)
    # 词级替换只在「整句被完全译完」时才接受；否则保留原文，避免
    # 「抓频率 / liquidatio保护」这类半中半英乱码。
    if any("\u4e00" <= c <= "\u9fff" for c in out):
        rx = _word_regex()
        if rx is not None:
            lookup = dict(WORDS)
            cand = rx.sub(lambda m: lookup.get(m.group(0), m.group(0)), out)
            if not any("\u4e00" <= c <= "\u9fff" for c in cand):
                out = cand
        if any("\u4e00" <= c <= "\u9fff" for c in out):
            return text
    for a, b in (("，", ", "), ("：", ": "), ("；", "; "),
                 ("（", " ("), ("）", ")"), ("。", "."), ("、", ", ")):
        out = out.replace(a, b)
    return out


_SRC = "_i18n_src"   # Qt property：翻译前的原文


def _restore_or_translate(widget, key: str, getter, setter) -> None:
    # QTableWidgetItem 不是 QWidget，没有 property()；用 Qt.UserRole 存原文。
    if isinstance(widget, type(None)):
        return
    is_item = hasattr(widget, "data") and hasattr(widget, "setData")
    if is_item:
        src = widget.data(Qt.UserRole)
        if src is None:
            src = getter()
            widget.setData(Qt.UserRole, src)
    else:
        src = widget.property(_SRC)
        if src is None:
            src = getter()
            widget.setProperty(_SRC, src)
    setter(translate(src) if _lang == "en" else src)


def apply_widget(root: QWidget) -> None:
    """递归遍历控件树，应用当前语言的文本。"""
    if root is None or not _lang:
        return
    for w in [root] + list(root.findChildren(QWidget)):
        if isinstance(w, QTabWidget):
            for i in range(w.count()):
                src = w.property(_SRC + "_tab")
                if not isinstance(src, list):
                    src = [w.tabText(i) for i in range(w.count())]
                    w.setProperty(_SRC + "_tab", src)
                if i < len(src):
                    w.setTabText(i, translate(src[i]) if _lang == "en" else src[i])
        if isinstance(w, QComboBox):
            src = w.property(_SRC + "_items")
            if not isinstance(src, list):
                src = [w.itemText(i) for i in range(w.count())]
                w.setProperty(_SRC + "_items", src)
            for i in range(min(len(src), w.count())):
                w.setItemText(i, translate(src[i]) if _lang == "en" else src[i])
        if isinstance(w, QTableWidget):
            for c in range(w.columnCount()):
                item = w.horizontalHeaderItem(c)
                if item is not None:
                    _restore_or_translate(
                        item, "text", lambda it=item: it.text(),
                        lambda v, it=item: it.setText(v))
            for r in range(w.rowCount()):
                for c in range(w.columnCount()):
                    item = w.item(r, c)
                    if item is not None:
                        _restore_or_translate(
                            item, "text", lambda it=item: it.text(),
                            lambda v, it=item: it.setText(v))
        if isinstance(w, QLabel):
            _restore_or_translate(
                w, "text", w.text, w.setText)
        elif isinstance(w, (QPushButton, QCheckBox)):
            _restore_or_translate(
                w, "text", w.text, w.setText)
        elif isinstance(w, QLineEdit):
            _restore_or_translate(
                w, "placeholder", w.placeholderText, w.setPlaceholderText)
        # QTextEdit（播报/日志）是富文本，逐条重画由业务层负责，这里跳过以免
        # 破坏字符格式。


_last_apply = 0.0


def apply_when_dirty(root: QWidget, min_interval: float = 0.25) -> None:
    """事件泵里调用：英文状态下节流重贴动态表格的译文。"""
    if _lang != "en":
        return
    global _last_apply
    now = time.monotonic()
    if now - _last_apply < min_interval:
        return
    _last_apply = now
    apply_widget(root)
