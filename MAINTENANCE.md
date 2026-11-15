# 币安跟单 · 维护文档

| 项 | 值 |
|---|---|
| 版本 | `v0.1.0` |
| 最后更新 | 2026-11-16 05:10 (GMT+8) |
| 状态 | 功能完整；界面层为 PySide6（`gui.py`）；`gui-config.json` 为**个人配置**（已 gitignore），首次使用需自己填带单员 ID 与凭证 |
| 回归基线 | `run_all.py` **10/10 PASS**；`copy_engine.py --selftest` **30/30**；`position_ledger.py` 8/8；`pnl_tracker.py` 5/5；`state_store.py` 1/1；`gui.py --selftest` **48 条断言**；`_refs/_replay_check.py` 7/7 |
| 变更历史 | `CHANGELOG.md`（含待决策清单与交接卡） |

面向后续改造和接手的 AI。本文只描述现行结构与现行行为；
「怎么走到现在、还有哪些没定」见 `CHANGELOG.md`。
换 agent 接手先读 `CHANGELOG.md` 的「交接卡」与「待决策清单」，再回到本文动手。

## 1. 这个程序做什么

币安合约「带单跟单」：带单员名额满了进不去官方跟单池，但币安带单本页面仍公开
「最新操作记录」。程序直连该页面背后的 `bapi` 接口拉逐笔成交，按秒聚合成开/平事件，
再在自己账户用**更优价（或容差内）限价单**等比复刻名义敞口；带单员平仓时撤掉我方
该品种全部挂单、有仓位才平仓。

运行时**不需要浏览器、不需要登录、不需要 Cookie**。`probe_apis.py` 里的 Playwright
只用于开发期从前端 JS 提取接口路径，跑 `--full` 回归时才会执行，缺它标 SKIP。

启动：`start-gui.bat`（内部 `py -3.12 gui.py`）。本机默认 `python` 是 3.13，
没装 PySide6；系统 Python 3.12 才有 PySide6 / requests / playwright。

## 2. 目录与职责

| 文件 | 职责 | 改它的时候 |
|---|---|---|
| `gui.py` | **界面层**：`QMainWindow` + `QTabWidget` 八个 tab、线程编排、配置读写、账户连接、取数/引擎调用。`DEFAULTS` 与文案也在文件头 | 加 tab、改参数项、改交互 |
| `theme_qss.py` | 主题：整份 QSS 模板 + `ThemeManager`，`setStyleSheet()` 一次换完整棵树（圆角直接写在 QSS 里，不再有 `R_*` 常量） | 改配色、改控件外观 |
| `net.py` | 所有币安 HTTP 请求的双通道 + 退避重试 | 改超时/代理/重试策略 |
| `api_feed.py` | 带单本公开接口（`detail` / `trade-history` / 聚合 / `mark_prices`） | 换数据源、改聚合规则 |
| `feed.py` | `Fill` / `Rule`、中文流水双格式解析、入场价单一真源 | 改仓位口径、改解析文案 |
| `copy_engine.py` | 事件流引擎、去重水位、挂单对账、幽灵单红线 | 改跟单行为，最核心 |
| `position_ledger.py` | 全量事件 FIFO 重放：存活批次、每笔 CLOSE 的平前持仓、每笔 OPEN 的当时累计量 | 改加仓/分次平/历史仓位口径 |
| `pnl_tracker.py` | 盈亏口径（纯函数）：持仓→浮盈、引擎动作→流水、净值曲线统计 | 改浮盈/已实现口径、改曲线算法 |
| `better_executor.py` | 入场限价单、价格模式文本 | 改下单参数 |
| `state_store.py` | SQLite 状态库（WAL，单连接 + 锁） | 改持久化字段 |
| `account.py` | 只读账户自检、时钟校准、Key 权限检查 | 改自检项 |
| `vendor/chatdsn/` | fork 自 `lampts/chatdsn-python` 的交易所客户端 | 改签名/下单端点 |
| `run_all.py` | 一键回归，`--full` 含慢项，`--json` 机器可读 | 加/改检查项 |
| `main.py` | CLI dry-run 主链路 | 改 CLI 演示 |
| `wallet_probe.py` | 只读诊断：把 `marginBalance` 拆成「钱包余额 + 未实现盈亏」 | 确认分母口径、查缺标记价 |
| `_refs/_replay_check.py` | 真实流水回放 | 改回放口径 |
| `_refs/_test_frame.py` | 无边框窗口的命中测试探针 + 深浅两套整窗抓图 | 改标题栏高度 / 命中区域 |
| `_refs/_test_drag.py` | 注入鼠标实测拖动与四边缩放（结束还原窗口与光标） | 改 `RESIZE_MARGIN` / 缩放逻辑 |
| `_refs/` 其余 | 参考开源项目 | 只读 |

`_refs/` 下的开源项目只是参考，主链路不 import 它们。
该目录被 `.gitignore` 排除：克隆下来是空的，`run_all.py --full` 里的
`_refs/_replay_check.py` 会因此标 SKIP —— 这是刻意行为，不是坏了。


### 界面与主题

界面只有一套：`gui.py`（PySide6）。业务层 `CopyEngine` / `api_feed` /
`account` / `state_store` / `better_executor` 与界面完全解耦，改业务不用碰界面。

**主题为什么是 QSS**：早期 tkinter 版换主题要按「重建 ttk 样式 → 重涂直上色
控件 → 重画自绘 Canvas」三步走，三步之间隔着 idle 周期，窗口从最小化恢复时
布局尺寸还没算完，自绘卡片会因尺寸无效提前返回，于是出现「卡片还是旧色、
按钮已经新色」的撕裂。Qt 这边 `setStyleSheet()` 是整体替换，Qt 在同一个
绘制周期里重建整棵控件树，结构上不存在中间态。QSS 管不到的地方（表格 item
前景色、`QTextEdit` 里的字符格式、下拉框箭头）在同一个函数里紧接着重涂，
中间不让出事件循环，所以也不会露帧。

**窗口是无边框的，标题栏自绘**：系统标题栏的颜色归 DWM 管，Win10 上无法让
它跟随 `setStyleSheet()`（第 8 节有实测结论）。所以 `App` 用
`Qt.FramelessWindowHint`，自己画一条 46px 的 `QWidget#titlebar`（`TITLEBAR_H`），
拖动 / 双击最大化 / 四边缩放由 `nativeEvent()` 回答 `WM_NCHITTEST` 交回系统，
投影由 `apply_frame_shadow()` 补。加内容不要加在标题栏里，标题栏只放
标题、带单员名、主题按钮、三个窗口按钮。

## 3. 数据流

```
币安带单本 bapi（未登录）
  └─ api_feed.LeadApi.orders()                  「最新操作记录」按订单聚合
     （不能用 trade-history：一次只回 10 条逐笔，大单会被截断）
       └─ LeadApi.aggregate()                  按 (秒, 品种, 方向, 开平) 聚合
            └─ feed.Fill                        事件（OPEN / CLOSE）
                 └─ copy_engine.CopyEngine.process()
                      ├─ position_ledger.replay(fills)  全量 FIFO 重放
                      │    ├─ surviving      仍存活的开仓批次（历史未平仓位）
                      │    ├─ close_before   每笔 CLOSE 发生前的持仓量 → 平仓比例
                      │    └─ open_context   每笔 OPEN 当时的累计量 → 加仓基准
                      ├─ position_ledger.wallet_state()  敞口比例的分母
                      │    └─ marginBalance − 未实现盈亏 = 钱包余额
                      │         未实现盈亏由 surviving × fapi 标记价算出
                      ├─ drain()     Fill.key 去重 + follow_start_wall / 超龄过滤
                      ├─ rebuild_from_exchange()   每轮认回孤儿挂单
                      ├─ reconcile() 已成交销账 / 超时撤单（历史单豁免）
                      ├─ 基线轮  → 历史未平批次按原始开仓价挂更优价限价单
                      ├─ OPEN  → _open()  按累计名义补差额挂单 + 登记 pending
                      └─ CLOSE → _close() 无条件撤单 + 有仓才平
                           └─ vendor/chatdsn/binance.py  →  /fapi/v1/order
```

`api_feed.mark_prices()` 每轮拉一次 `fapi/v1/premiumIndex`（全量 949 个合约，
约 100KB，5 秒缓存）。拿不到标记价的品种按未实现盈亏 0 计，方向偏保守：
`unreal` 少算 → `wallet` 偏高 → 分母偏大 → 我方仓位偏小。

GUI 线程模型：Qt 主线程只做渲染；`fetch_events()`、`engine.process()`、
`AccountProbe.check()`、`_refresh_lead_name()` 都在后台线程，结果经
`queue.Queue` + `QTimer(300ms)` 的 `_drain_events()` 回主线程。`_drain_events()` 是 UI 事件泵，
不抓数据；抓数间隔由 `poll_interval_s` 控制，等待用独立的 `_poll_wake`。
`process()` 返回的 `signals`（本轮新事件）经 `_render_snapshot()` 交给
`_announce()` 渲染播报卡并在 live/testnet 异步发声；基线轮 `signals=[]` 不播报。

## 4. 配置项

### `gui-config.json`（界面「运行配置」tab 可改，也可用文本编辑器改）

| 键 | 默认 | 含义 |
|---|---|---|
| `portfolio_id` | `1234567890123456789` | 带单本链接里的纯数字 ID |
| `lead_nickname` | 空 | 昵称缓存：启动/换人时由 `_refresh_lead_name()` 主动拉 `detail` 回填，无需手填 |
| `lead_nickname_pid` | 空 | 昵称缓存所属的 `portfolio_id`；不匹配时显示「读取中…」，防换人串名 |
| `sound_enabled` | `true` | 播报发声开关；`live`/`testnet` 才响，`dry-run` 静音 |
| `poll_interval_s` | `30` | 抓数间隔秒数，最小 5 |
| `log_max_rows` | `500` | 日志保留条数，最小 100，下次启动生效 |
| `improve_pct` | `0.3` | 更优幅度 % |
| `slippage_tol_pct` | `0.3` | 关闭更优开关后的可接受滑点 % |
| `better_price_enabled` | `true` | 开=更优价，关=容差内限价 |
| `mode` | `dry-run` | `dry-run` / `testnet` / `live` |
| `theme` | `dark` | `dark` / `light` |
| `auto_poll` | `true` | 程序启动后自动开始轮询。关掉就得每次手点，忘了就是整夜不跟 |
| `autostart` | `false` | 开机自启。**由配置写/删**启动文件夹的 `币安跟单.lnk`（指向 `pythonw.exe`），别手工改那个目录；每次启动会自检并自愈 |
| `keep_awake` | `true` | 轮询期间阻止系统睡眠（`PowerRequestSystemRequired`，显示器照常熄灭） |
| `rebalance` | `true` | 每轮按账本对账补差：漏掉一条开仓后靠它自愈补回等比目标 |
| `managed_capital` | `0` | 管理额度（USDT）。`0` = 用账户全部权益；`> 0` = 只按这个额度等比，相当于「可用 100 U 里只划 50 U 给程序」。超过账户权益时按权益封顶，不会凭空放大。dry-run 忽略此项（用模拟本金） |

`_load_config()` 只认 `DEFAULTS` 里的键，非法值回落默认；无 API 凭证时 `mode`
强制回落 `dry-run`。保存走 `_save_config()`，凭证不写这个文件。

### `.env`（凭证，不进版本库）

```
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_TESTNET=true
BINANCE_NET_MODE=auto                       # auto | direct | proxy | proxy_first
BINANCE_NET_PROXY=http://127.0.0.1:20122    # 本地代理端口，按代理软件实际设置改
```

`BINANCE_NET_MODE` 只作用于合约 API / 账户自检 / 下单链路；取数链路
`api_feed.LeadApi` 固定用 `proxy_first`，不受这个键影响。
`BINANCE_NET_PROXY` 让 `www.binance.com` 的请求显式走本地代理，
不依赖系统 PAC 规则，也不需要开 TUN；留空则跟随系统代理。
端口以代理软件实际设置为准，换了端口改这一行。详见第 6 节。

`net._dotenv_value()` 会直接读项目根 `.env`，所以 `api_feed.py` 单独跑
（不经过 `account.load_env`）也能拿到同一份网络配置。

### `state.db`（SQLite，程序自动创建）

- `meta(k, v)`：`last_run_wall`、`lead_portfolio_id`、`follow_start_wall`、
  `ledger_json`、`lead_wallet`
- `seen(k, seen_wall)`：已处理事件水位（有 TTL，不是历史事件的唯一防线）
- `pending(symbol, order_id, side, qty, lim, leverage, created_wall, source_key)`
- `pnl_snapshot(ts, mode, equity, avail, unreal, exposure, margin, n_pos,
  lead_wallet, lead_unreal, lead_exposure)`：每轮一行，`(ts, mode)` 主键
- `pnl_position(ts, mode, symbol, side, qty, entry, mark, unreal, notional)`：
  每轮每品种一行
- `pnl_trade(ts, mode, kind, symbol, side, qty, price, notional, realized, note)`：
  我方动作流水，`kind` 取 `OPEN` / `CLOSE` / `CANCEL`

`pending` 用挂钟时间存（monotonic 跨进程无意义），恢复时反算年龄。
`lead_portfolio_id` 用于识别换人；`follow_start_wall` 是基线时窗口内最新成交
时间，`drain()` 永久忽略 `stamp <= 它` 的事件，防止 `seen` TTL 淘汰后历史
仓位复活成新信号。`ledger_json` 存 `(品种,方向) -> 他还持有的数量`。

## 5. 必须守住的不变量

1. **幽灵单红线**：`CopyEngine._close()` 无条件 `cancel_all_orders(symbol)`，
   与入场价是更优价还是容差价、与我方有没有仓位都无关。带单员平仓后，
   我方未成交的入场限价单必须消失，否则价格回落会成交一笔无人管理的仓位。
2. **事件时序**：按 `OPEN/CLOSE` 事件顺序处理，不能用持仓快照 diff。
   快照会漏掉两次轮询之间「开仓又止损平仓」的中间态。
3. **去重键**：`Fill.key = symbol|side|action|秒级时间戳`，不含聚合后的
   `qty`/`price`。同秒逐笔成交分两轮返回时聚合值会变，把数值放进 key 会重复开平。
4. **入场价单一真源**：`feed.entry_limit_price()`。多单更优挂更低、空单挂更高；
   容差模式允许多单挂更高 / 空单挂更低，按 `tickSize` 向下/向上取整。
   改一处即可，不要在 `better_executor.py` 里另写一遍。
5. **等比口径是数量等比，唯一入口 `target_position()`**：
   `我的数量 = 他的数量 × (我的权益 / 他的钱包余额)`，按 `step` 向下取整。
   `_open()`、`rebalance()`、仓位管理页、成交事件表的「应开」全部调它 ——
   2026-11-16 之前 `_open()` 自己按「名义 / 限价」算过一遍，限价偏离他均价的
   幅度全部变成数量偏差，界面显示的应开数量不是实盘会下的数量。
   杠杆只影响保证金占用，固定 20x，不参与这个比例。
   杠杆默认值在 `feed.Rule.leverage`，**不在 `gui-config.json` 里**；
   `_open()` 每次下单前都会 `set_leverage(symbol, 20)` 与
   `set_margin_mode(symbol, "ISOLATED")`，会覆盖该品种在账户里已有的设置。
   要改成可配，需同时动 `DEFAULTS`、界面控件、`_collect_config()`、
   `_apply_runtime()`（把值写进 `self.rule.leverage`）。
6. **下单 POST 不重试**：避免重复成交。请求超时但交易所其实挂上的情况，
   靠每轮 `rebuild_from_exchange()` 用 `clientOrderId` 的 `copy_` 前缀认回来；
   确实失败则 `_unsee()` 撤回事件，下一轮补开。
7. **重启窗口只吞老 OPEN，不吞 CLOSE**：`resume_cutoff = now - max_signal_age_s`。
   过老的开仓信号记水位不补开；平仓信号一律放行，因为撤单/平仓是保护动作。
   关掉程序期间带单员止损了，重启后这条必须执行，否则留下一笔无人管理的
   仓位或挂单。`_close()` 幂等，重复执行只会撤单。
8. **换带单员走 `engine.begin_following(pid)`**：重建 `LeadApi`、清详情缓存、
   清内存与 SQLite 的 `seen` 和账本、重置跟单起点与昵称缓存、下轮重建基线；
   `pending` 保留（那是我账户里的真实挂单）。
9. **开始/停止是同一个开关按钮**：`toggle_polling()` 按 `running` 切换，
   `_set_poll_button()` 同步文字与强调色，不存在两个按钮状态打架。
10. **轮询等待用 `_poll_wake`，不能用 `running`**：`running` 在轮询期间恒为
    set，`Event.wait(N)` 会立即返回，实际间隔只剩一次网络请求耗时。
    `stop_polling()` 通过 `_poll_wake.set()` 立刻唤醒等待。
11. **换人基线只挡「旧事件」，不挡「未平的历史仓位」**：`state.db` 记住
    `lead_portfolio_id` 与 `follow_start_wall`。换人首轮把窗口内历史事件标为
    已见（TTL 淘汰也不复活），但 `position_ledger.replay()` 判定他**至今仍持有**
    的批次，按原始开仓价挂更优价限价单 —— 只有价格回到那个位置或更优才成交，
    不追高；已被 CLOSE 核销的批次一律不跟。
12. **账本每轮全量重放，不靠增量累加**：`close_before[f.key]` 给出每笔 CLOSE
    发生前的持仓量，`f.qty / before` 是平仓比例；`open_context[f.key]` 给出
    每笔 OPEN 当时的累计数量/名义/加权均价，加仓按累计补差额。名义按**被核销
    批次的开仓名义**扣，不能用 CLOSE 的平仓名义（价格不同会拉偏基准）。
    平前持仓缺失、比例 ≥1、账本不可用时整仓平兜底；部分平仓带 `reduceOnly`。
13. **真实模式每轮同步账户权益**：`my_equity` 默认 10000 只供 dry-run 演示。
    `live/testnet` 用 `get_balance()["total_balance"]` 覆盖（不能用
    `available_balance`，它会随占用保证金缩水）；读不到且从未验证过就停止跟单。
14. **失败必须让事件回到队列**：撤单失败、读取持仓异常、平仓下单失败都要
    `_unsee(f.key)`，否则 CLOSE 已进 `seen`、下轮不再处理，会留下挂单或未平仓位。
    `get_positions()` 返回空不能当成「无仓位」。
15. **历史挂单豁免 300s 超时**：`PendingOrder.historical=True`（由
    `clientOrderId` 的 `copy_hist_` 前缀认回）在 `reconcile()` 里跳过超时，
    只有他平仓时由 `_close()` 无条件撤单。普通入场单超时只撤自己那笔。
16. **敞口比例的分母是「钱包余额」，不是 `marginBalance`**：
    `marginBalance` 含未实现盈亏，他浮亏越深分母越小，同一笔开仓算出的
    比例越大 —— 浮亏 60% 时放大 2.5 倍，等于在最不该加仓的位置被动加杠杆。
    `position_ledger.wallet_state()` 用 `marginBalance − 未实现盈亏` 还原
    钱包余额，未实现盈亏由 `surviving × fapi 标记价` 算出。他空仓时未实现
    为 0，此时 `marginBalance` 就是精确值，是每次的校准点，所以推算误差
    只在一个持仓周期内存在，不跨周期累积。`_open()` 取
    `self.lead_wallet`，为空才回落 `rule.lead_balance`。
    **`marginBalance` 到底含不含浮亏必须实测确认**，用 `wallet_probe.py`：
    他持仓浮亏时若 `wallet` 不动而 `marginBalance` 在跌，分解有效；
    若两者同向同幅变化，说明 `marginBalance` 本身不含浮亏，应把分母退回它。
17. **出入金播报靠 `wallet` 的变化，不靠 `marginBalance`**：
    钱包余额只在已实现盈亏和出入金时变。比较前先扣掉本轮新处理 CLOSE 的
    `realized_pnl`（按 `Fill.key` 去重，防 `_unsee` 重试重复累加），剩下的
    才是出入金。阈值 `max(50, 前值×0.1%)`，挡住手续费和推算抖动。
    口径切换（空仓 ↔ 有仓）那一轮不比较：推算值和精确值不可比。
18. **换主题必须是原子的**：`ThemeManager.apply()` 里一次
    `QApplication.setStyleSheet()` 换完整棵树，紧接着在同一个函数里
    `_recolor()` 重涂 QSS 管不到的三处（表格 item 前景色、`QTextEdit`
    字符格式、下拉框箭头），中间不得调用 `processEvents()` 或 `sleep`。
    不允许把换主题拆成多个 `QTimer.singleShot` 分步执行。
19. **`_announce_render()` 从 0 重画前必须先清空文档**：它按
    `_announce_drawn` 增量插入，切主题时把计数归零再整份重画，如果不清空
    就会把已有内容再追加一遍，播报变成两份。重画前记下滚动位置、画完还原，
    免得用户正在看的段落被甩走。
20. **业务层不依赖界面**：`copy_engine` / `api_feed` / `account` /
    `state_store` / `better_executor` 不许 import `gui` 或 `theme_qss`。
    界面可以随便换，这几个文件应该一行不用动。
21. **自检不许碰真实配置**：`selftest()` 会把模块级 `CONFIG_FILE` 指向临时
    文件，`main()` 里再比一次真实文件的字节，被改写就断言失败。原因：
    `_toggle_theme()` 末尾要存盘，自检点了两次切主题，于是每跑一次回归就把
    用户的实盘参数覆盖一遍（实测把 `better_price_enabled` 写成 `false`）。
    新增任何「自检里会触发存盘」的路径，都必须走这个临时配置。
22. **窗口是无边框的，拖动 / 缩放靠 `WM_NCHITTEST` 交回系统**：
    `App.nativeEvent()` 回答命中码 —— 标题栏空白 `HTCAPTION`、四边四角
    `HTLEFT`…`HTBOTTOMRIGHT`、**按钮上必须 `HTCLIENT`**（返回 `HTCAPTION`
    会让按下变成拖窗口，按钮点不动）。不要改成手写鼠标追踪：交给系统才有
    Aero Snap、双击最大化、右键系统菜单。坐标是「屏幕物理像素 − 窗口原点
    ÷ `devicePixelRatio()`」，150% 缩放下漏了 DPR 会整体偏移。
23. **`WM_GETMINMAXINFO` 必须自己写回 `ptMinTrackSize`**：接管这条消息后
    Qt 自己的最小尺寸约束失效，实测不写就能把窗口拖成 0×0。同一条消息还负责
    把最大化限制在显示器工作区 —— 无边框窗口没有 `WS_MAXIMIZEBOX`，
    Windows 默认铺满整块屏幕、盖住任务栏。
24. **标题栏是 QSS 上色的，不碰 DWM**：Win10 上 `DwmSetWindowAttribute`
    写入返回 `hr=0` 但画面不变、切主题也不跟随（详见第 8 节）。整条标题栏
    由 `QWidget#titlebar` 绘制，切主题只走 `setStyleSheet()`。窗口按钮的
    `role` 只能是 `winbtn` / `winclose`，符号由 `WinButton.paintEvent` 画。
25. **价格步进必须用 `PRICE_FILTER.tickSize`，不能用 `pricePrecision`**：
    `BetterPriceExecutor.price_step_of()` 走 `BinanceService.get_symbol_tick()`。
    代币化股票合约（NVDAUSDT / SOXLUSDT）`pricePrecision=5`、`tickSize=0.01`，
    按 1e-5 取整挂出去会被 **-4014** 拒；BTCUSDT 也是 `pricePrecision=2` /
    `tickSize=0.1`。查不到 tick 才回落到 `pricePrecision`。
26. **双向持仓（hedge）模式下每笔订单都要带 `positionSide`**：
    `BinanceService._apply_position_side()` 按 `(side, reduce_only)` 推：
    开仓 positionSide = 订单方向，平仓 = 持仓方向（SELL 平多 → LONG）。
    双向模式下**不能**发 `reduceOnly`（-1106 会拒），平仓靠 positionSide
    表达方向、数量由 `close_position()` 夹到持仓量以内。
    单向模式下反过来：不发 positionSide、平仓靠 reduceOnly。
    调用方（引擎）不需要传这个参数，也不该写死。
27. **保证金模式是全仓（`Rule.margin_type = "CROSSED"`）**：跟带单员一致。
    逐仓会让单笔被单独强平，和「照抄他的仓位、跟着他止损」不是一回事。
    注意币安的参数值是 `CROSSED` 不是 `CROSS`。另外**有持仓时改不了模式**
    （-4048），要改只能 平仓 → 改 → 原量重开。
28. **我方不挂任何止盈止损**：`tp_roe_pct=None` / `sl_roe_pct=None`，
    出场只有一条路 —— 带单员平仓时跟平。别在这里加"保护性止损"，
    那是另一套口径（他扛回来我们被扫出去，仓位就断了）。
29. **出入金只在「上一轮和这一轮都是空仓」时比较**：持仓期间 `wallet` 是
    推算值，而推算只覆盖 `trade-history` 返回得到的仓位 —— 接口一次只给最近
    一个仓位的成交，他同时持九个品种时我们只算得到一个的浮盈。拿这个值跟
    上一轮比，差额会全被当成出入金，每次价格波动都误报一条。
    空仓时 `wallet = marginBalance` 是精确值，才是唯一可比的锚点。
30. **同一时间只能有一个实例**：`acquire_single_instance()` 锁 `gui.lock`。
    开机自启 + 手动双击同时发生会有两个进程各自下单，仓位翻倍。
31. **轮询期间阻止系统睡眠、但显示器照常熄灭**：`set_keep_awake(True)` 走
    Power Request API —— `PowerCreateRequest` + `PowerSetRequest(PowerRequestSystemRequired)`，
    **不申请** `PowerRequestDisplayRequired`。Windows 把「系统」和「显示器」拆成
    两个独立请求，只申请前者，VIDEOIDLE 超时完全不受影响。
    早先用 `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`
    （文档上也说不管显示器），但实测在部分机器上会连带把屏幕顶住，挂一夜
    整间屋子亮着；而且它是线程级的、状态跟着线程生死。该 API 调用失败时
    仍退回 `SetThreadExecutionState`，同样不加 `ES_DISPLAY_REQUIRED`。
    系统一睡 socket 全断，醒来时信号早过有效期。`stop_polling()` /
    `closeEvent` 必须恢复，否则关了程序还在拦着系统睡觉。
    此外 `sleep_already_disabled()` 会在 `STANDBYIDLE` 交流/直流都是 0
    （电源方案里「睡眠」已经是「从不」）时直接跳过，**一条请求都不申请** ——
    不需要的句柄就不该存在。这种情况下日志会写一行
    「电源方案里睡眠已是「从不」，不申请唤醒请求」。
    验证：管理员命令行跑 `powercfg -requests`，SYSTEM 段应有本进程，
    **DISPLAY 段必须为空**。
32. **成交事件表的两列都是名义 USDT，且「应开」用累计口径**：
    `rep.open_context` 给的是他这笔开完之后的累计名义，`_open()` 用的就是它。
    按单笔算会显示成「12.5 / 25 / 12.5」，让人以为加起来 50 却只开了 25。
    「实开」= 我当前持仓的名义（数量 × 我的开仓均价），不是历史快照。
    用名义不用数量：数量随币价变，名义才是等比口径。
33. **成交表的列必须能手动拖**：`_table(..., stretch_last=False)`，9 列全
    `Interactive`。`Stretch` 的列宽由剩余空间算，用户拖不动；自检里有断言
    （`resizeSection(0, +60)` 必须生效）防止改回去。
    列宽要按**中文表头**算，不是按数字算 —— 否则「开/平」会被截成「开/…」。
34. **开机自启 / 息屏保持 / 启动即轮询都是配置项，不是写死的**：
    `autostart` / `keep_awake` / `auto_poll` 在 `DEFAULTS` 里，界面上是
    「运行配置 → 运行方式」三个勾选框，保存时立即生效。
    `autostart` 勾了就 `sync_autostart(True)` 写启动文件夹快捷方式、取消就删 ——
    **不要手工往启动文件夹里丢 .lnk**，否则配置说「关」而快捷方式还在。
    写快捷方式必须用 `-EncodedCommand`（base64+UTF-16LE），
    `-Command` 会被 Windows 命令行转义拆坏（实测 `$s.Arguments` 被写成
    字面量 `+ $env:TEN_ARGS +`）。
    **目标必须用 `pythonw.exe`**（`_pythonw()`，找不到才退回 `sys.executable`）：
    用 `python.exe` 登录时先弹一个黑框（`WindowStyle=7` 只是最小化），而且那个
    控制台是父进程，用户顺手关掉黑框会把跟单一起带走。
    **启动时自检（`ensure_autostart()`）**：`App.__init__` 在
    `autostart=True` 且非离屏实例时调一次，快捷方式缺失 / 指错 / 有历史残留
    就重建并写日志。原来只在配置值变化时才写，快捷方式被删掉或被安全软件
    清掉后界面照样显示「已勾选」，看起来就是「开机自启没用」。
    残留按「.lnk 字节里是否提到 `ROOT.name`」判定（`_lnk_mentions()`，
    不走 COM），能连历史上编码坏掉的 `澶栧満璺熷崟.lnk` 一起清掉。
35. **对账补差只补「跟单起点之后」的仓位，且只补不减**：
    `rebalance()` 每轮按 `_replay.totals` 的累计名义算目标，和真实持仓比。
    `_lots_after_start()` 过滤掉他跟单前的历史仓位 —— 否则一开程序就会把
    整个旧仓位本子按市价镜像一遍。已有挂单、差额太小、方向相反三种情况都不动。
    没有它，漏掉一条 OPEN 之后仓位会永久偏小且永不纠正。
36. **成交事件与操作播报都是「最新在最上面」**：
    成交表按 `Fill.ts`（完整时间戳，带年份）倒序，**不要**按展示用的
    `11-13 10:19:42` 排 —— 跨年时 `12-31` 会排到 `01-01` 前面。
    播报按「组」存储（`_announce_groups`），渲染时组倒序、组内也按时间倒序
    （`_announce()` 里 `fresh.sort(reverse=True)`，不能依赖上游顺序：
    引擎给的是重放用的正序）。
    代价是每来一条就要整份重画，600 块上限下开销可忽略。
    滚动位置按新增高度补偿，正在看旧内容时不会被顶走。
37. **提示音不能用 `PlaySound` 的别名**：`PlaySound("SystemExclamation")`
    读 Windows「声音方案」，方案设成「无声」或提示音被单独关掉时完全不响，
    且没有返回值能看出失败 —— 表现就是「播报出来了但没声音」。
    用 `winsound.Beep`（与声音方案无关），它会阻塞调用线程（实测 259 ms），
    所以必须放后台线程。声音只跟「声音提示」复选框走，不再按 dry-run 拦。
38. **事件日志的 ACTION 行必须过 `format_action()`**：直接 `json.dumps`
    出来的是被列宽截断的原始 dict，复盘时读不懂。布尔值写成「是/否」，
    金额带单位。原始 dict 留在 `engine.actions`，需要程序化处理取那个。
    另外 `QueueLogHandler` 的 formatter 只有 `%(message)s` —— 级别是表格
    的独立一列，正文里再带 `INFO ` 是重复；落盘日志才带级别。
    与 ACTION 行重复的引擎日志（开仓跟随 / 平仓跟随 / 仅撤单）走 `debug`。
39. **「他的仓位 → 我该持多少」只有 `copy_engine.target_position()` 一份实现**：
    `rebalance()` 和「仓位管理」页都调它。各写一套的话，排查时看到的数和
    实际下单的数可能不是一回事，等于白排查。改口径（更优幅度、取整、门槛）
    只改这一处。
40. **持仓在界面线程只读一次**：`_run_engine()` 在引擎线程取
    `get_positions()`，同时喂给「实开」列和仓位管理页（`snap["positions"]`）。
    界面线程再调一次就是一次签名 REST（200–500 ms），每 30 秒卡一次界面。
41. **模拟仓无状态**：`SimPortfolio.replay()` 每次从空仓重放，不累积。
    有状态会像实盘一样攒下历史误差，排查时分不清是数据错还是账本错。
    「差额」= （模拟数量 − 真实数量）× 应开均价，用 `WARN` 色 —— 少开和多开
    都是偏差，用 POS（绿）会让「该开没开」看起来像盈利。
42. **带单员标识只在本次会话确认后显示**：`_lead_display()` 不读
    `lead_nickname` 缓存（那是上次运行留下的，可能是别人的），只认
    `_lead_verified`（接口真实返回过昵称）。未确认写「未连接」，
    确认后写「昵称 · portfolioId 尾 6 位」，换人立即作废。
    `_ensure_lead_name()` 每 30 秒补拉一次；`enable_store=False` 的离屏实例
    （自检、截图）必须不联网。
43. **文案称谓固定**：界面与日志里写「带单员」「本程序」，不写「他」「我方」；
    播报字段冒号用全角。改文案时连同上一条一起看，别只改一处。
44. **仓位页两列占比必须同口径**：「带单员占比」= 名义 ÷ 带单员钱包，
    「本程序占比」= 名义 ÷ 本程序权益，两边都是「名义 ÷ 权益」，才能直接比杠杆。
    行按偏差绝对值降序排列，孤儿仓自然置顶。
45. **份额按数量等比，不按名义等比，且只有 `target_position()` 一份实现**：
    `target_qty = 带单员数量 × (权益 ÷ 带单员钱包)`，与限价无关；
    `target_notional` 只是「数量 × 限价」的展示值。
    名义口径（先算 USDT 敞口再除以限价）在限价与带单员成交价不一致时会
    多算或少算数量。价格相同时两者完全等价 —— 改口径时别用「价格相同」的
    数据验证，看不出区别。
    **`_open()` 也必须走它**：2026-11-16 之前 `_open()` 自己按
    `floor(my_notional / limit / step)` 算过一遍，界面上「应开数量」
    （按数量等比）和实盘真正下的数量（按名义等比）是两套数，
    实测同一组数据下相差 0.7%。
46. **口径由「锁定状态」决定，不靠数值接近去猜**：点「按实盘权益」置
    `_sim_pin_real = True`，之后每轮直接取最新权益；手动改输入框
    （`textEdited`）解锁回模拟口径。曾经用「两者相差 <1%」判断，权益浮动
    超过 1% 就切回「模拟资金」—— 用户看到的就是「刚开成实盘又变回去」。
    锁定后标题、输入框标签、摘要都不许出现「模拟」，自检里有断言守着。
47. **这一页的布局必须点「重算」不跳**：摘要与说明设固定高度（3 行），
    输入框左侧标签固定宽度（「模拟资金」4 字 /「本程序权益」5 字），
    列宽用固定基线且只在窗口宽度变化超过 40px 时重排 —— 按内容自适应
    会让列宽随数字位数每轮抖动。
48. **入场限价单不自动撤**：`Rule.entry_timeout_s = 0`（GUI 口径）。
    更优价挂单本来就是等价格回到更好的位置，撤了等于主动放弃这一单；
    只有带单员平仓时 `_close()` 才无条件撤。要恢复超时撤单就把它设回秒数。
49. **盈亏有三条口径，不许混用**：
    - **钱包余额** `equity` = `totalWalletBalance`，已实现盈亏已在里面，浮盈不在；
    - **浮动盈亏** `unreal`，优先取交易所的 `unRealizedProfit`（含资金费），
      拿不到才用 `(标记价 − 开仓价) × 数量` 估；
    - **净值** = `equity + unreal` —— 曲线画的是它。只画余额的话，一次平仓
      会让曲线凭空下蹲（浮盈消失、钱进了钱包），看着像亏了。
    **已实现盈亏按 `pnl_trade.realized` 逐笔累加，不做钱包差分**：差分会把
    用户手动划转的钱算成盈利。平仓价拿不到交易所成交均价
    （`place_market_order` 只回 order_id/symbol/side/quantity/status），
    记的是平仓那一刻的标记价，属近似值，UI 上必须标明。
50. **盈亏账本按 `mode` 隔离**：`pnl_*` 三张表都有 `mode` 列，查询一律
    带 `mode` 过滤。dry-run 的持仓来自 `MemoryBroker`（模拟成交、模拟成本），
    和 testnet/live 的交易所数据混在一条曲线上就是假数据。
    `MemoryBroker` 现在记开仓均价（`self.entry`）并把 `unrealized_pnl` 返回
    `None`（不是 `0.0`）—— 写 0 会被当成「真的不赚不亏」，浮盈永远算不出来。
51. **快照写库节流 30 秒，动作流水一条不漏**：10 秒一条一天就是 8000 多行，
    而这个页面的用途是看趋势。有动作时无条件写。`prune_pnl()` 每天清一次
    90 天前的快照/持仓，动作流水不清（那是账本）。

52. **程序只管理「自己下的单」，账户里其余东西一律当别人的**：这条是
    2026-11-16 引入「管理额度」时定下的硬约束，四个入口都必须守：
    - **平仓量按账本算**（`filled`），不是按账户持仓。`managed =
      min(账本, 账户持仓)`，方向不符就不动手。账户里多出来的部分是用户
      自己开的仓，平掉它等于擅自处置别人的钱。
    - **整仓平时只在「刚好覆盖账户全部持仓」时才传 `quantity=None`**，
      否则一律传具体数量（`_close` / `close_manual` 同一套判定）。
    - **撤单只撤带 `copy_` 前缀的单**（`_cancel_ours`）。不能用
      `cancel_all_orders`：那会把用户自己在同一品种挂的止盈/止损一起撤掉。
      特别注意 `TradingExecutor.close_position()` 内部会先
      `cancel_all_orders` 一次，所以平仓必须直接调
      `executor.binance.close_position()`。
    - **账本不被账户持仓污染**：`_open` / `rebalance` / `cancel_entry`
      都不再把「账户持仓比账本多」并进 `filled`。并进去程序就会以为已经
      跟满，后续事件少开甚至不开。
53. **账本缩减按「实际平掉的数量」**：`floor` 之后实际平仓量小于
    理论值 `book*ratio`，按理论值缩会让账本比真实持仓小，下一轮
    `rebalance` 又按差额补回来 —— 「平了又补」就是这么产生的。
    只在 `closed_qty<=0`（无仓可平/方向不符）时才退回按 `ratio` 缩。
54. **额度变更要给净值曲线打水位**：`managed_capital` 变了就是换本金，
    曲线不打 `live_rebased_at` 会在两轮之间从 1000 台阶掉到 50，
    累计盈亏立刻 -950。只认**配置值**变化，不认权益波动（权益每轮都在动，
    按它 rebase 曲线永远只有一个点）。
55. **额度输入必须规范化**（`norm_capital`）：`abc` / `nan` / 负数 / 空串
    一律当 0。`nan` 尤其危险 —— 它能过 `float()` 和 `>=0` 校验，然后
    `floor(nan)` 让引擎每轮抛异常，界面上看不出任何异样。

56. **必须逐仓（ISOLATED），不能全仓**：全仓下所有仓位共享账户保证金，
    一个仓位亏光会吃掉整个账户余额 ——「投给这个交易员 100 U，最多亏 100 U」
    在全仓下物理上不可能。逐仓下爆仓损失以该仓位保证金为限。
    代价：单笔会被单独强平，不会像全仓那样被其他仓位的浮盈兜住。
57. **逐仓预算 = 该交易员的额度**：`CopyEngine.margin_budget`，
    `margin_used()` 只统计「账本那部分」持仓的保证金（`min(账户持仓, 账本)`），
    `_fit_to_budget()` 在 `_open()` 里裁量。**保证金之和 ≤ 预算**就等价于
    「最坏情况亏损 ≤ 预算」。超预算时缩量、缩到 0 就不开，
    绝不为了凑等比目标而超配。
58. **爆仓价优先用交易所的 `liquidationPrice`**：本地公式
    `entry×(1−1/L±MMR)` 只在交易所没给值时兜底（dry-run 走这条），
    MMR 取 0.004 是保守常数，真实值按名义分档。

## 6. 网络与限流

**两个币安域名的可达性不同，必须用不同的通道顺序**：

| 域名 | 用途 | 强制直连 | 走本地代理 | 程序用的模式 |
|---|---|---|---|---|
| `www.binance.com/bapi/...` | 取带单员流水 | 8s 超时，不通 | 200 / 0.18s | `proxy_first` |
| `fapi.binance.com` | 合约账户 / 下单 | 随本机代理或 TUN 状态变化 | 通常可用 | `auto` |

`net.BinanceHttp` 持有两个 Session：`direct.trust_env=False`（忽略系统代理）与
`proxied`（跟随系统代理；设了 `BINANCE_NET_PROXY` 时固定走该端口）。
`api_feed.LeadApi` 用 `proxy_first`，`account.py` 与
`vendor/chatdsn/binance.py` 用 `auto`。首次成功后记住通道（`_preferred`），
后续请求直接走它，不必每轮先撞一次不通的路。只有网络异常和 5xx/429 才重试；
4xx 直接返回。超时是 `(3.05, 8~10)`，连不上 3 秒就换通道。

**不开 TUN 的取数办法**：在 `.env` 里填
`BINANCE_NET_PROXY=http://127.0.0.1:20122`（端口以代理软件实际设置为准），
程序就显式把 `www.binance.com` 的请求交给本地代理，不依赖系统 PAC 规则是否
命中该域名。留空则跟随系统代理；TUN 开着时两者都能工作。

`net.py --selftest` 的 PASS 只判定纯逻辑（通道顺序、断线重试、显式代理、
通道记忆），连通性结果只打印不参与判定，避免本机代理/TUN 状态变化让回归假红。

`api_feed.LeadApi._request()` 单独处理业务忙码 `11012005`：最多 6 轮、
退避 `min(0.8*2^n, 6s) + jitter`。这个限流窗口可达 1 分钟以上，重试再久也可能
全撞上；此时 CLI 返回 `rc=2`，`run_all.py` 标 SKIP，GUI 轮询显示
「带单本接口限流，N 秒后自动重连」。轮询恢复后拉的是全量历史，事件不会丢。

轮询循环本身是 `while running.is_set()` 无限重连，失败按 `min(2^fails, interval)`
退避，不会自己停。

## 7. 常见改造入口

| 想改什么 | 动哪里 | 注意 |
|---|---|---|
| 换带单员 | `gui-config.json` 的 `portfolio_id`，或界面「运行配置」 | ID 必须逐位核对；换人自动清 `seen` |
| 换数据源 | `api_feed.LeadApi`，保持 `fills()` 返回 `list[Fill]` | 聚合口径不能变 |
| 改跟单策略 | `copy_engine.CopyEngine._open()` / `_close()` | 别动 `_close()` 的无条件撤单 |
| 改平仓比例 | `position_ledger.replay()` 的 `close_before` + `_close(f, ratio)` | 分母是「这笔 CLOSE 发生前他还持有多少」；`quantity=None` 仍是整仓平 |
| 改加仓口径 | `open_context` / `_open(lead_notional, lead_avg_price)` | 旧单已部分成交时按 `累计目标 − 实际持仓` 补差额，不能按全量重挂 |
| 改播报 | `_build_announce_tab()` / `_announce()` / `_play_sound()` | 三个「不可用」字段不得用我方 20x 或余额冒充；dry-run 不发声 |
| 改仓位口径 | `feed.Rule` + `copy_engine._open()` 的 `my_notional` | 与 GDD 口径同步；分母是 `lead_wallet`，不是 `marginBalance` |
| 改敞口分母 | `position_ledger.wallet_state()` + `copy_engine._update_wallet()` | 空仓锚点不能去掉，否则推算误差会跨周期累积 |
| 加参数项 | `DEFAULTS` + 控件 + `self._inputs/_checks/_combos` + `_collect_config()` | 四步都要做，`_load_config()` 里加校验；少一步旧配置文件会静默缺键 |
| 加 tab | `_build_widgets()` 里 `self.nb.addTab(...)` | 页面套 `scroll_host()`，分组用 `card()`；同时改自检里的 `pages == 8` 和（若动末位）`tabs[-1]` 两条断言 |
| 改配色 | `theme_qss.py` 的 `DARK` / `LIGHT` 色板 | 自绘控件取色走 `CURRENT`，它跟着 `apply()` 一起换 |
| 改圆角 | `theme_qss.py` 的 `R_CARD/R_TABLE/R_BTN/R_INPUT` | QSS 里不要就地写魔数 |
| 改交易所 | `vendor/chatdsn/binance.py` + `account.py` | 两端 host 要一起改 |
| 加回归项 | `run_all.py` 的 `CHECKS` / `FULL_ONLY` | 期望码支持 `None` / `int` / `tuple` |

## 8. 已知坑

- **`portfolioId` 是 19 位**：`1234567890123456789`。截图里肉眼容易漏一位，
  抄错会返回 `11012030 当前项目已关闭`。涉及长数字一律放大逐位核对。- **`trade-history` 是 POST**，body `{portfolioId,page,rows}`；GET 会报 `000002 参数非法`。
- **接口返回逐笔撮合成交**：一次平仓可能拆成几十笔同秒记录，必须先 `aggregate()`。
- **网页版与 App 版中文流水不同**：`feed.py` 的正则同时兼容两者（有无年份/秒、
  「卖出开空」vs「开启做空」、均价后带不带 USDT、盈亏逗号），改文案前先看 `WEB_SAMPLE` 和 `SAMPLE`。
- **`detail` 缓存 60 秒**：`force_refresh()` 会清缓存，别在每轮里绕开缓存直接请求。
- **本机时钟比交易所慢约 32 秒**：`account.py` 和 `vendor/chatdsn/binance.py`
  各自采样 3 次取最小 RTT 做补偿，签名不会因此被 `-1021` 拒；校准系统时间更省心。
- **testnet 需要单独建 Key**：币安主站 API 管理页创建的 Key 只在实盘有效，
  在 `testnet.binancefuture.com` 会返回 `-2015 Invalid API-key`。
  该域名 `reachable=true` 但仍 401，属于认证错误而不是网络故障，开 TUN 无效；
  GUI 的 `_render_account()` 会把 `-2015` 翻译成中文原因。
- **截图验证要走 `widget.grab()`**：150% DPI 下 `QWidget.grab()` 返回物理像素
  （逻辑 1180×740 → 物理约 1769×1109），按逻辑坐标去 `copy()` 会截错位置。
  要单独看某个控件就直接 `widget.grab()`，不要猜偏移。
- **账户里已有手动持仓 / 手动挂单会被牵连，三处**：
  ① `_open()` 的 `have` 读该品种**全部**持仓（不分方向、不分谁开的），
  手动仓会被当成「已持有」，导致我方少开甚至不开，比例失真；
  ② `_close()` 平掉同品种任何仓位，你手动的反向仓会被平掉；
  ③ `cancel_all_orders(symbol)` 撤该品种**所有**挂单，包括你手动挂的保护单。
  切 `live` 前要么用跟单专用账户，要么给引擎加「只管自己开的仓」隔离
  （待决策 #2）。
- **窗口只有 500 条逐笔成交**：开仓滑出窗口时 `close_before` 偏小 → 比例偏大
  → 退化整仓平，安全但会多平。关机几小时且成交密集时，那笔平仓可能整个滑出
  窗口；靠 `pending` 恢复和 `rebuild_from_exchange()` 撤挂单兜底，已成交仓位
  不会自动平掉。
- **`follow_start_wall` 用 `<=` 忽略事件**：与锚点同一秒、但不同品种的新成交
  会被一并吞掉（同品种同秒同方向同开平会聚合成同一 `Fill.key`，本来就不重复）。
  这是当前已知边界，若要放宽需同时解决「基线事件 TTL 淘汰后复活」。
- **部分平仓 FIFO 先核销历史批次**：若历史挂单未成交、新单已成交，按总持仓
  算出的比例仍会作用在我方新单上。当前未做按批次隔离，属于已知边界。
- **播报的三个「不可用」字段**：公开 `bapi` 只提供带单余额和成交流水，没有
  他的保证金明细或逐笔杠杆；`lead-data/positions` 实测返回 `[]`。禁止用
  我方固定 20x 或 `lead_balance` 冒充。
- **昵称回填不能全量写盘**：`_set_lead_nickname()` 只原子写
  `lead_nickname` / `lead_nickname_pid` 两个键。若改用 `_save_config()`，
  GUI 内存里的旧 `portfolio_id` 会覆盖用户在文件里改过的值 —— `live` 模式下
  会跟错交易员。曾实测发生（被写回默认 ID），已修。
- **运行中的 GUI 不会热加载磁盘配置**：直接改 `gui-config.json` 后必须重启
  GUI；否则界面里的「保存配置」会用内存旧值覆盖文件。
- **`marginBalance` 与 `totalWalletBalance` 不是一个口径**：前者含未实现盈亏，
  后者只有已实现。两侧混用会让「浮亏时我方仓位放大、浮盈时缩小」。
  引擎的分母统一用推算的钱包余额（第 5 节第 16 条），
  `gui.py` 的 `_apply_runtime()` 仍用 `totalWalletBalance` 算我方权益，
  这是刻意的：我方权益不含浮亏，他的分母也已剔除浮亏，两侧才对得上。
- **标记价拿不到时仓位偏保守**：冷门币、已下架合约不在 `premiumIndex` 返回里，
  那笔浮亏按 0 计，`wallet` 偏高 → 我方仓位偏小。GUI 日志会列一次品种名，
  不重复刷。用 `wallet_probe.py` 看 `missing` 字段确认是哪些。
- **`wallet_probe.py` 是口径确认工具，不是下单链路**：他持仓期间跑它，
  看 `marginBalance` 在动而 `wallet` 不动，才能确认分解有效。
  一直空仓时两者恒相等，看不出差别。
- **我方权益按轮询周期刷新，不是每秒**：`_apply_runtime()` 每轮读一次
  `get_balance()["total_balance"]`（钱包总额，不含浮亏），间隔是
  `poll_interval_s`（默认 30 秒）。加钱进去最多等一轮；想立刻生效点
  「单次运行」，会重走 `_apply_runtime()`。指标卡「我的权益」显示的就是
  这个值，不是连接时的快照。
- **状态栏的模式文本由 `_on_mode_change()` 维护**：不是 `_render_snapshot()`
  的副产物。没跑过轮询时状态栏显示的是「未运行 · <mode> <短标签>」，
  短标签用 `MODE_SHORT`，不要拿 `MODE_TEXT`（那是整句说明）。
- **QSS 只认 `background-color`，不认 `background` 简写**：写成
  `background: transparent` 会被 Qt 静默忽略，控件退回上一条匹配规则的底色。
  曾经因此让每个 `QLabel` 都被刷成窗口底色，在卡片里显示成一块块深色补丁。
- **别把 `background-color` 挂在通配的 `QWidget` 上**：那样每个布局格子、
  滚动视口都会被强制上色，塞进卡片里就是深色方块。底色只给顶层容器
  （`QMainWindow` / `QDialog` / `QWidget#root`），其余留空由父级透上来。
- **动态属性改完要重新 polish**：`setProperty("role", ...)` 之后必须
  `style().unpolish(w)` + `style().polish(w)`，否则 QSS 不会重新求值
  （`_set_poll_button()` 和 `_set_account_state()` 都靠这个）。
- **`QHBoxLayout.addLayout()` 是横向追加**：想排「三列两行」必须用
  `QGridLayout`，否则两行会被并成一行。
- **Qt 里必须 `app.setStyle("Fusion")`**：Windows 原生风格会忽略一部分 QSS
  规则（尤其 `QComboBox` 下拉和 `QTabBar`），换主题时看起来像没换干净。
- **QSS 管不到的地方有三处**：表格 item 的前景色、`QTextEdit` 里写进
  文档格式的字符颜色、下拉框箭头（QSS 的 border 三角在 Qt 里会塌成实心方块，
  SVG 又依赖 qsvg 插件，所以由 `ComboBox.paintEvent()` 自绘）。
  `_toggle_theme()` 里 `apply()` 之后紧接着 `_recolor()` 把它们按新色板重涂，
  中间不让出事件循环，所以不会露帧。新增直接上色的控件必须加进 `_recolor()`，
  否则切主题只变一半。
- **不要往 `gui.py` 搬「恢复可见时补重绘」那套补丁**：`setStyleSheet()` 是
  整体替换，Qt 在同一个绘制周期重建整棵控件树；窗口从最小化恢复也由 Qt 自己
  重排重绘。那套 `after()` 轮询重画是 tkinter 时代为解决撕裂加的，
  搬过来只会白掉帧。
- **原生标题栏染色在 Win10 上做不到**：`DwmSetWindowAttribute(20)`
  （`USE_IMMERSIVE_DARK_MODE`）和 `(35)`（`CAPTION_COLOR`）写入都返回
  `hr=0`、读回值确实是 1，但 `PrintWindow` 与屏幕抓取都显示标题栏仍是白色；
  `SetWindowPos(SWP_FRAMECHANGED)`、`SetWindowTheme("DarkMode_Explorer")`、
  uxtheme 的 `AllowDarkModeForWindow`（ordinal 133，本机返回 False）、
  hide/show 全部无效。`styleHints().setColorScheme(Dark)` 曾让标题栏变
  `(43,43,43)`、标题文字转白，但再切一次主题就复现不出来。
  所以标题栏是自绘的，别再去调 DWM。
- **`WinButton` 的符号不要用字符**：中文字体里「□」「✕」「—」的字宽和基线
  都不稳，46×34 的按钮里会偏。`paintEvent` 画线可控。关闭按钮 hover 是红底，
  符号要跟着转白（`underMouse()` 依赖 `WA_Hover`，不设属性就永远是 False）。
- **纯 `QWidget` 的 QSS 背景要 `WA_StyledBackground`**：`QWidget#titlebar`
  不设这个属性就不画 `background-color`，标题栏会透出后面的东西。
- **接管 `WM_GETMINMAXINFO` 后要自己写最小尺寸**：Qt 的
  `setMinimumSize()` 约束走的就是这条消息，被我们接管后不再生效，
  实测不写 `ptMinTrackSize` 能把窗口拖成 0×0。
- **无边框窗口的坐标换算必须除 DPR**：`msg.pt`、`GetWindowRect` 都是屏幕
  物理像素，Qt 的 `width()` / `childAt()` 是逻辑像素。本机 150%，
  漏掉 `/ devicePixelRatio()` 会让整片命中区域偏移，表现为「点标题栏
  没反应、拖边缘拖不动」。
- **`-4014 Price not increased by tick size`**：挂单价不是 `tickSize` 的
  整数倍。别用 `pricePrecision` 当 tick（见不变量 25）。查真实值：
  `exchangeInfo` 里 `symbols[].filters[]` 的 `PRICE_FILTER.tickSize`。
- **`-4061 Order's position side does not match user's setting`**：账户
  `dualSidePosition=true`（双向持仓）但订单没带 `positionSide`，或者反过来
  单向账户带了它。查模式：`GET /fapi/v1/positionSide/dual`。
  实盘第一次报这个错，说明**整个账户一笔单都发不出去**，不是偶发。
- **超龄开仓信号会被吞，但不再静默**：`drain()` 里 `age_cutoff`
  （`signal_max_age_s`，GUI 设 900 秒）和 `resume_cutoff` 都会把过老的 OPEN
  标记已见、不下单，同时打 WARN。看到这条日志说明「成交拿到得太晚」，
  要么轮询没开、要么取数一直失败 —— 去 `logs/gui.log` 看那段时间有没有
  「取数失败」。
- **日志现在会落盘**：`logs/gui.log`，2MB × 5 份滚动。界面那个 500 行的
  事件日志是内存里的，重启就没了；复盘「几点几分在干什么」只看文件。

## 9. 验证命令

```bat
py -3.12 run_all.py --full          :: 全套回归 12 项（常规 10 + 真实取数/流水回放 2）
py -3.12 gui.py --selftest       :: 界面：控件 + 同步/异步链路 + 主题切换 + 48 条显式断言
py -3.12 _refs/_test_frame.py       :: 无边框窗口：13 处命中测试 + 最大化铺工作区 + 深浅两套抓图
py -3.12 _refs/_test_drag.py        :: 无边框窗口：注入鼠标实测拖动 / 左边缘 / 下边缘 / 最小尺寸
py -3.12 copy_engine.py --selftest  :: 30 个场景，含真实数字加仓/分次平/失败路径
py -3.12 position_ledger.py --selftest :: 8 个重放 + 钱包余额分解场景
py -3.12 pnl_tracker.py --selftest   :: 5 项盈亏口径：浮盈来源/空头绝对值/撤单不算盈亏/回撤口径
py -3.12 state_store.py --selftest   :: 老库升级 + 曲线排序 + 动作流水去重
py -3.12 net.py --selftest          :: 通道顺序/断线重试/显式代理/通道记忆（连通性只打印）
py -3.12 wallet_probe.py            :: 只读诊断：marginBalance 拆成钱包余额 + 未实现盈亏
py -3.12 wallet_probe.py --watch 30 :: 同上，每 30 秒采样一次，用来看他持仓时两者是否分离
py -3.12 account.py --check --json  :: 只读账户自检（需要 .env 凭证）
py -3.12 _refs/_replay_check.py     :: 真实流水回放 7 项（联网，限流返回 2 标 SKIP）
```

`run_all.py --full` 里 `account.py --check` 未配凭证返回 1 属预期；
`api_feed.py` 撞 `11012005`、`probe_apis.py` 缺 playwright、
`_replay_check.py` 外部接口不可用，均返回 2 标 SKIP，不算 FAIL；
`gui.py` 未装 PySide6 返回 3，同样标 SKIP。
改完代码至少跑 `run_all.py`；改了引擎必须跑 `copy_engine.py --selftest` 与
`position_ledger.py --selftest`；改了仓位口径还要跑真实流水回放；
改了盈亏/曲线还要跑 `pnl_tracker.py --selftest` 与 `state_store.py --selftest`。

## 10. 给接手 AI 的操作顺序

1. 先读 `position_ledger.py` 的 `replay()`，再读 `copy_engine.py` 的
   `process()` / `_close()` / `_open()`，理解「重放 → 事件流 → 下单」；
   不要先看 GUI，界面只是壳。
2. 明确要改的是「取数」「仓位口径」「执行」还是「界面」，只动对应文件。
   `_close()` 的无条件撤单、`Fill.key` 的形状、`entry_limit_price()` 的单一真源、
   `close_before` 的 FIFO 核销口径，这四处不要随手重构。
3. 改完跑 `run_all.py --full`；有真实凭证时补跑 `account.py --check --json`。
4. 改了视觉就跑 `gui.py --selftest`，再用 `widget.grab()` 截图，用
   `ViewImage` 目视确认深色 / 浅色两套。
5. 新增配置项必须同时改 `DEFAULTS`、界面控件、`_collect_config()`，
   并在 `_load_config()` 里加合法性校验，否则旧配置文件会静默缺键。
6. 收尾在 `CHANGELOG.md` 顶部追加一条变更记录（触发 / 改动 / 文件 / 验证 /
   遗留），新出现的取舍进「待决策清单」，并同步更新本文对应章节。
   验证栏只写真实跑过的命令与真实输出。
