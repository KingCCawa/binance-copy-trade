# 币安带单跟单 · 变更日志

| 项 | 值 |
|---|---|
| 版本 | `v0.1.0` |
| 最后更新 | 2026-11-16 08:05 (GMT+8) |
| 状态 | 功能完整；界面层为 PySide6（`gui.py`）；`gui-config.json` 当前 `mode=dry-run`（`.env` 已配凭证），见「待决策清单」#9 |
| 回归基线 | `run_all.py` 10/10 PASS；引擎 38/38；重放 8/8；盈亏 5/5；GUI 54 断言；真实回放 7/7 |
| 配套文档 | `MAINTENANCE.md`（现行结构 / 改造入口 / 验证命令） |
| 项目路径 | `E:/.AIGC/binance-lead-copy/` |

**两份文档的分工**：`MAINTENANCE.md` 回答「现在是什么、改哪里、怎么验证」；
本文件回答「怎么走到现在、每次改动的动机与实测结果、还有什么没定」。
后续改造升级和换 agent 接手，先读本文件的「交接卡」和「待决策清单」，
再按 `MAINTENANCE.md` 动手。

---

## 文档维护约定（给后续 agent）

1. 每完成一轮代码或配置改动，在「变更记录」**顶部**追加一条，字段固定为
   `触发 / 改动 / 文件 / 验证 / 遗留`。
2. 「验证」只写真实跑过的命令与真实输出；没跑的步骤写「未执行」并给出
   下一条可复现命令。不写「应该通过」「预计正常」。
3. 只记录现行代码的来源与决策理由，不对方案做评价式回溯。适用范围发生
   变化时用「边界更正」写明新证据，避免后来者重复下同一个结论。
4. 待决策统一进「待决策清单」；拍板后把结论移到对应变更条目，并注明
   落地文件，不留悬空条目。
5. 新增配置项、新增 tab、改引擎、改视觉之后，同步更新 `MAINTENANCE.md`
   的对应章节，并追加本文件条目。
6. 改用户文件（`.env`、`gui-config.json`、`state.db`）前留可回滚路径；
   改运行中的服务器 / GUI 每次先问。

**追加条目模板**

```markdown
### YYYY-MM-DD HH:MM · 一句话标题

**触发**：用户诉求或实测发现。
**改动**：
- 具体行为变化（文件:函数 级别）
**文件**：`a.py`、`b.py`
**验证**：`py -3.12 <命令>` → 真实输出
**遗留**：未解决项 / 已进待决策清单的编号
```

---

## 变更记录（时间倒序）

**播报不再播「没执行的历史信号」**

触发：用户看到 11-15 的开空播报，以为刚发生了操作，实际是重启后
把历史事件重新播了一遍。

1. **超龄开仓信号不播**：它本来就被 `drain()` 判为超龄、没有下单，
   播出来只会误导。按 `rule.signal_max_age_s`（默认 900 秒）过滤，
   跳过的条数会写一条 INFO 日志。**平仓信号不按年龄过滤** —— 它一定会
   被执行，晚到也必须让人看见。
2. **去重表持久化**：`_announced` 是内存 `deque(maxlen=200)`，重启即清空，
   于是历史事件被当成新操作再播一次。现在读写 `state.db` 的
   `announce_keys`，启动时恢复。

文件：`gui.py`

验证：
- `py -3.12 gui.py --selftest` → **54 asserts**（新增：喂一条 11-15 的超龄
  OPEN，断言 `OLDUSDT` 不出现在播报正文里）
- `py -3.12 run_all.py` → **10/10 PASS**

**盈亏页加时间周期（7 / 30 / 90 / 365 天 / 全部），数据不足如实说明**

触发：用户要求像币安资产页那样能切时间段，并且「程序没跑那么久就不能假装有」。

1. **周期选择**放在指标卡标题行右侧，默认「全部」。只筛显示与区间统计，
   **不删数据**；切周期立刻重算净值、区间收益率、最大回撤。
2. **数据不足的显式提示**：区间跨度大于程序实际记录时长时，摘要里写
   `· 程序只记录了 8.8 天数据`；区间内点数不足 2 条时写
   `该区间内没有成交或权益变化，曲线不足两点`。不拿更早的点凑数。
3. **修掉一处双算**：`累计盈亏` 原来是 `区间净值变化 + 已实现盈亏`，
   但净值（钱包 + 浮盈）本身就已经含了已实现，叠加是重复计算。
   自检断言同步改为 `100 → 105 = +5.00`。

文件：`gui.py`

验证：
- `py -3.12 gui.py --selftest` → **53 asserts**（新增：周期项 5 个、默认「全部」）
- `py -3.12 run_all.py` → **10/10 PASS**
- 离屏实测（9 天数据 / 60 点）：选「最近 30 天」→ 60 点 + 提示「只记录了 8.8 天」；
  选「最近 7 天」→ 46 点、区间收益率从 +2.81% 变 +1.60%、不再提示

**盈亏曲线改事件驱动：不再每 30 秒写一条**

触发：用户指出「资产变化图应该平仓才变一次，这个频率太高，性能影响大」。

1. **写库条件**：成交（开/平/撤）**或** 权益变化 ≥ max(0.5 U, 0.1%)
   **或** 距上次 ≥ 15 分钟（保底锚点）。其余轮次不写。
   两次交易之间本来就是一条平线，按 10 秒采样只是把直线切成一万个点。
2. **读库降频**：曲线/流水/已实现金额改成缓存，只在写过库或首次时回读。
   原来每 10 秒从 SQLite 拉 800 行、构造 800 个 dict，纯浪费。
3. **曲线尾巴补实时点**：渲染时把「此刻」的权益/浮盈追加到缓存末尾
   （不落库），否则图上最后一点会停在 15 分钟前，看着像程序停了。
4. **窗口 800 → 2000**：点变稀疏后，2000 点能覆盖更长历史。

实测（同一份持仓、30 轮无成交无权益变化）：

```
旧逻辑    pnl_snapshot 30 行
新逻辑    pnl_snapshot  1 行
有平仓动作           +1 行
```

线上库现状：147 行 / 1.35 小时（≈2800 行/天、WAL 1.7MB）。
按新口径同场景约 1 行，写入量降 2 个数量级。

文件：`gui.py`

验证：
- `py -3.12 gui.py --selftest` → **52 asserts**
- `py -3.12 run_all.py` → **10/10 PASS**
- 实测脚本：30 轮无变化写 1 行；追加一笔平仓动作后 2 行

**模拟资金口径统一：输入框只是本金，权益由模拟账户算出来**

触发：用户指出「模拟资金」和「仓位对账」的资金不是一回事，改余额又像
「重新开始跟单」，模拟期的收益率到实盘还要能对上。

1. **根因**：`_apply_runtime()` 的 dry-run 分支只设了 `_pnl_avail`，从不动
   `rule.my_equity` —— 于是输入框写 1000、引擎按 `Rule` 默认 10000 算仓位、
   盈亏账本也按 10000 记账。三个数字互相矛盾，这就是「反人类」的来源。
2. **统一为模拟账户**（`sim_state()`）：本金 + 历史段累计 + 本期已实现。
   dry-run 下 `rule.my_equity` 直接取它，仓位比例、盈亏账本、仓位对账页
   从此是同一个数。**权益不含浮盈**：含浮盈会让比例随行情振荡，
   同一笔加仓每轮算出不同数量。
3. **改本金 ≠ 清零**（`_set_sim_principal()`）：把「本期已实现」累进
   `sim_carried`，并给已实现打快照 `sim_epoch_realized`，之后
   `本期 = realized - epoch`。盈亏页显示
   `模拟账户 本金 1000.00 U · 本期 +23.45 U（+2.35%）· 历史段 +200.00 U`。
4. **离开模拟盘留档**：`_switch_broker()` 在 dry-run → testnet/live 时把
   模拟期成绩写进 meta `sim_graduation`（本金、盈亏、百分比），切回来还能对上。

文件：`gui.py`

验证：
- `py -3.12 gui.py --selftest` → **52 asserts**
- `py -3.12 run_all.py` → **10/10 PASS**
- 自检里 `sim.equity` 仍为 `1000`（输入框显示本金），摘要中的计算基数
  与该值一致

遗留：
- 「仓位对账」页的模拟数量仍由 `SimPortfolio` 离线投影给出，与实际
  `MemoryBroker` 撮合持仓可能不同（一个是投影、一个是撮合），页签标题已区分。

**未成交挂单加「市价追单」，去掉「平 1/2」**

触发：用户要求「行情一路走掉、更优价等不到」时能按现价把这一笔追进去；
并指出「平 1/2」没必要。

1. **`chase_entry(symbol)`**：撤掉限价单 → 按市价开出同样数量。
   账本处理与 `_open` 的撤旧重挂一致（撤单先扣回、成交后加回），
   追完账本仍等于「按事件应持」，下一轮 `rebalance()` 不会再补一次。
   撤单未确认时**拒绝追单**，避免两张单叠加。
2. **`MemoryBroker.place_market_order()` 现在真的会成交**：以前只记一笔
   call、不改持仓，dry-run 追单会出现「账本加了、持仓没变」。成交价用
   最近一次标记价（`last_price`，由 `match_orders()` 每轮更新），
   拿不到价格就不改持仓，避免把成本算成 0。
3. **界面**：未成交挂单页并排「撤销选中挂单 / 市价追单」，说明文案改为
   「改『更优幅度』不会改已挂的单；追单=不等了，按现价成交」。
   跟单盈亏页删掉「平 1/2」，保留「平选中 / 一键全平 / 部分平仓…」。
4. **顺手修**：持仓表与平仓明细表的「方向」列 36→44px（表头两个字被截）；
   对账页「账本 vs 持仓」差异提示只在实盘口径且该品种无挂单时出现
   （模拟资金口径下两边目标量本来就不同，且未成交挂单会让账本天然偏高）。

文件：`copy_engine.py`、`gui.py`、`pnl_tracker.py`

验证：
- `py -3.12 copy_engine.py --selftest` → **38/38**（新增：市价追单后
  持仓=账本=目标量，且 rebalance 不再补）
- `py -3.12 gui.py --selftest` → **52 asserts**
- `py -3.12 run_all.py` → **10/10 PASS**
- 离屏渲染：挂单页两个按钮并行不重叠；持仓页「平 1/2」已消失

**清尾巴：对账口径改按账本、列宽固定、杠杆校验、换人清孤儿单**

触发：上一轮遗留清单。

1. **「待补差额」改按已跟账本算**（`filled`），不再拿真实持仓比。
   原来手动平仓后会长期显示「应开未开」，与引擎「故意不补」相反。
   现在：`差额 =（应开数量 − 已跟数量）× 应开均价`，正数=还该补。
   真实持仓与账本不一致时，摘要里直接列出来（`真实持仓与已跟账本不一致：
   SPCXUSDT +5（手动平仓/加仓，不会被自动补回）`）。
2. **对账表 10 列 → 9 列**：去掉「真实名义」（与已跟数量/摘要里的总敞口重复）。
   800px 里塞 10 列必然把 9 位数字挤成「…」。列宽固定 800、视口 802，
   **不再做自适应压缩** —— 压缩会把表头挤坏，按内容重排又会让「重算」时横向抖。
3. **杠杆输入加校验**：非数字 / 越界（<1 或 >125）直接报错并拒绝保存，
   不再原样落盘、等下次启动才静默回落。
4. **换人后清理孤儿单**：`_restore` 检测到换人时置 `_purge_orphans`，
   下一轮 `rebuild_from_exchange()` 把所有 `copy_` 前缀的旧挂单撤掉且不认回。
   不撤的话，`historical` 单永不超时，几小时后成交就是一笔账本为 0 的
   无人管理仓位。
5. **dry-run 明确提示**：手动平仓/撤单在模拟盘下只作用于本地撮合，
   按钮旁文案会跟着执行模式变。

文件：`gui.py`、`copy_engine.py`

验证：
- `py -3.12 run_all.py` → **10/10 PASS**
- `py -3.12 copy_engine.py --selftest` → **37/37**
- `py -3.12 gui.py --selftest` → **52 asserts**
- 离屏渲染（加载 msyh.ttc 中文字体）：对账表 9 列表头与数值全部完整、
  无省略号、无横向滚动条；挂单页「撤销选中挂单」按钮在位
- 重启实例后 `logs/gui.log`：`轮询已启动` + `电源方案里睡眠已是「从不」，
  不申请唤醒请求` + `执行模式切换：本地模拟（不下单）`

遗留：
- dry-run 下「仓位对账」的模拟持仓来自 `SimPortfolio` 离线重放，
  与 `MemoryBroker` 的撮合结果可能不同（一个是投影、一个是撮合），
  属两套模型，已在页签标题区分。

**手动干预：撤挂单 / 平选中 / 平一半 / 一键全平 / 自定义数量**

触发：用户指出「改了更优幅度，已经挂出去的单不会自动改价」，要求能在
「未成交挂单」里撤单，并在持仓处提供平仓入口（含自定义平多少）。

1. **引擎新增两个手动入口**（`copy_engine.py`）：
   - `cancel_entry(symbol)`：撤入场挂单。撤完把该笔从「已跟账本」里扣回，
     再用真实持仓抬底（可能已部分成交）—— 不扣的话账本永远偏高、后续少开。
   - `close_manual(symbol, ratio, qty)`：按比例或指定数量平仓。
     **刻意不缩「已跟账本」**：用户自己平掉的不该被 `rebalance()` 补回来，
     这是「按事件跟单」的核心 —— 他下一笔新开仓照常按那笔的份额跟。
2. **界面**：未成交挂单页加「撤销选中挂单」；跟单盈亏页持仓表下加
   「平选中 / 平 1/2 / 一键全平 / 自定义…」。实盘模式弹确认框，
   dry-run 与测试网直接执行；网络请求走后台线程，界面只收日志。
3. **动作流水认得新类型**：`MANUAL_CLOSE` → `CLOSE`、`ENTRY_CANCELLED` →
   `CANCEL`，盈亏账本不会漏记手动操作。

文件：`copy_engine.py`、`gui.py`、`pnl_tracker.py`

验证：
- `py -3.12 copy_engine.py --selftest` → **37/37**（新增 3 项：手动撤单账本
  归零、手动全平后 rebalance 不补回、手动平一半账本不变）
- `py -3.12 gui.py --selftest` → **52 asserts**（新增按钮存在性 + 持仓表
  没有把按钮挤出页面）
- `py -3.12 run_all.py` → **10/10 PASS**
- 离屏渲染 1180×760：按钮行不挤、明细表完整可见、无省略号

遗留：
- 撤单/平仓按钮在 dry-run 下也作用于 `MemoryBroker`（模拟撮合），
  不会碰到交易所，但界面上没有额外的「这是模拟」提示。
- 手动平仓后「仓位对账」页的「差额」列仍按「累计目标 vs 真实持仓」算，
  会显示成「应开未开」，与引擎「故意不补」的语义相反。

**按事件记账（平掉的不再补）、布局按「先看结果」重排、配色与杠杆可配**

触发：用户指出「平仓不是看他现在持多少，是按他开单那一刻的比例」，
并要求删掉没人用的按钮、把盈亏页提到最前、API Key 并入运行配置、
涨跌配色改币安习惯（多绿空红）、切模式后文案必须立刻跟着变。

1. **引擎改成按事件记账**：新增 `self.filled`（按事件累计已安排量）。
   `_open` 的差额 = `目标累计 - filled`，**不再减真实持仓**；
   `_close` 按带单员平仓比例缩 `filled`；这样用户手动平掉的仓位不会被
   `rebalance()` 补回来，而带单员再开一笔时只按该笔事件的份额开。
   自检新增 T8 锁死这条语义（首份 0.21 → 手动平到 0 → 他再开只补 0.284，
   不是累计目标 0.494）。
2. **tab 精简到 6 个并重排**：跟单盈亏 → 操作播报 → 成交事件 → 未成交挂单
   → 事件日志 → 运行配置。删掉「账号连接」独立 tab（凭证本来就是配置）；
   「仓位管理」并入跟单盈亏的第二子页签「仓位对账」。
3. **删掉冗余按钮**：左侧「单次运行」「清空日志」删除（轮询才是正常用法；
   清日志在日志页自己有按钮）。
4. **涨跌配色可配**：`color_scheme` = `binance`（默认，涨绿跌红）/
   `cn`（涨红跌绿）。`ThemeManager._palette()` 在返回副本时对调 POS/NEG，
   代码里所有判断写的是 POS/NEG，不感知这个开关。
5. **杠杆可配**：`leverage`（1-125，默认 20）。只影响保证金占用，不改变
   仓位大小。`set_leverage` 失败会明确告警（品种最大杠杆限制，-4028）。
6. **切模式立即刷新**：`_on_mode_change()` 里直接 `_refresh_sim()`，
   不再等下一轮快照；`_save_config()` 保存后立刻应用配色 + 刷新杠杆。
7. **盈亏页显示带单员金额**：指标行加第 6 格「带单员钱包 / 浮盈亏」，
   tooltip 里给总敞口与杠杆倍数。

文件：`copy_engine.py`、`gui.py`、`theme_qss.py`

验证：
- `py -3.12 copy_engine.py --selftest` → **34/34**（新增 T8）
- `py -3.12 gui.py --selftest` → **50 asserts**（新增：tab 顺序、配色真的
  对调 POS/NEG、杠杆输入进配置）
- `py -3.12 run_all.py` → **10/10 PASS**
- 离屏渲染 1180×760：6 个指标一行不溢出、曲线与两张表完整可见

遗留（对抗性审查发现，未全部处理）：
- **账本与真实持仓少两条对齐路径**：委托被外部撤掉、或部分成交后余量消失时，
  `filled` 会偏高 → 后续少开。刻意选保守方向（宁可少开不多开），
  因为「持仓库比账本少」与「手动平仓」在外部无法区分。
- `reconcile()` 里 `cancel_order` 仍可能抛异常吞掉整批已标 seen 的事件
  （读持仓已加 try）。
- `begin_following` 换人会撤旧单，但重启换人（`_restore`）不撤。
- 仓位对账页的「差额」仍按「累计目标 vs 真实持仓」算，与引擎新语义不一致，
  手动平仓后会长期显示「应开未开」——需要改成按 `filled` 口径。
- `leverage` 输入无校验：填 `abc` 会原样落盘，下次启动才静默回落。
- 最小窗口（980px）下「仓位对账」10 列溢出，且横向滚动条被关掉。

**规范化改名 + 清理回声 + 修账本/撮合/切换模式的硬伤 + 发布前脱敏**

触发：用户一轮集中反馈 —— 命名不规范（`gui_qt.py`、`binance-lead-copy`）、
之前删掉的东西还留着回声、播报里塞了拿不到的字段、模拟仓「价格没到也成交」、
切 live 后模拟文案没变、以及发布 GitHub 前必须清掉个人数据。

1. **改名**：`gui_qt.py` → `gui.py`（不再有 `_qt` 后缀）；产品名统一为
   「币安带单跟单」（窗口标题、启动快捷方式、电源请求原因字符串）。
   读代码的人不会再看到「外场跟单」这种内部叫法。
2. **清回声**：`_refs/archive-tkinter/`（1.6MB 已退役的 tkinter 版）
   移出仓库；`lead_provider.py`（零 import）删除；模块 docstring 里对
   `gui.py`（tkinter 版）/`theme.py` 的引用、`R_CARD/R_TABLE/R_BTN/R_INPUT`
   死常量、注释里三处 tkinter 对比全部清掉。
3. **播报不再写拿不到的字段**：删掉「当前操作杠杆 / 当前持仓保证金 /
   当前持仓杠杆：不可用（接口未提供）」三行。播报是照着操作的东西，
   塞「不可用」只是噪音。
4. **模拟仓改成真实撮合语义**（这是本轮最严重的一处）：`MemoryBroker`
   新增 `auto_fill` 参数，dry-run 用 `auto_fill=False` —— 限价单先挂进
   挂单簿，每轮用标记价撮合：**买单要 `mark <= 限价`、卖单要 `mark >= 限价`
   才成交**。之前 `unfilled=False` 是一挂就按限价成交，「更优价」根本没等
   价格回来，等于假装稳赚。自检里新增 2 项锁住这个语义。
5. **切模式重建客户端**：新增 `_switch_broker()`。`base_url` 在
   `BinanceService` 构造时定死，原来按 `isinstance` 复用对象，会出现
   「下拉框写 live、实际还在打 testnet」；现在换模式/换 Key 一律重建，
   并且**先把旧账户上的挂单撤掉**再切，避免留下没人管理的幽灵单。
6. **平仓账本修好**：`CLOSE_FOLLOW` 动作补 `side` 与**实际平仓数量**。
   原来整仓平的 `close_qty` 是 `None`（API 参数语义），账本按 0 记，
   已实现盈亏永远长不大；`side` 缺失让 `_sign("")` 恒为 -1，平多盈利
   记成亏损。`pnl_tracker` 改成按 `(品种, 方向)` 取开仓价，方向缺失
   就不算盈亏，而不是猜。
7. **动作流不再断供**：`self.actions` 是 `deque(maxlen=500)`，装满后
   `len()` 恒等于 500，`list(self.actions)[start:]` 永久返回空 ——
   动作日志与盈亏账本一起哑掉。改成单调序号（`_record()` / `_actions_since()`），
   自检里加了一项「装满 500 条后仍能取到本轮动作」。
8. **切 live 后文案跟着变**：`_refresh_sim()` 的口径判断从「是否点了按实盘
   权益」改成「模式不是 dry-run **或** 点了按实盘权益」。
9. **发布前脱敏**：真实带单员 ID（6 处源码 + 6 处文档）→ 占位值
   `1234567890123456789`；带单余额 → `100000.0`；`run_all.py` 里的
   `C://Users//Administrator//...` → `sys.executable`；`net.py` 自检不再写死
   代理端口。`api_feed.py` 遇到占位 ID 返回 2（SKIP）而不是报 404。
10. **建立版本库**：`git init` + `.gitignore`（`.env` / `gui-config.json` /
    `state.db*` / `logs/` / `_refs/` 全部排除），首次提交前已核对
    `git ls-files` 不含任何凭证。新增 `README.md` / `LICENSE` / `THIRD-PARTY.md`。

文件：`gui.py`（原 `gui_qt.py`）、`copy_engine.py`、`pnl_tracker.py`、
`state_store.py`、`feed.py`、`api_feed.py`、`net.py`、`main.py`、`run_all.py`、
`theme_qss.py`、`README.md`（新）、`LICENSE`（新）、`THIRD-PARTY.md`（新）、
`.gitignore`（新）

验证：
- `py -3.12 run_all.py` → **10/10 PASS**（`api_feed` 在本机有 `gui-config.json`
  时仍测真实接口；空环境会标 SKIP）
- `py -3.12 copy_engine.py --selftest` → **33/33**（新增撮合 2 项 + 动作流 1 项）
- `py -3.12 gui.py --selftest` → **48 asserts**
- 敏感串复扫：`git ls-files | xargs grep -E "5075...|5283...|C://Users//Administrator"`
  → **零命中**

遗留（按危害排序，未处理）：
- **样例流水是他人真实交易记录**：`feed.py` 的 `SAMPLE` / `WEB_SAMPLE` 是
  带单员的真实成交（含 44,977 USDT 已实现盈亏）。需要改成虚构数值，但这会
  牵动 `gui.py --selftest` 里 8 处硬编码断言（时间戳、首行、播报文本），
  属一次独立改动。
- **`_open()` 读持仓失败时 `have=0`**：会按累计目标重挂一整套，单次故障即成倍
  放大敞口。`_close` / `rebalance` 的同类失败都选了保守路线，这里不一致。
- **`match_orders` 用标记价**：真交易所是盘口撮合（对手价），标记价含基差，
  偏差有方向性。dry-run 的成交率不能外推到实盘。
- **dry-run 无挂单恢复**：`MemoryBroker` 全内存，重启后挂单与均价丢失。
- **仓位管理 + 跟单盈亏合并 / 快捷平仓**：用户已提出，但「手动平仓后如何抑制
  `rebalance()` 补回」需要先定语义（带单员下次加仓跟不跟），见待决策 #15。

**跟单盈亏入库 + 开仓份额口径统一 + 电源请求不再申请不需要的东西**

触发：用户三点诉求 —— ① 浮盈浮亏要显示并落库，好复盘跟单数据；
② 切真实跟单后这些数据怎么处理、仓位管理要斟酌；
③ 屏幕还是不熄（挂一夜亮着没法睡）。

1. **新增 `pnl_tracker.py`（纯函数）与 `state.db` 三张表**：
   `pnl_snapshot`（每轮一行：权益/可用/浮盈/敞口/保证金/持仓数/带单员侧）、
   `pnl_position`（每轮每品种：数量/开仓价/标记价/浮盈/名义）、
   `pnl_trade`（我方动作流水：开/平/撤 + 这一笔的实现盈亏）。
   三张表都有 `mode` 列，`dry-run` / `testnet` / `live` **分开存**。
2. **三条口径写死，不许混用**：钱包余额（`totalWalletBalance`，已实现已在里面）
   / 浮动盈亏（优先交易所 `unRealizedProfit`）/ 净值 = 余额 + 浮盈。
   曲线画净值：只画余额的话，一次平仓会让曲线凭空下蹲，看着像亏了。
   **已实现盈亏逐笔累加，不做钱包差分** —— 差分会把用户手动划转算成盈利。
   平仓价拿不到交易所成交均价（`place_market_order` 只回 order_id/symbol/
   side/quantity/status），记的是平仓那一刻的标记价，属近似值，UI 里标明。
3. **新增「跟单盈亏」tab（第 4 个）**：5 个指标卡（净值/浮动/已实现/累计/
   最大回撤）+ QPainter 自绘净值曲线 + 持仓浮盈表 + 平仓明细表。
   不引第三方绘图库，requirements 仍然只有 requests + PySide6。
4. **快照节流 30 秒、动作流水一条不漏**：10 秒一条一天 8000 多行，而这一页
   是看趋势的。`prune_pnl()` 每天清 90 天前的快照与持仓明细（动作流水保留）。
5. **`_open()` 改用 `target_position()`**：此前 `_open()` 自己按
   `floor(my_notional / limit / step)` 算了一遍名义口径，而仓位管理页、
   成交事件表的「应开」走的是数量口径 —— 界面上显示的数量不是实盘会下的数量。
   实测同一组数据相差 0.7%（12.416 → 12.458 张）。改完全项目只剩一份份额算法。
6. **`MemoryBroker` 记开仓均价**：`self.entry` 按数量加权维护，反手时以本次
   成交价为新成本；`get_positions()` 的 `unrealized_pnl` 返回 `None` 而不是
   `0.0` —— 写 0 会被当成「真的不赚不亏」，模拟盘浮盈永远算不出来。
7. **电源请求再收一层**：`set_keep_awake()` 增加 `sleep_already_disabled()`
   前置判断 —— `STANDBYIDLE` 交流/直流都是 0（电源方案里睡眠已是「从不」）时
   **一条请求都不申请**，从根上排除「连带顶住显示器」的可能。
8. **清理死代码**：`App.val` / `App.apply_config` / `Lot.avg_price` /
   `sim_portfolio._fmt` / `account.py` 的 `import sys` / `net.py` 重复的
   `NET_ERRORS`。`lead_provider.py` 整模块无人 import，但项目没有版本库，
   删了不可恢复，先留着并在「待决策清单」登记。

文件：`pnl_tracker.py`（新增）、`state_store.py`、`gui.py`、`copy_engine.py`、
`position_ledger.py`、`sim_portfolio.py`、`account.py`、`net.py`、`run_all.py`、
`MAINTENANCE.md`、`CHANGELOG.md`

验证：
- `py -3.12 run_all.py` → **10/10 PASS**（新增 `pnl_tracker.py --selftest`）
- `py -3.12 gui.py --selftest` → **48 asserts**（新增 4 条：盈亏页列数、
  净值 = 余额 + 浮盈 = 105.00、累计 = 区间 + 已实现 = +10.00、曲线点数/
  两张表的行数真的画上去了）
- `py -3.12 pnl_tracker.py --selftest` → 5/5（含「交易所浮盈优先」「空头数量
  取绝对值」「只撤单不算实现盈亏」「最大回撤按当时的峰值算」）
- `py -3.12 state_store.py --selftest` → 老库 `CREATE TABLE IF NOT EXISTS`
  升级通过；同 `mode` 不同 `ts` 曲线按时间升序；动作流水重复投递被挡掉
- 真实 `state.db` 副本上跑迁移 + 写入 + 回读：`tables` 出现三张 `pnl_*`，
  `load_pnl_curve("live")` 返回 `123.45`，未改动线上库
- 离屏渲染「跟单盈亏」页并读几何：page 623px，内容底 613px，无横向滚动条、
  无溢出（并排两表会出横滚、列被挤成「…」，已改成上下排）

遗留：
- **熄屏仍未取得实测证明**：线上那个实例还是旧代码（`ES_CONTINUOUS |
  ES_SYSTEM_REQUIRED`），必须重启才会走新的 Power Request 路径；而且本机
  `STANDBYIDLE` 交流/直流本来就是 0，重启后应该连请求都不申请。
  验证方法：管理员命令行 `powercfg -requests`，SYSTEM 段可有本进程、
  **DISPLAY 段必须为空**。
- 已实现盈亏是「按平仓时刻标记价估算」，不是交易所成交均价：误差来自滑点。
  要精确得上 `/fapi/v1/userTrades` 或 `income`，但那要额外一次签名请求。
- 手续费/资金费没有单独记账：浮盈取的是交易所的 `unRealizedProfit`（含资金费），
  已实现里不含手续费明细。
- `lead_provider.py` 整模块无人 import，删不删待定；`api_feed.py` 的
  `snapshot_fills` / `trade_history` / `position_history` / `fills_from`
  也无人调用，但 `_refs/_diag_live.py` 还在用 `trade_history`，留着当接口文档。

**「阻止睡眠」改为 Power Request，开机自启加自检与 pythonw**

触发：用户反馈两点 —— 开机自启没用；「息屏保持」实际是把屏幕顶住不熄，
挂一夜没法睡觉，而需求恰恰相反（息屏但保持活跃）。

1. **电源请求换 API**：`set_keep_awake()` 从
   `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` 改为
   `PowerCreateRequest` + `PowerSetRequest(PowerRequestSystemRequired)`。
   Windows 把「系统唤醒」与「显示器常亮」拆成两个独立请求，只申请前者，
   显示器按电源方案正常熄灭。旧写法文档上同样声称不管显示器，但实测在部分
   机器上会连带顶住屏幕，且是线程级状态、跟着线程生死。建不出句柄时仍退回
   旧 API，同样不加 `ES_DISPLAY_REQUIRED`。句柄与 `REASON_CONTEXT`
   （含原因字符串）挂在 `_power_req` 里一直持有，`stop_polling()` /
   `closeEvent` 释放。
2. **开机自启自检**：新增 `ensure_autostart()`，`App.__init__` 在
   `autostart=True` 且非离屏实例时调一次。原来只在配置值变化时才写快捷方式，
   快捷方式被删 / 被安全软件清掉 / 指到别的解释器后，界面照样显示「已勾选」。
   现在每次启动核对一次，异常就重建并写日志。
3. **目标改用 `pythonw.exe`**（`_pythonw()`）：`python.exe` 登录时会先弹一个
   黑框（`WindowStyle=7` 只是最小化），而那个控制台是父进程 —— 用户关掉黑框
   会把跟单一起带走。
4. **清理历史残留**：`_lnk_mentions()` 直接扫 .lnk 字节（ASCII + UTF-16LE），
   找出启动文件夹里所有提到本项目目录名的快捷方式，非规范名的删掉。
   实测清掉了 2026-11-14 11:36 那个编码坏掉的 `澶栧満璺熷崟.lnk`
   （指向 `py.exe … --autostart`，登录时同样会拉起一个实例，只是被单实例锁
   挡回去）。

文件：`gui.py`、`MAINTENANCE.md`、`CHANGELOG.md`

验证：
- `py -3.12 gui.py --selftest` → **44 asserts**（无回归）
- `py -3.12 run_all.py` → 9/9 PASS
- 单独调用 `set_keep_awake(True/False)` → `on` / `off`，句柄创建与释放均成功
- 单独调用 `ensure_autostart()` → `已重建，清理残留 1 个`；回读 `外场跟单.lnk`
  字节：含 `pythonw.exe`（ASCII+UTF-16LE）、含 `gui.py`、含
  `binance-lead-copy`，**不含** `python.exe`；启动文件夹只剩规范名的 .lnk
- `pythonw.exe -c "import PySide6"` → exit 0

遗留：
- **自启从未被真实触发过**：`Win32_OperatingSystem.LastBootUpTime` =
  2026-11-09 12:23:48，快捷方式创建于 2026-11-14 11:58，日志自 2026-11-14
  11:48 起连续无中断 —— 期间没有重启也没有注销，所以「自启没用」这个结论
  当时无法被观测证实。下一次重启/登录后看 `logs/gui.log` 是否出现
  「就绪」+「轮询已启动」，才算验证完成。
- 本机 `powercfg -query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE` 交流/直流都是
  `0x00000000`（从不睡眠），所以 `keep_awake` 在当前电源方案下是冗余的；
  它的价值是在睡眠超时被改回非零的机器上兜底。显示器超时 AC=900s / DC=300s。
- `powercfg -requests` 需管理员权限，本次未取得 DISPLAY 段的实测空证明。

**修「点重算布局跳」与「切实盘后又变回模拟」**

触发：用户反馈点「重算」/「按实盘权益」布局会变；切成实盘口径后又变回模拟。

1. **口径改为锁定跟随**：原来靠「模拟资金与权益相差 <1%」判断，权益每轮
   都在动（浮盈），浮动超过 1% 就切回「模拟资金」—— 看起来像按钮失效。
   现在点「按实盘权益」置 `_sim_pin_real = True`，之后每轮直接取最新权益；
   手动编辑输入框（`textEdited`）才解锁回模拟口径。
2. **布局不再跳动**：
   - 摘要与说明设固定高度（3 行）—— 文案在「模拟/实盘」之间长度不同，
     按内容撑高会把下面整张表顶下去；
   - 输入框左侧标签固定宽度 78px —— 「模拟资金」4 字 /「本程序权益」5 字，
     宽度一变输入框就位移；
   - 列宽改为固定基线（104/46/100/82/78/78/78/80/82/72），只在视口宽度
     变化超过 40px 时按比例压缩数字列（品种与方向不压）；
     不再用 `resizeColumnsToContents` —— 数字位数每轮都变，列宽跟着抖。
   - 关掉该表的横向滚动条：出条会改变视口宽度，形成「压缩→出条→再压缩」的循环。
3. 列宽按中文表头重新标定：「方向」34px 会把表头截成「方」，
   「NVDAUSDT」在 92px 里会截成「NVDAUS…」。

文件：`gui.py`、`MAINTENANCE.md`、`CHANGELOG.md`

验证：
- `py -3.12 gui.py --selftest` → **44 asserts**（新增 2 条：权益波动 5% 后
  口径仍是「实盘口径」、手动改资金后必须切回「模拟资金」）
- 实测同一窗口内连续点「重算」两次：表格 top/高度、摘要高度、标签宽度、
  输入框 x 坐标、10 列列宽全部一致
- `run_all.py` 9/9 PASS；引擎 30/30

**开仓份额改为数量等比 + 口径文案跟随 + 精简冗余说明**

触发：用户三点意见 —— 点「按实盘权益」后仍提示「模拟仓」；界面有多余说明
（不需要登录 / 浏览器 / Cookie）；开仓份额应按合约标的本位算，而不是拿 USDT
名义折算。

1. **份额改成数量等比**（`target_position()`）：
   `target_qty = 带单员数量 × (本程序权益 ÷ 带单员钱包)`，按 step 向下取整；
   `target_notional = target_qty × 限价`，只用于展示与保证金估算。
   原来的名义口径是「先算 USDT 敞口，再除以限价」，限价与带单员成交价不一致时
   会多算或少算一份数量 —— 那不是「份额一致」。**价格相同时两种口径结果完全
   相同**（用户直觉正确），差异只出现在限价与带单员均价不同的时候。
   实测同一组数据（权益 92.63 / 带单员钱包 332153.76）：NVDA 名义口径 0.904 张、
   数量口径 0.894 张，差的就是限价与均价的偏离。
   成交事件表的「应开 USDT」也改走同一个 `target_position()`，不再自己算一遍。
2. **口径文案跟随**：`模拟资金 == 本程序权益`（±1%）时，标题写
   「仓位对账 · 实盘口径」、输入框标签写「本程序权益」、摘要写「本程序权益 /
   应开总名义」，全文不出现「模拟」。点「按实盘权益」立刻切过去 ——
   原来资金对齐了文案还是「模拟资金 92.63 U」，看起来像按钮没反应。
3. **精简冗余说明**：「跟单目标」卡片去掉「不需要登录、不需要开浏览器、
   不需要 Cookie」；更优价 / 滑点 / 执行模式三处说明压到一句；
   `dry-run` 说明改为「本地模拟，不下单。」

文件：`copy_engine.py`、`gui.py`、`MAINTENANCE.md`、`CHANGELOG.md`

验证：
- `py -3.12 gui.py --selftest` → **42 asserts**（新增 3 条：按实盘权益后标题
  必须含「实盘口径」、摘要必须含「本程序权益」且不含「模拟」）
- `py -3.12 sim_portfolio.py --equity 92.63` → NVDA 0.894 张 / 145.06 U、
  ZEC 0.047 张 / 41.74 U（数量 = 3206.99 × 0.000279）
- `run_all.py` 9/9 PASS；引擎 30/30

**文案正式化 + 带单员标识改为「确认后显示」+ 仓位页并排对比**

触发：用户三点意见 —— 文案要正式、不要口语化的「他」；左上角带单员标识
不该靠配置缓存；仓位页要把带单员与本程序的占比并排对比。

1. **文案统一**：界面与日志里的「他」全部改为「带单员」，「我方」改为「本程序」，
   播报字段冒号统一为全角。涉及指标卡、仓位页表头与说明、挂单摘要、播报、
   动作日志、引擎日志（仅撤单 / 对账补差 / 超时撤单提示）。
2. **带单员标识只在确认后显示**：`_lead_display()` 不再读 `lead_nickname` 缓存，
   只认本次会话里接口真实返回过的昵称（`_lead_verified`）。未确认写「未连接」，
   确认后写「昵称 · portfolioId 尾 6 位」。换人立即作废回「未连接」。
   `_ensure_lead_name()` 在未确认时每 30 秒补拉一次 —— 开机那一刻本地代理
   常常还没就绪，首次必然失败，不重试就永远停在「未连接」。
   离屏实例（`enable_store=False`，自检与截图用）不发起任何网络请求。
3. **仓位页并排对比**：新增「本程序占比」列（真实名义 ÷ 本程序权益），与
   「带单员占比」（带单员名义 ÷ 带单员钱包）并排，可直接比杠杆水平。
   摘要行加「总敞口 带单员 X.XX× / 本程序 Y.YY×」。
   行按偏差绝对值降序 —— 这一页是排查用的，问题最大的先出现（孤儿仓自然置顶）。
   10 列放不下时按比例压缩列宽，而不是甩出横向滚动条。
4. **修播报滚动位置**：插入游标原来被设为「可见游标」，停在文末会让
   QTextEdit 自动滚到底部，把刚画好的最新一条顶出视野。改用文档游标插入，
   可见游标固定在文首。

文件：`gui.py`、`copy_engine.py`、`sim_portfolio.py`、`MAINTENANCE.md`、
`CHANGELOG.md`

验证：
- `py -3.12 gui.py --selftest` → **39 asserts**（新增 2 条：未确认时标题必须是
  「未连接」、确认后必须是「昵称 · 尾号」；仓位表列数 9 → 10）
- 抓图：仓位页 10 列无截断、两列占比并排；播报页首条为最新且滚动在顶部
- `run_all.py` 9/9 PASS；引擎 30/30

**新增「仓位管理」页：用模拟资金核对 应开 / 实开**

触发：用户要求「接一个仓位管理界面，排查应开实开是否正确，模拟仓默认 1000 U」。

问题背景：成交事件表的「应开 / 实开」两列在实盘上没法验证 —— 想验证就得真下单，
或者把资金调大，两种都有代价。而且那两列只在**成交那一刻**有意义，事后看
分不清「算错了」和「当时没跟上」。

改动：
1. 新增 `sim_portfolio.py`：`SimPortfolio.replay()` 拿一笔假资金把带单员的成交
   按时间重放，逐笔算目标、补差额、按比例平仓，输出每个品种的
   他名义 / 占比 / 应开名义 / 应开数量 / 模拟持仓。**无状态**：每次从空仓开始，
   结果只取决于输入 —— 有状态就会和实盘一样攒历史误差，排查时分不清是数据错
   还是账本错。
2. 抽出 `copy_engine.target_position()`：把「他的仓位 → 我该持多少」变成纯函数，
   `rebalance()` 和模拟仓调同一个。两边各写一套的话，排查时看到的数和实际下单
   的数可能不是一回事，等于白排查。
3. 新增「仓位管理」页签（第 3 个）：模拟资金输入框（默认 `1000`）、重算、清空、
   **按实盘权益**；表格 9 列，列宽按内容自适应；差额 =（模拟数量 − 真实数量）×
   应开均价，用警示色，正数=该开没开，负数=多开（孤儿仓）。
4. 「按实盘权益」按钮：资金不一样时差额没有可比性（1000 U 的模拟仓对 92 U 的
   实盘，差额永远是正的，那是资金差不是漏单）。点一下把基数对齐，摘要里也会
   明确标注两边差多少。
5. 孤儿仓单独成行：我方持有、他账本里没有的品种也列出来，差额全负 ——
   这类仓位没有对应信号，永远不会被平仓，是超配的主因。
6. 持仓只读一次：`_run_engine()` 在引擎线程取 `get_positions()`，同时喂给
   「实开」列和仓位管理页。界面线程再调一次就是一次签名 REST，会把界面卡住。
7. 该页不用 `scroll_host()`：外面再套一层滚动区，表格拿到的是滚动区内容宽，
   比可见宽度窄一截，列会被挤成省略号。和「成交事件」页一样直接铺。

文件：`sim_portfolio.py`（新增）、`copy_engine.py`、`gui.py`、
`MAINTENANCE.md`、`CHANGELOG.md`

验证（真实数据，只读取数，不下单）：
- `py -3.12 sim_portfolio.py --equity 1000` → 他钱包 332019.96、缩放 0.003012，
  NVDA 应开 9.688 张 / 1572.00 U，ZEC 0.509 张 / 452.27 U
- GUI 抓图（`_refs/_sim-panel.png` / `_sim-panel-real.png`）：
  1000 U 时 NVDA 10.855 张；按实盘权益 92.63 对齐后 NVDA 1.002 张 / 162.70 U、
  ZEC 0.052 张 / 46.81 U、SOXL 孤儿仓 −799.42 U
- `py -3.12 gui.py --selftest` → **37 asserts**（新增 4 条：9 列、默认 1000、
  应开数量非零、摘要含缩放系数）
- `run_all.py` 9/9 PASS；引擎 30/30

**事件日志的动作行改成中文 + 去掉重复级别前缀**

触发：用户贴图指出事件日志里 ACTION 行是
`{"type": "CLOSE_FOLLOW", "symbol": "SPCXUSDT", "cancelled_pending": false, ...}`
这种被列宽截断的原始 JSON，读不懂也读不全。

1. **新增 `format_action()`**：按动作类型翻成人话 ——
   `开仓跟随：挂单 ZECUSDT 空 1.76 张 @ 1010.00638 · 他的名义 49988.77 U ·
   占他总仓位 17.78% · 20x`、
   `平仓跟随 ETHUSDT：我方无仓位，仅撤单（撤挂单=否）`、
   `挂单结果 X：已成交 / 未成交（已撤单）`、`入场挂单 X 超时撤单 —— 这一单算漏了`、
   `挂单对账：接管 N 笔本程序遗留挂单（…）`、
   `带单员资金变动 +X USDT（钱包 a → b，空仓精确 / 持仓推算）`。
   布尔值一律写成「是 / 否」，不再出现 `False`。
   原始 dict 仍完整保留在 `engine.actions` 里，程序化处理取那个。
2. **界面日志去掉重复的级别前缀**：`QueueLogHandler` 的 formatter 从
   `"%(levelname)s %(message)s"` 改成 `"%(message)s"` —— 级别在表格里是
   独立一列，正文再带一次 `INFO ` 是重复。落盘日志（`logs/gui.log`）保持
   带级别，因为文件里没有列。
3. **降级三条与 ACTION 行重复的引擎日志**：`开仓跟随 … qty=… limit=…`、
   `平仓跟随 …`、`仅撤单 …` 从 INFO 改 DEBUG（只在文件里留底）。
   界面上原来同一条操作会出现两遍，一遍是原始参数、一遍是结构化动作。
4. **日志单元格加 tooltip**：`_mk_item()` 新增 `tip` 参数，事件日志三列都带全文
   —— 动作行比列宽长，原来只能看到「20…」。

文件：`gui.py`、`copy_engine.py`、`MAINTENANCE.md`、`CHANGELOG.md`

验证：
- `py -3.12 gui.py --selftest` → **33 asserts**（新增 1 条：ACTION 行不许以 `{` 开头）
- 抓图 `_refs/_log-readable.png`：ACTION 行全部中文，无 `INFO` 前缀重复
- `run_all.py` 9/9 PASS；引擎 30/30

**流水顺序改为最新在上 + 提示音换实现**

触发：用户指出「成交事件、操作播报应该是最新的在上面」以及「交易员操作没有发出声音」。

1. **成交事件倒序**：`_fills_table_rows()` 按 `Fill.ts`（完整时间戳）倒序，
   不按展示用的 `11-13 10:19:42` —— 后者去年份，跨年会把 `12-31` 排到
   `01-01` 前面。原来的顺序取决于上游 `fills()` 的返回顺序，实测
   `parse(WEB_SAMPLE)` 本身就是倒序，所以「反转一下」这种写法会翻错方向，
   必须显式排序。
2. **操作播报倒序**：改成按「组」存储（`_announce_groups`），渲染时组倒序，
   组内也按时间倒序。原来 `_announce_blocks` 是一条扁平列表、增量追加到尾部，
   结构上只能往末尾长，倒序渲染会打乱组内顺序（标题和明细被拆开）。
   代价是每来一条整份重画；600 块上限下开销可忽略。
   滚动位置按新增高度补偿（`bar.maximum()` 差），看旧内容时不会被顶走。
3. **提示音换实现**：`PlaySound("SystemExclamation", SND_ALIAS)` 读的是
   Windows 声音方案，方案设成「无声」时完全不响且无法感知失败。
   改用 `winsound.Beep`（880 Hz 110 ms + 1320 Hz 130 ms），与声音方案无关；
   它阻塞调用线程（实测 259 ms），所以放后台 daemon 线程。
   同时去掉 `mode != "dry-run"` 的门禁 —— dry-run 也是真实数据，同样要看。
   播报页头加「试听」按钮，用户能当场确认音频通不通。

文件：`gui.py`、`MAINTENANCE.md`、`CHANGELOG.md`

验证：
- `py -3.12 gui.py --selftest` → **32 asserts**（新增 2 条：成交表第一行
  时间必须最大、播报第一段时间必须最大）
- 抓图 `_refs/_order-fills.png` / `_refs/_order-announce.png`：
  成交表首行 `11-13 10:19:42`、末行 `11-12 00:42:31`；播报首条
  `ETHUSDT · 平空 / 时间: 2026-11-13 10:19:42`，往下递减
- `winsound.Beep` 实测正常返回，耗时 259 ms
- `run_all.py` 9/9 PASS；引擎 30/30

**对账补差：漏单自愈**

触发：用户选「加启动时按账本对账补差」而不是手动补这一笔。

背景：引擎只在「新 OPEN 事件」时下单。一条事件被漏掉（超龄吞掉、下单被拒、
程序没跑、数据源截断），账本是对的但仓位永久偏小，而且不会有任何后续事件来纠正。
NVDA 那笔就是典型：账本 3206.99 张，我方只有 0.15 张，差 6 倍。

改动：
1. `CopyEngine.rebalance()`：每轮（`process()` 的两个返回点前）拿
   `_replay.totals` 的累计名义按等比算目标数量，和我方真实持仓比，**只补不减**。
   三条防线：
   - 该品种已有挂单就跳过，不重复下单；
   - 差额名义不到 `max(5 USDT, 目标×5%)` 就不动，避免 wallet 推算抖动引起反复调仓；
   - 我方方向和他相反时不自动反手，只记警告（反手是另一套决策）。
2. `_lots_after_start()`：**只补跟单起点之后开的仓**。他跟单前就持有的历史仓位
   不算「漏单」，要不要镜像仍由 `follow_history_positions` 决定 ——
   否则一开程序就会把他整个旧仓位本子按市价镜像一遍。
3. `_open()` 加 `tag` 参数，补差单的 `source_key` 前缀是 `rebal:`，和事件单区分。
4. 配置项 `rebalance`（默认 `true`）+「运行方式」页第四个勾选框。
   自检里 `_make()` 默认关掉（场景都在数下单笔数），场景 U 单独打开验证。

文件：`copy_engine.py`、`feed.py`、`gui.py`、`gui-config.json`、
`CHANGELOG.md`、`MAINTENANCE.md`

验证：
- `py -3.12 copy_engine.py --selftest` → **30/30**（新增场景 U：超龄信号被吞
  → 同一轮补差补到 0.05 张 → 之后不再重复补）
- 用真实数据离线模拟（MemoryBroker + 真实持仓 0.15）：
  `对账补差 NVDAUSDT LONG：目标 0.904 张，持有 0.15 张，补 0.754 张`
  → `LIMIT BUY 0.754 @ 162.74899`（improve 0%，即他的加权均价）
- `run_all.py` 9/9 PASS；`_refs/_replay_check.py` 7/7 PASS

遗留：
- 补差只在 `process()` 里跑，也就是**必须在轮询**。停着不动时不会补。
- 目标基于 `my_equity`（钱包总额）：用户加钱/减钱后，下一轮会按新权益重新对齐，
  这是等比口径的应有行为，但可能表现为「突然补仓」。

**数据源换成订单级：仓位算小 6 倍的根因**

触发：用户指出「他开了两单 NVDAUSDT，合计总价值 50 万 USDT 左右，而他带单余额
是 329144，你这里也没处理好啊」。

核实：
- 网页「最新操作记录」显示两笔：`02:51:37` 1624.71 张 @ 162.71125（264,358.60）、
  `02:51:48` 1582.28 张 @ 162.78775（257,575.80），合计 **3206.99 张 / 521,934.40**。
- 我们账本里只有 **539.65 张 / 87,855**，是真实仓位的 **16.8%**。
- 根因：`trade-history` 一次只回 **10 条逐笔撮合**（`total=6000`），他一秒里
  成交几十笔，我们只拿到最后 10 笔。`page` / `rows` / `indexValue` / `startTime`
  等服务端**全部忽略**（逐个试过，都返回同一批），翻不了页。

改动：
1. 新增 `LeadApi.orders()` 读 `lead-portfolio/order-history` —— 这是网页
   「最新操作记录」用的端点，**按订单聚合**，一条就是一个完整订单，
   `executedQty` 是整单成交量、`avgPrice` 是整单均价，不截断。
2. 新增 `LeadApi.order_fills()`：`side` + `positionSide` 判开平（LONG 的 BUY 开、
   SELL 平；SHORT 反之），`executedQty × avgPrice` 算名义，`totalPnl` 带已实现盈亏。
   同一秒同方向的多单仍由 `aggregate()` 合并，`Fill.key` 语义不变。
3. `LeadApi.fills()` 与 `api_feed.py` 的 CLI 都切到订单级数据源；
   `trade_history()` / `fills_from()` 保留做诊断，不再走主链路。

文件：`api_feed.py`、`CHANGELOG.md`、`MAINTENANCE.md`

验证：
- `py -3.12 api_feed.py --rows 10` → 10 条订单，末两条正是
  `NVDAUSDT 1624.71@162.71125` / `1582.28@162.78775`，与网页逐条对得上。
- `position_ledger.replay()` → 存活 2 个批次、名义合计 **521,934.40**，
  与网页两笔之和一致（旧数据源只有 87,855）。
- `run_all.py` 9/9 PASS；`_refs/_replay_check.py` 7/7 PASS。

遗留：
- **已经跟小的仓位不会自动补齐**：重启后这两条 OPEN（02:51）会被
  `signal_max_age_s=900` 当超龄信号吞掉，引擎不会补开。按比例应为 0.90 张，
  当前只有 0.15 张。要么手动补，要么加「启动时按账本对账补差」。
- `order-history` 同样只回最近 10 单，翻页参数无效。10 单 ≈ 一天多的操作，
  比 10 条逐笔好得多，但连开十几个仓位时仍会溢出。

**开机自启 / 息屏保持 / 启动即轮询改为配置项**

触发：用户质疑「是否开机自启、息屏保持允许，应该加到配置文件里面吧，
你为什么不加进去呢」。确实是我做偏了 —— 上一轮是按「一次性配好」的思路做的
（启动文件夹丢个 .lnk + 无条件拦睡眠），没走配置契约。

问题在于：**配置项才是唯一真源**。快捷方式在启动文件夹里、行为在代码里，
两边对不上时用户没法从界面看出到底会怎样；换机器、改路径、别人接手都要去
猜启动文件夹里有什么。

改动：
1. `DEFAULTS` 加三个键，`_load_config()` 用 `as_bool` 校验：
   - `auto_poll`（默认 `true`）：启动后自动开始轮询。关掉就得每次手点，
     忘了就是整夜不跟 —— 2026-11-14 凌晨那次漏单很可能就是这个。
   - `autostart`（默认 `false`）：开机自启。
   - `keep_awake`（默认 `true`）：轮询期间阻止系统睡眠、允许息屏。
2. 界面：「运行配置」页新增「运行方式」卡片，三个勾选框 + 保存按钮，
   走 `_checks` 自动进 `_collect_config()` / `_save_config()`。
3. `sync_autostart(enabled)`：勾了就写启动文件夹的 `外场跟单.lnk`
   （目标 `sys.executable`、参数 `"…\gui.py"`、工作目录项目根、
   窗口最小化），取消就删。**不再需要手工放快捷方式**。
   实现踩了一个坑：`-Command` 传含引号和中文路径的命令会被 Windows 命令行
   转义拆坏，实测 `$s.Arguments` 被写成字面量 `+ $env:TEN_ARGS +`；
   改用 `-EncodedCommand`（base64 + UTF-16LE）后正常。
4. 保存时立即生效：正在轮询时改 `keep_awake` 直接调 `set_keep_awake()`；
   `autostart` 只在值变化时才去动启动文件夹（要起 PowerShell，别每次都调）。
5. `--autostart` 参数保留但降级为「强制自动轮询」，兼容旧快捷方式；
   现在是否自动开始由 `auto_poll` 决定。
6. 顺手把「运行配置」页的卡片间距 16→10、内边距 16→14，让三个勾选框
   一屏放得下（原来第三个被挤到滚动区外，看着像只有两个）。

文件：`gui.py`、`gui-config.json`、`MAINTENANCE.md`、`CHANGELOG.md`

验证：
- `py -3.12 gui.py --selftest` → 30 asserts
- `py -3.12 run_all.py` → 9/9 PASS
- `sync_autostart(False)` → `已关闭`，`外场跟单.lnk` 消失；
  `sync_autostart(True)` → `已开启`，回读快捷方式：
  target=`…\Python312\python.exe`、args=`"E:\.AIGCinance-lead-copy\gui.py"`、
  wd=项目根、style=7
- 抓图 `_refs/_config-run.png`：三个勾选框完整可见

遗留：
- **运行中的旧进程会把 `gui-config.json` 写回去**。实测 11:57 时发现
  `improve_pct` 从 `0` 变回 `0.3`、`theme` 从 `dark` 变 `light` —— 旧进程内存里
  还是旧值，点一次「保存配置」就覆盖。而且旧进程的 `DEFAULTS` 里没有这三个
  新键，它存盘时会把它们**整个丢掉**。重启前别在旧界面点保存。

**成交事件加「应开/实开」+ 入场挂单不再超时撤单**

触发：用户要「成交事件里把等比缩放成我要开的、开了多少份额也搞进去」，并说
「超时就不用管了，如果他没平、然后有了更好的位置开单那就开吧」。

1. **成交事件表加两列（名义 USDT，不是数量）**：`应开 USDT` = 他这笔开完之后的
   **累计**名义按等比折成我该持的名义（用 `rep.open_context` 的累计名义，跟引擎
   `_open()` 同一套输入）；`实开 USDT` = 我当前持仓的名义（数量 × 我的开仓均价，
   `entry_price` 为 0 时用该品种最新成交价兜底）。
   用名义不用数量：数量随币价变，名义才是等比口径 —— 24.27 U 比 0.15 张直观。
   按单笔算会显示成「12.5 / 25 / 12.5」，让人以为加起来 50 却只开了 25；
   按累计算才是 12.5 / 37.5 / 50，和实开对得上。
   时间列去掉年份（`11-13 10:19:42`），单元格左右内边距 10→8px，9 列才排得下。
2. **列宽可拖**：`_table()` 新增 `stretch_last` 参数，成交表传 `False`，
   9 列全部 `Interactive`（`Stretch` 的列宽由剩余空间算，用户拖不动）。
   自检加一条断言：`resizeSection(0, +60)` 必须真的生效，防止以后改回 Stretch。
   顺带把「开/平」「方向」「品种」加宽 —— 中文表头比数字宽，按数字估的宽度
   会把表头截成「开/…」。
3. **入场限价单不再超时撤单**：`Rule.entry_timeout_s`（默认 300，GUI 设 0）。
   更优价挂单本来就是「等他回到更好的位置」，300 秒撤掉等于主动放弃这一单。
   设 0 后一直挂到他平仓（`_close` 里无条件 `cancel_all_orders`）。
   挂单页的说明文字跟着口径走。
4. **修 MemoryBroker 的持仓模拟**：原来 `positions[symbol] = quantity` 是覆盖，
   连续两次 SELL 会互相抵消，自检里「加仓只补差额」等于在假数据上验证。
   改成带符号净持仓（正=多/负=空），`get_positions` 再折成 side + 绝对值。
   改完 29/29 仍 PASS，且成交表的「应开=实开」对得上了。

文件：`feed.py`、`copy_engine.py`、`gui.py`、`theme_qss.py`、`CHANGELOG.md`、
`MAINTENANCE.md`

验证：
- `py -3.12 copy_engine.py --selftest` → 29/29 PASS
- `py -3.12 gui.py --selftest` → **30 asserts**（新增 4 条：9 列、列名、
  开仓行必须算出「应开」、列宽必须拖得动）
- `py -3.12 run_all.py` → 9/9 PASS
- 抓图 `_refs/_fills-cols.png`：9 列一屏排下，无截断、无横向滚动条；
  SPCX 三笔累计 12.520 / 37.512 / 50.017，实开 50.017，对得上

遗留：
- 「实开」显示的是**当前**持仓，不是每一行当时的历史持仓 —— 历史持仓没有留档，
  要做得再准就得把每轮持仓快照也存下来。

**出入金误报 + 开机自启 + 息屏继续跑**

触发：用户指出「应该不是他保证金转入转出，应该是开单了，这里的识别就有问题」，
并要「开机自启」和「屏幕息屏后继续工作」。

1. **出入金播报误报（真 bug）**：`_update_wallet()` 原来只跳过「空仓 ↔ 有仓」
   翻转的那一轮，持仓期间每轮都拿 `wallet` 推算值比较。而推算只覆盖**我们
   看得见的仓位** —— 带单本 `trade-history` 一次只返回最近一个仓位的成交，
   截图里他同时持有 ETH 72% / BTC 8% / SNDK 7% / MU 7% / HYPE 4% / NVDA 0.86%
   等九个品种，我们账本里只有 NVDA 一个。差额全被当成出入金，于是每次价格
   波动都凭空播一条「带单员资金变动」。
   改成**只在上一轮和这一轮都是空仓（`anchored`）时比较**：空仓时
   `wallet = marginBalance` 是精确值，持仓期间的推算值不再参与比较。
   代价：他持仓期间入金不会立刻播报，等他空仓后第一轮跳过（口径切换）、
   第二轮就能报出来。
2. **开机自启**：启动文件夹放 `外场跟单.lnk`（
   `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`），指向
   `py.exe -3.12 "…\gui.py" --autostart`，窗口最小化。`--autostart`
   在窗口起来 1.5 秒后自动 `start_polling()`，失败按 15 秒重试最多 20 次
   （开机那会儿网络/本地代理常常还没就绪）。
3. **单实例锁**：自启 + 手动双击很容易叠在一起，两个实例各自轮询、各自下单
   = 仓位翻倍且互相不知道对方挂过什么。`acquire_single_instance()` 用
   `msvcrt.locking` 锁 `gui.lock`，进程退出自动释放；拿不到锁直接退出码 4。
4. **息屏继续跑**：`start_polling()` 调
   `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`，
   **刻意不加 `ES_DISPLAY_REQUIRED`** —— 屏幕该灭就灭，只阻止系统进睡眠。
   `stop_polling()` / `closeEvent` 恢复。系统一睡 socket 全断、定时器全停，
   醒来时信号早过 15 分钟有效期，等于整夜没跟。

文件：`copy_engine.py`、`gui.py`、`CHANGELOG.md`、`MAINTENANCE.md`

验证：
- `py -3.12 copy_engine.py --selftest` → 29/29（「他提走 5 万保证金：播报一次」
  仍 PASS，因为那个用例里他是空仓；「浮亏 12 万加仓」误报数仍为 0）
- `py -3.12 gui.py --selftest` → 26 asserts
- `py -3.12 run_all.py` → 9/9 PASS
- 单实例锁实测：第一个实例拿到，第二个被拦下，释放后可再拿
- `set_keep_awake(True/False)` → `on` / `off`（`SetThreadExecutionState` 返回非 0）
- 快捷方式回读：target=`py.exe`、args=`-3.12 "E:\.AIGC\binance-lead-copy\gui.py"
  --autostart`、工作目录=项目目录、WindowStyle=7

遗留：
- 02:51 那一轮到底在不在轮询，**已经查不回来了**：`state.db-wal` 只保留最近
  1000 帧（约 76 分钟，最早只到 10:14:26），更早的被 checkpoint 覆盖进主库；
  旧代码又没有落盘日志。能确定的只有：进程从 01:58:41 起没重启过，且
  10:14 之后每 30 秒都在轮询（496 帧，间隔中位 30.2 秒）。
- 他持仓期间的真实浮盈我们算不准（只看得到一个品种），所以「敞口比例的分母」
  在持仓期本身就是估算值，这是接口窗口决定的，不是这一轮引入的。

**实盘下单被拒（tickSize / 双向持仓）+ 漏单留痕**

触发：用户「什么情况 他两点开单 你没给我操作下单啊」——带单员 02:51:48 开了
NVDAUSDT LONG 539.65 张，我方一笔没下。

定位：引擎第一次看到这条成交是 **10:40:05**（`state.db` 的 `seen.seen_wall`），
晚了 7 小时 49 分。`signal_max_age_s = 900`（15 分钟）会挡超龄 OPEN，所以它被
标记已见、不下单 —— 这条规则本身是对的（不拿几小时前的价格挂限价单），问题是
**被吞时一声不响**。至于为什么晚了 7 小时，从 `state.db` 分不出来（轮询没开 /
取数失败都可能），因为日志只存在内存里。

改动：
1. **超龄信号不再静默**：`CopyEngine.drain()` 里被 `age_cutoff` /
   `resume_cutoff` 吞掉的 OPEN 打 WARN，带品种、方向、成交时间和已过多少分钟。
2. **日志落盘**：`logs/gui.log`（2MB × 5 份滚动）。`--selftest` 把 `LOG_FILE`
   一起指向临时文件，否则自检那批假成交会写进真实日志（复盘时误导）。
3. **修 tickSize（真 bug）**：`BetterPriceExecutor.price_step_of()` 原来拿
   `get_symbol_precision()` 的 `pricePrecision` 当最小价格变动。代币化股票
   合约 `pricePrecision=5` 但 `tickSize=0.01`，挂 162.47439 直接被
   **-4014「Price not increased by tick size」** 拒掉。新增
   `BinanceService.get_symbol_tick()` 读 `PRICE_FILTER.tickSize`，取不到才回落。
   BTCUSDT 同样受影响（pricePrecision=2 / tickSize=0.1）。
4. **修双向持仓下单（真 bug）**：账户 `dualSidePosition=true`，所有订单必须带
   `positionSide`，不带就 **-4061「Order's position side does not match user's
   setting」**——等于这个账户上一笔实盘单都发不出去。新增 `is_dual_side()` +
   `_apply_position_side()`：positionSide 由 `(side, reduce_only)` 推出
   （开仓同向、平仓反向），双向模式下同时不发 `reduceOnly`（会被 -1106 拒）。
   引擎侧一行没改。
5. **保证金模式改全仓**：`_open()` 原来写死 `set_margin_mode(ISOLATED)`。
   带单员是全仓，逐仓会让单笔被单独强平、和「照抄仓位 + 跟着他止损」的口径
   不是一回事。改成 `Rule.margin_type = "CROSSED"`（币安的写法），
   `better_executor` 的旧路径同步。已用 平仓 → 改模式 → 原量重开
   （`-4048` 有持仓时不让改）把 NVDAUSDT 切成全仓，现持仓 0.15 @ 162.09。

文件：`vendor/chatdsn/binance.py`、`better_executor.py`、`copy_engine.py`、
`gui.py`、`CHANGELOG.md`、`MAINTENANCE.md`

验证：
- **真实下单**：NVDAUSDT LONG 0.15 @ 限价 162.47（他 162.8 × 0.998）→
  `order_id 1746786573`，成交价 **162.13**，`positionRisk` 已出现该持仓。
  名义 24.3 U、20x、保证金 1.22 U（按 92.67 / 327861 × 87855 等比算出）。
- `py -3.12 copy_engine.py --selftest` → 29/29 PASS
- `py -3.12 gui.py --selftest` → 26 asserts，`gui-config.json` md5 前后一致
- `get_symbol_tick`：NVDAUSDT 0.01、BTCUSDT 0.1；
  `entry_limit_price(68000, "LONG", 0.1, …)` = 67864.0（合法倍数）

遗留：
- **运行中的 GUI 还是旧代码**，这两处修复要重启才生效。不重启的话带单员平仓时
  `close_position` 会 -4061 反复失败，人被卡在持仓里。
- 02:51 那一轮为什么没在 15 分钟内拿到成交，仍未定论（轮询没开 or 取数失败）。
  现在有落盘日志，下次可查。

**自绘标题栏（无边框窗口）**

触发：用户贴图问「最上面这个可以解决也变成主题颜色吗」——系统标题栏是白的，
不跟深 / 浅主题走。

结论先摆：**Win10（本机 19045）上没有任何 DWM / uxtheme 组合能让标题栏颜色
可靠跟随 `setStyleSheet()` 切主题**，所以改成无边框 + 自绘标题栏。实测过的
死路：`DwmSetWindowAttribute(20)` 与 `(35, CAPTION_COLOR)` 写入都返回 `hr=0`、
读回值确实是 1，但 `PrintWindow` 与屏幕抓取都显示标题栏仍是白色；
`SetWindowPos(SWP_FRAMECHANGED)`、`SetWindowTheme("DarkMode_Explorer")`、
`AllowDarkModeForWindow`（ordinal 133 返回 False）、hide/show 全部无效；
`styleHints().setColorScheme(Dark)` 曾让标题栏变 `(43,43,43)`、标题文字转白，
但再切一次主题就复现不出来。

改动：
- `App.__init__`：`Qt.Window | Qt.FramelessWindowHint`。`_build_widgets()` 先加
  46px 高的 `QWidget#titlebar`（标题 + 带单员名 + 主题按钮 + 三个窗口按钮），
  再放原来的 body。
- `App.nativeEvent()` 接管三条消息：
  - `WM_NCHITTEST` → 标题栏空白 `HTCAPTION`、四边四角 `HT*`、**按钮上必须
    `HTCLIENT`**（否则按下会变成拖窗口）。拖动 / 双击最大化 / 贴边吸附全部
    由系统实现，不手写鼠标追踪。
  - `WM_NCLBUTTONDBLCLK` + `WM_LBUTTONDBLCLK` 自己切最大化。
    `DefWindowProc` 转 `SC_MAXIMIZE` 要求 `WS_MAXIMIZEBOX`，无边框窗口没有
    这个位；加上会多出一圈原生边框、还要重算坐标，不划算。
  - `WM_GETMINMAXINFO` 把最大化限制在显示器工作区，并写回 `ptMinTrackSize`。
- 坐标换算：`msg.pt` 与 `GetWindowRect` 都是屏幕物理像素，减窗口原点后要除以
  `devicePixelRatio()` 才和 Qt 的逻辑坐标对齐（本机 150%，不除就整体偏移）。
- `apply_frame_shadow()`：`DwmExtendFrameIntoClientArea(MARGINS(1,1,1,1))` 给
  无边框窗口补回投影。
- `theme_qss.py` 新增 `QWidget#titlebar`（`$BG` + 1px 下边框）与
  `QPushButton[role="winbtn"]` / `[role="winclose"]`（透明底、无圆角，
  hover 用 `$HOVER` / `$WIN_CLOSE=#E81123`）。符号不用字符，由
  `WinButton.paintEvent` 画线（中文字体里「□」「✕」的字宽和基线都不稳），
  关闭按钮 hover 时符号转白。
- 顺手补两处属性：`WinButton` 加 `WA_Hover`（`underMouse()` 在重绘时才准）、
  `titlebar` 加 `WA_StyledBackground`（纯 `QWidget` 不设就不画 QSS 背景）。

文件：`gui.py`、`theme_qss.py`、`MAINTENANCE.md`、`CHANGELOG.md`；
新增 `_refs/_test_frame.py`、`_refs/_test_drag.py`

验证：
- `py -3.12 gui.py --selftest` → `Qt GUI selftest OK (26 asserts)`
  （新增 4 条：无边框、标题栏高度、标题栏走 QSS、窗口按钮 `role`）。
  `gui-config.json` md5 前后一致。
- `py -3.12 _refs/_test_frame.py` → 命中测试 **13/13**：标题栏空白 `HTCAPTION`、
  四个窗口按钮 `HTCLIENT`、四边四角 `HTLEFT/HTRIGHT/HTTOP/HTBOTTOM/
  HTTOPLEFT/HTBOTTOMRIGHT`、内容区 `HTCLIENT`；最大化后窗口高 1540 =
  工作区高 1540（不盖任务栏），按钮符号 `restore`，还原后 `max`。
- `py -3.12 _refs/_test_drag.py` → 注入鼠标实测 **4/4**：拖标题栏位移
  (90,60) 物理 = (60,40) 逻辑；拖左边缘宽 +120、x −120；拖下边缘高 +75；
  往左上猛拖被 `ptMinTrackSize` 挡在 1470×900（= 980×600 逻辑）。
- 深浅两套整窗抓图 `_refs/_frame-dark.png` / `_refs/_frame-light.png`：
  标题栏底色分别 `#0b0e11` / `#ffffff`，标题、带单员名、三个按钮符号都清晰。
- `py -3.12 run_all.py` → **9/9 PASS**。

遗留：
- 注入鼠标测「双击标题栏」不稳定（未激活的窗口会吃掉第一下点击；按下与抬起
  同一瞬间发出会让模态移动循环吞掉第二下），该脚本没有留在 `_refs/`。双击分支
  本身用直接投递 `WM_NCLBUTTONDBLCLK` 验证过：最大化到工作区、再双击还原、
  按钮符号同步。
- 无边框窗口最大化后没有系统窗口的圆角，观感略有差别，未处理。

**视觉细化 + 移除 tkinter 版**

触发：用户反馈「qt 很厉害 但是原来的一些圆角啊 细节啊 没学到位」，
并要求用前端 skill 重做排版配色；同时指出「开始轮询后 停止轮询怎么没有颜色」。
确认可用后移除 tkinter 版赘余代码。

改动：
- **停止按钮配色**：`_set_poll_button()` 原先把 `role` 置空回落到灰底，
  看起来像禁用。改为 `accent`（黄，开始）/ `danger`（红，停止）一对语义色，
  QSS 新增 `QPushButton[role="danger"]` 三态。
- **卡片描边**：1px `BORDER` → **2px `HOVER`**。tk 版 `RoundedCard` 就是
  `outline=HOVER, width=2`，1px 深色描边会让卡片失去轮廓、和背景糊在一起。
- **圆角刻度**：`R_CARD=14 / R_TABLE=12 / R_BTN=9 / R_INPUT=8` 写进
  `theme_qss.py` 顶部，与 tk 版 `RoundedCard(radius=14)`、
  `RoundedButton(radius=9)` 对齐，QSS 里不再出现魔数。
- **表格**：行高 32、表头高 34、表头 padding 8/10、`item:hover` 整行高亮
  （需 `setMouseTracking(True)`）、表头对齐跟随列对齐（数字列表头右对齐，
  否则读起来错位）、交替行改用独立的 `ROW_ALT` 而不是窗口底色。
- **按钮手感**：`:pressed` 用上下内边距互换模拟按下（QSS 没有 transform）。
- **输入框**：`:hover` 换描边色、`:focus` 变 accent、下拉列表带圆角与内边距。
- **下拉箭头自绘**：QSS 里 CSS 那套 border 三角在 Qt 会塌成一个实心方块
  （实测确认），SVG 又要依赖 qsvg 插件。新增 `gui.ComboBox`，在
  `paintEvent` 里补一笔折线，颜色取 `CURRENT`，跟着主题变。
- **修一个真 bug**：`_announce_render()` 从 0 重画时没清空文档，切主题会把
  播报正文**再追加一遍**（变成两份）。现在从 0 重画先 `clear()`，并保存/还原
  滚动位置。自检加了一条断言守它。
- **修一个更危险的 bug**：`--selftest` 会改写真实的 `gui-config.json`。
  `_toggle_theme()` 末尾要存盘，自检里点了两次切主题，于是每跑一次回归就把
  用户的实盘参数覆盖一遍 —— 实测把 `better_price_enabled` 写成 `false`、
  `theme` 写成 `dark`。现在 `selftest()` 把 `CONFIG_FILE` 指向临时文件，
  `main()` 里再比一次文件字节，被改就断言失败。
- **移除 tkinter 版**：`gui.py`(1754) / `theme.py`(415) / `_refs/_capture_preview.py`
  / 旧预览图 / `start-gui-qt.bat` 全部归档到 `_refs/archive-tkinter/` 后删除；
  `config_defs.py` 并入 `gui.py` 文件头；`start-gui.bat` 改为启动 `gui.py`；
  `run_all.py` 去掉 tkinter 检查项、`pick_python()` 的探测改成 `PySide6`。

文件：`theme_qss.py`、`gui.py`、`run_all.py`、`start-gui.bat`、
`requirements.txt`、`MAINTENANCE.md`、`CHANGELOG.md`；
删除 `gui.py`、`theme.py`、`config_defs.py`、`_refs/_capture_preview.py`、
`start-gui-qt.bat`、`_refs/gui-preview-*.png`

验证：
- `py -3.12 gui.py --selftest` → `Qt GUI selftest OK (22 asserts)`
  （新增「切主题后播报正文长度不变」「自检不改写真实配置」）。
- `py -3.12 run_all.py` → **9/9 PASS**（移除 tkinter 项后从 10 项变 9 项）。
- 深浅两套 × 六个 tab 共 12 张截图目视确认：卡片描边、表格行高、
  表头对齐、停止按钮红色、下拉箭头、播报不再重复。

遗留：
- 表格滚动到底部时首行会被表头切掉半行，属于滚动视图的固有表现，未处理。
- 截图脚本目前是临时命令，没有固化成 `_refs/` 下的工具。

**界面层迁移到 Qt（PySide6 + QSS）**

触发：主题切换在 tkinter 版上改了三轮仍是「一部分先变、一部分后变」，
用户抓拍到「按钮正常、卡片整片空白」；随后又反馈「打开还会卡一下」。
用户直接指定方案：PySide6 / PyQt6 + QSS + ThemeManager，用
`QApplication.setStyleSheet()` 整体替换，追求「绝对可靠」。

根因（前一轮已定位）：tkinter 换主题必须按「重建 ttk 样式 → 重涂直上色
控件 → 重画自绘 Canvas」三步走，三步之间隔着 idle 周期；窗口从最小化恢复
时布局尺寸还没算完，`RoundedCard._draw()` 因 `w < 4` 提前返回，而
`RoundedButton._draw()` 用 `cget("width")` 兜底照画 —— 于是「卡片空、
按钮正常」。这套时序问题在 tkinter 里没有干净的解法。

改动：
- 新增 `theme_qss.py`：整份 QSS 模板（`string.Template`，用 `$VAR` 避开与
  花括号冲突）+ `ThemeManager`。`apply()` 只做一次 `setStyleSheet()`。
- 新增 `gui.py`：`QMainWindow` + `QTabWidget` 六个 tab，业务编排与
  `gui.py` 同构，`CopyEngine` / `api_feed` / `account` / `state_store` /
  `better_executor` 一行未改。
- 新增 `config_defs.py`：`DEFAULTS` / `MODE_TEXT` / `PCT_TEXT` / `as_bool`
  两套界面共用，`gui.py` 改为从这里 import。
- `_toggle_theme()` 的顺序：`apply()` → `_recolor()` → 存盘 → 记日志，
  中间不让出事件循环。`_recolor()` 重涂 QSS 管不到的两处：表格 item 前景色、
  `QTextEdit` 的字符格式（播报正文的颜色是写进文档格式的，QSS 改不动它）。
- 播报正文改为结构化存储 `[(kind, text)]` 再渲染，主题切换时整份重画；
  超过 600 段从头部裁 200 段并重画。
- 删掉 tkinter 那套「恢复可见时补重绘」的补丁在 Qt 版里不再需要；
  `gui.py` 原样保留作回退。
- `run_all.py` 增加 Qt 自检项；`gui.py` 缺 PySide6 时退出码 3，
  标 SKIP 而不是 FAIL。
- 新增 `start-gui-qt.bat`。

文件：`theme_qss.py`（新）、`gui.py`（新）、`config_defs.py`（新）、
`gui.py`、`run_all.py`、`requirements.txt`、`start-gui-qt.bat`（新）、
`MAINTENANCE.md`、`CHANGELOG.md`

验证：
- `py -3.12 gui.py --selftest` → `Qt GUI selftest OK (20 asserts)`。
  其中主题相关的三条：QSS 文本切换后确实不同、二次切换回到原主题、
  表格行颜色随主题变化（`#dc5b50` ↔ `#c4453e`）。
- `py -3.12 gui.py --selftest` → `GUI selftest OK (16 asserts)`，回退壳未破。
- `py -3.12 run_all.py` → 10/10 PASS（含新增的 Qt 项）。
- 离屏与真实 Windows 平台各渲染 12 张截图目视确认：深浅两套、六个 tab、
  无深色补丁、无文字裁切、表格正负色正确。
- PySide6 6.10.0 装在系统 Python 3.12 上；首次 `pip install` 只留下
  `dist-info` 而没有包目录，导入报 `ModuleNotFoundError`，
  `pip install --force-reinstall --no-deps PySide6_Essentials` 修好。

遗留：
- 主题切换的「无撕裂」目前是结构性论证（一次 `setStyleSheet` + 同帧重涂）
  加截图确认，还没有逐帧录屏验证；等用户实机点一次。
- `gui.py` 与 `gui.py` 的界面代码是两份，加 tab 要改两处。
  等 Qt 版稳定后可以把 `gui.py` 降级为「仅回退」并停止同步新功能。

**第三轮修正（同日 01:12）**：用户抓拍到「按钮正常、卡片整片空白」，
指出脚本抓不到中间态。这一张图直接定位了根因：
`<Visibility>` 早于布局完成到达，那一刻 `canvas.winfo_width()` 还是 1，
`RoundedCard._draw()` 有 `w < 4 → return` 保护所以跳过；而
`RoundedButton._draw()` 用 `cget("width")` 兜底，照样画了出来 ——
所以是「卡片空、按钮正常」，不是颜色问题。恢复时尺寸没变又不会产生
`<Configure>`，卡片就一直空着，直到用户拖一下窗口。

改动：
- `_on_window_visible()` 先 `root.update_idletasks()` 再重画。顺序是关键：
  先让 Tk 算完几何，`winfo_width()` 才有真值。
- `RoundedCard._draw()` 尺寸无效时改成有限重试（8 × 40ms），不再静默返回；
  画成功后 `_draw_retries` 归零。
- `RoundedCard` / `RoundedButton` 加绑 `<Expose>`（对应 WM_PAINT，
  恢复和遮挡移开都会走到它，比 `<Visibility>` 更贴时机）。
- `_on_window_visible()` 打诊断日志「窗口重绘 Xms · 事件 · 主题」，
  只在切主题后 15 秒内记录，平时鼠标进出不刷屏。

**为什么脚本复现不了**：脚本里 `root.update()` 是原子的，会把
`<Visibility>` 回调连同布局计算一次跑完，回调里拿到的尺寸已经正确；
真实交互是 Windows 消息逐条处理，回调早于布局。所以这条只能靠实测确认。

**第四轮修正（同日 01:20）**：用户反馈「还是一样，而且打开会卡一下」。
卡顿是上一轮引入的两个问题：
- `RoundedCard` / `RoundedButton` 绑了 `<Expose>`，Windows 上它随每次重绘
  反复触发，`_draw()` 里 `delete("all")` + `create_polygon` 又产生新的重绘，
  形成重绘风暴。已全部去掉。
- `force_redraw()` 带了 `RDW_UPDATENOW`，是同步重绘，在事件回调里阻塞整个
  消息循环。改为异步失效（`RDW_INVALIDATE|RDW_ERASE|RDW_ALLCHILDREN`）。

同时把补救窗口收紧：`_on_window_visible()` 改成在 `(0,16,32,60,100,200)`ms
各重画一次，覆盖前 200ms 而不是 400ms；`RoundedCard._draw()` 的内部重试从
8 次降到 3 次。实测单次重画 avg 4.70ms / max 16.00ms，
`force_redraw()` 异步版 0.00ms（只是 InvalidateRect）。

**边界**：Windows 恢复窗口时第一帧必然是缓存位图，任何程序都无法避免；
能控制的只是多久之后纠正。现在是不超过 200ms。要 0 延迟只能
`withdraw()` → 应用主题 → `deiconify()`，代价是窗口闪一下，未采用。

### 2026-11-14 00:58 · 我方权益可见 + 主题重绘 + 状态栏模式同步

**触发**：用户反馈两点 —— ① 「我的余额好像不是实时获得的，万一我自己开单、
我加钱进去呢」；② 「开关深浅色模式时从最小化点开界面，会有黑白不同步，
要一小会才能完全同步」。

**根因**：
- 权益其实每轮都在刷（`_apply_runtime()` 调 `get_balance()`），但界面上
  没有位置显示它，只有变化 >0.01 时打一条日志。加钱进去看不到反馈，所以
  主观上「不是实时」。
- 主题不同步是两个原因叠加：`ttk.Style` 的变更要等一个 idle 周期才落到
  窗口；Canvas 自绘的 `RoundedCard`/`RoundedButton` 在窗口最小化时
  `_draw()` 因 `winfo_width() < 4` 提前返回，恢复后尺寸没变就不产生
  `<Configure>`，于是停在旧色或空白，要等用户拖一下窗口才补上。

**改动**：
- `gui.py`：运行概览加「我的权益」指标卡（每轮显示 `rule.my_equity`），
  五个指标改三列两行布局；`_render_account()` 连接成功时同步刷新该卡。
- `theme.py`：新增 `repaint_all()`；`apply_theme()` 末尾
  `root.update_idletasks()`；`RoundedButton` / `RoundedCard` 绑
  `<Map>` 和 `<Visibility>` 补帧。
- `gui.py`：`_toggle_theme()` 后 `after(80)` / `after(240)` 各刷一次
  `theme.repaint_all()`；`App.__init__` 给 root 绑 `<Enter>` 和
  `<Visibility>` 到 `_on_window_visible()`，同时重画自绘控件并重设标题栏
  （Windows 上最小化恢复不一定产生 `<Map>`，这两个事件更可靠）。
- `gui.py`：`_on_mode_change()` 同步状态栏文本。原本状态栏是硬编码的
  「未运行 · dry-run 本地模拟」，只有跑过一轮才会被 `_render_snapshot()`
  覆盖；切到 live 但还没运行时一直显示 dry-run，看着像没生效。
  新增 `MODE_SHORT` 短标签表（`MODE_TEXT` 是整句说明，不能直接用在状态栏）。

**文件**：`gui.py`、`theme.py`、`MAINTENANCE.md`、`CHANGELOG.md`

**验证**：
- `py -3.12 gui.py --selftest` → `GUI selftest OK (16 asserts)`。
- `py -3.12 run_all.py --full` → **11/11 PASS**。
- 卡片色实测：`apply_theme(light)` 后所有 `RoundedCard` 的 canvas fill 均为
  `#FAFAFA`，切 `dark` 后均为 `#181A20`（脚本断言 PASS）。
- tab 按钮实测：切换后 / 最小化恢复后 / 最小化状态下切主题再恢复，
  三种路径的 polygon fill 分别为 `#0B0E11`、`#0B0E11`、`#FFFFFF`，
  与当前主题的 BG 完全一致（PASS）。
- 事件实测：`iconify()` → `deiconify()` 会触发 `<Visibility>`、`<Map>`、
  `<FocusIn>`、`<Configure>` 各一次（`<Enter>` 不触发，鼠标不在窗口内）。
- 抓拍实测：深色 → 最小化 → 恢复 → `update()` 后立即 `ImageGrab` 截图，
  内容区完整渲染（卡片、tab、按钮均为深色），无黑屏、无浅色残留。
- `gui-config.json` 执行前后 `diff` 无输出。

**第二轮修正（同日 01:05）**：用户反馈「最小化打开后整个客户区空白、只剩
浅色 tab 栏」，复查出两处：
① `repaint_all()` 只 `except tk.TclError`，任何一处 `AttributeError` 都会
中断整个循环，后面的控件全部停在旧主题 —— 表现是「一部分变了、一部分没变」。
改为逐控件 `except Exception: continue`，`apply_theme()` 的 `_TINTED` /
`_TAGFN` 循环同步改。
② 只改 ttk style 变量不会产生 `WM_PAINT`，最小化恢复时 DWM 用缓存位图，
Tk 层面刷不掉。新增 `theme.force_redraw()`：`RedrawWindow` 带
`RDW_INVALIDATE|RDW_ERASE|RDW_ALLCHILDREN|RDW_UPDATENOW`，同步走一遍重绘；
在 `_on_window_visible()` 里跟重画、标题栏一起调用。

**遗留**：手动开单会污染跟单（`have` 少开、`_close` 平掉手动仓、
`cancel_all_orders` 撤手动挂单），仍是待决策 #2，未动。

### 2026-11-14 00:40 · 敞口分母改用钱包余额：剔除浮亏，加出入金播报

**触发**：用户确认跟单策略是「不挂止损、全看他操作、共生死」，并要求
① 敞口比例必须恒定（他浮亏不能让我方仓位放大）；② 用实时获取的仓位和
余额解决，不要固定比例手工重算；③ 他动保证金要在日志和播报里体现；
④ 精度和显示全部保留，不为好看截小数。

**根因**：`_open()` 的分母是 `detail().marginBalance`，含未实现盈亏。他浮亏
30% 时分母缩水 30%，同一笔开仓算出的比例放大 1.43 倍；浮亏 60% 放大 2.5 倍。
叠加 `open_context` 的「累计对齐」逻辑，每一单都会把之前浮亏造成的目标漂移
一次性补上，形成复利式膨胀。而 `my_equity` 取 `totalWalletBalance`（不含浮亏），
两侧口径不对称，方向永远是「他亏我重、他赚我轻」。

**改动**：
- `position_ledger.wallet_state()`：`marginBalance − 未实现盈亏 = 钱包余额`。
  未实现盈亏由 `surviving × fapi 标记价` 逐批算出。空仓时未实现为 0，
  `marginBalance` 就是精确值，是每次的校准点，所以推算误差只在一个持仓
  周期内存在。拿不到标记价的品种按 0 计，方向偏保守（仓位偏小）。
- `api_feed.mark_prices()`：`fapi/v1/premiumIndex` 全量标记价，949 个合约
  约 100KB，5 秒缓存，走 `auto` 通道（bapi 与 fapi 可达性不同）。
- `copy_engine`：新增 `lead_wallet` / `wallet_state` / `fund_changes` /
  `_pnl_counted`；`process(fills, lead_balance=None, marks=None)` 接收实时
  余额与标记价；`_open()` 的分母改为 `lead_wallet`，为空才回落
  `rule.lead_balance`；`lead_wallet` 落 `state.db` 的 `meta`，换人时清空。
- `copy_engine._update_wallet()`：出入金 = 钱包余额变化 − 本轮新处理 CLOSE
  的已实现盈亏（按 `Fill.key` 去重，防 `_unsee` 重试重复累加）。阈值
  `max(50, 前值×0.1%)`；空仓 ↔ 有仓的口径切换那一轮不比较。
- `gui.py`：指标卡改为「他的钱包余额」+「他的浮盈亏」；状态栏显示钱包余额；
  播报 tab 新增「保证金转入/转出」卡片，含变动金额、钱包余额前后值、
  保证金余额、未实现盈亏、口径标注（空仓精确 / 持仓推算），全部保留
  8 位小数。`_announce()` 的门禁从「必须有 signals」改成「signals 或
  资金变动任一非空」，否则他空仓提钱时播报会被吞掉。
- 新增 `wallet_probe.py`：只读诊断，打印 `marginBalance` / `unrealized` /
  `wallet` 与逐批明细，`--watch N` 连续采样，用来自证分解是否成立。

**文件**：`position_ledger.py`、`api_feed.py`、`copy_engine.py`、`gui.py`、
`wallet_probe.py`（新）、`MAINTENANCE.md`、`CHANGELOG.md`

**验证**：
- `py -3.12 copy_engine.py --selftest` → **29/29 PASS**，新增 3 个场景：
  他浮亏 12 万时加仓，分母仍是钱包余额（`加仓后=0.496`，按 `marginBalance`
  会是 `0.828`）；他提走 5 万保证金播报一次、1 USDT 抖动不报；
  平仓已实现盈亏改变钱包余额但不误报为出入金。
- `py -3.12 position_ledger.py --selftest` → **8/8 PASS**，新增 4 个：
  空仓精确锚点、多仓浮盈剥离、缺标记价记 `missing`、空仓浮亏方向正确。
- `py -3.12 run_all.py --full` → **11/11 PASS**。
- `py -3.12 gui.py --selftest` → `GUI selftest OK (16 asserts)`；
  执行前后 `gui-config.json` 字节一致（`diff` 无输出）。
- `py -3.12 wallet_probe.py` → 实时输出
  `marginBalance 331219.18986268 / 未实现盈亏 +0.00000000 /
  钱包余额 331219.18986268 · 空仓精确`。

**遗留**：`marginBalance` 是否确实含未实现盈亏尚未实测确认 —— 他当前空仓，
两者恒相等，看不出差别。等他持仓浮亏时跑 `wallet_probe.py --watch 30`：
若 `marginBalance` 在跌而 `wallet` 不动，分解成立；若两者同向同幅变化，
说明 `marginBalance` 本身不含浮亏，应把分母退回它并把 `unrealized` 置 0。
已进待决策清单 #10。

### 2026-11-13 23:40 · 多 agent 复查：重放账本、历史仓位可跟、播报与声音

**触发**：用户提出四点 —— ① 昵称为什么不能自动取（「只有发给过你一次才给搞个名字」）；
② 无我方仓位时他的平仓怎么处理、日志怎么写；③ 想要类似通知机器人的播报卡并
「有操作就用电脑发声音提示」；④ 「分多个 agent 专门找问题，测试漏掉了吗」。
并明确**推翻** 21:53 的设定：「历史仓位不是不能跟，如果他还没平仓、价格到了
他已经开着的位置或者有更优价，可以开」；还要求「先后开两次 BTC 然后一次平、
或开一次分几次平，都要按他的比例开平」。

**按用户要求开了 3 个 agent 并行审查**（仓位账本 / 昵称日志播报 / 测试盲区），
第二轮再开 2 个 agent 独立验收。找出的真实缺陷：
- 同品种加仓会 `cancel_all_orders` 撤掉第一笔，只跟最后一笔 → 丢量；
- `ledger` 增量累加只统计跟踪后 OPEN，历史仓与分次平的比例失真；
- 基线轮一刀切忽略历史仓位，与新需求相反；
- 无仓位 CLOSE 仍记 `closed=1`、日志写「平仓跟随…平仓量=全部」→ 误导；
- 昵称缓存是硬编码残值、换人后要等下一轮轮询才更新；
- `gui.py --selftest` 零断言、`run_all.py` 以 rc=0 判过 = 不崩即 PASS。

**改动**：
- 新增 `position_ledger.py`：每轮对窗口内全部 `Fill` 做 FIFO 重放，
  产出 `surviving`（仍存活批次）、`close_before`（每笔 CLOSE 的平前持仓）、
  `open_context`（每笔 OPEN 当时的累计数量/名义/加权均价）、`totals`。
  名义按**被核销批次的开仓名义**扣，不能用 CLOSE 的平仓名义。
- `CopyEngine.process()` 每轮全量重放；`_open()` 按累计名义补差额
  （旧单已部分成交时读实际持仓，只挂 `目标 − 已持有`）；`_close(f, ratio)`
  用 `f.qty / close_before` 算比例，无仓位时记 `cancel_only` 而非 `closed`。
- **历史未平仓位基线轮按原始开仓价挂更优价限价单**（限价单天然只在价格
  回到那个位置或更优时成交，不追高）；已被 CLOSE 核销的批次不跟。
  `PendingOrder.historical` + `clientOrderId` 的 `copy_hist_` 前缀，
  在 `reconcile()` 里豁免 300s 超时，只有他平仓时由 `_close()` 无条件撤单。
- 失败路径兜底：撤单失败 / `get_positions()` 异常 / 平仓下单失败一律
  `_unsee(f.key)`，下轮重试；`get_positions()` 返回空不再当成「无仓位」。
  `cancel_order` 统一 str 比较 order_id；超时只撤自己那笔。
- 权益改用 `get_balance()["total_balance"]`（`available_balance` 会随占用
  保证金缩水，导致累计仓位越跟越小）。
- 昵称：`lead_nickname_pid` 缓存绑定、`_refresh_lead_name()` 启动/换人时
  后台主动拉 `detail()`、`lead_name`/`lead_name_error` 都带 pid 校验，
  缓存不匹配显示「读取中…」；换人瞬间在飞行的旧批次不能串名。
- 新增第 6 个 tab「操作播报」：`winsound.PlaySound(..., SND_ASYNC)` 零依赖
  异步发声，卡片字段参照通知机器人；「当前操作杠杆 / 当前持仓·保证金 /
  当前持仓杠杆」明确标注「不可用（接口未提供）」，不编造；指纹去重 +
  3 秒节流；`dry-run` 静音且播报标「（模拟）」。
- `feed.py` 文档字符串里过时的「取他最近一笔仓位的实际杠杆」改为「固定 20x 逐仓」。

**文件**：`position_ledger.py`（新）、`copy_engine.py`、`gui.py`、
`state_store.py`、`feed.py`、`run_all.py`、`_refs/_replay_check.py`、
`_refs/_capture_preview.py`、`CHANGELOG.md`、`MAINTENANCE.md`

**验证**（`py -3.12 run_all.py --full` → **11/11 PASS**）：
- `copy_engine.py --selftest` **26/26**，新增 10 个场景：真实数字两次开多
  6.305+8.532→全平 14.837、ZEC 分两次平空 50%→100%、历史未平 BTC 按更优价
  跟/已平 ZEC 不跟、无仓位 CLOSE 只撤单、撤单失败重试、读取持仓失败补平、
  历史单超 300s 不撤、旧单部分成交补差额、平仓价≠开仓价基准不漂移。
- `position_ledger.py --selftest` **4/4**；`gui.py --selftest` **16 条显式断言**
  （含播报字段与模拟标注）；`_refs/_replay_check.py` **7/7**，断言改为按
  重放结果动态计算，不随真实流水变化假红。
- 播报 tab 预览图目视确认：字段齐全、不可用项标注正确、无裁切。

**收尾时又发现并修掉一处配置污染**：`_set_lead_nickname()` 原本调
`_save_config()` 全量写盘。运行中的 GUI 若持有旧的内存配置（例如用户直接改了
文件但实例没重载），一次昵称自动回填就会把旧的 `portfolio_id` 写回磁盘。
实测 `gui-config.json` 的 `portfolio_id` 曾被写回默认值 `1234567890123456789`
（熬鹰资本），而用户当前跟的是黑石策略 —— `mode=live` 下这会跟错人。
改为**只原子写昵称两个键**，并把配置恢复为 `9876543210987654321` / 「黑石策略」。
验证：`gui.py --selftest`、`_capture_preview.py`、`run_all.py --full`
三者执行前后配置字节一致，不再污染。

**遗留边界**（已写入 `MAINTENANCE.md` 第 8 节）：
① `follow_start_wall` 用 `<=` 会吞掉与锚点同秒但不同品种的新成交；
② 部分平仓 FIFO 先核销历史批次，历史挂单未成交而新单已成交时比例仍按总持仓算；
③ 同品种双向持仓目前共用一个 `pending` 键。三项均需改动更大，待用户决定是否推进。
当前 `gui-config.json` 为 `mode=live`；代码改动需重启 GUI 生效，未擅自重启或改配置。

---

### 2026-11-13 21:53 · 换人不跟历史仓位、按比例平仓、恢复日志筛选

**触发**：用户换到新交易员「黑石策略」（`9876543210987654321`，带单余额
约 9.6k USDT）后，程序把他在跟单开始前就持有的仓位当成开仓信号跟了进去；
同时指出「没有考虑多种情况、自己没测试」。另要求恢复事件日志的级别筛选、
以及平仓数量按他平掉的比例走（他开 1000u、平 200u，我方只能平 20%）。

**根因（三个独立缺陷）**：
1. `baseline_done` 只由 `last_run_wall` 决定，不看 `portfolio_id`。直接改配置
   或换人后重启时，`_switch_lead()` 不会执行，首轮直接 `process()`，窗口内
   全部历史事件被当成新信号。
2. `seen` 有 TTL（2×`max_signal_age_s` = 1800s）。即便首轮建了基线，30 分钟后
   基线 key 被淘汰，而 `trade-history` 窗口里仍是同一批历史成交，它们会
   **复活**成新 OPEN。`seen` 的 key 也不含 portfolio_id。
3. `Rule.my_equity` 默认 10000，只有手动点「保存并连接」才被
   `_render_account()` 覆盖。实盘余额实测 103.48 USDT，未连接时按 10000 等比
   计算，名义敞口放大约 **96 倍**。

**改动**：
- `state.db` 新增 `lead_portfolio_id` / `follow_start_wall` / `ledger_json`。
  `CopyEngine` 构造时比对交易员 ID，换人或首次跟单 → 清 `seen`、清账本、
  强制基线；基线锚点取窗口内**最新成交时间**，`drain()` 永久忽略
  `stamp <= 锚点` 的事件（不依赖 seen，TTL 淘汰也不复活）。
- 基线判断从 `gui.py` 下沉到 `CopyEngine.process()`，GUI 只展示
  `result["baseline"]`；运行中换人走 `engine.begin_following(pid)`。
- 新增信号年龄检查 `rule.signal_max_age_s`（生产 900s）：运行中接口延迟补回的
  超龄 OPEN 不再拿旧价格挂限价；CLOSE 仍放行（保护动作）。
- **按比例平仓**：`ledger[(symbol, side)]` 记他累计持有的**数量**（不用名义，
  价格会变、数量才守恒）。`_close(f)` 用 `f.qty / ledger` 算比例，
  部分平仓走 `close_position(quantity=...)` 并强制 `reduceOnly`；
  比例 ≥1 或账本缺失 → 整仓平兜底。撤单红线不变。
- `gui.py` 的 `_build_log_tab()` 恢复级别下拉、关键字输入、计数状态栏。
- `live/testnet` 每轮用 `get_balance()` 刷新 `my_equity`；读不到且从未验证过
  就停止跟单，不再按默认 10000 放大。

**文件**：`copy_engine.py`、`gui.py`、`CHANGELOG.md`、`MAINTENANCE.md`、
`_refs/_replay_check.py`（新）、`_refs/_capture_preview.py`

**验证**：
- `py -3.12 copy_engine.py --selftest` → **16/16 PASS**（新增 6 个场景：
  换人首轮、TTL 淘汰不复活、换人后新单照跟、部分平 20%、连续部分平仓后清仓、
  账本缺失兜底、运行中换人、超龄 OPEN 拦截）。
- `py -3.12 _refs/_replay_check.py` → **4/4 PASS**，用真实接口拉到的
  黑石策略 4 条聚合事件回放（只读，不下单）。
- `py -3.12 gui.py --selftest` → OK；`py -3.12 run_all.py --full` → 9/9 PASS。
- 实盘只读核对：`get_balance()` → `available_balance=103.48` USDT。

**遗留**：用户当前 `mode=live`；代码改动需重启 GUI 才生效，`state.db` 会在
下次启动时自动识别 `lead_portfolio_id` 缺失并重建基线。已开仓位需用户自行核对。

---

### 2026-11-13 20:32 · 杠杆口径核对与注释校正（无行为变更）

**触发**：用户问程序按多少倍杠杆开单，是 10x 还是别的。

**核对结论**：固定 **20x 逐仓（ISOLATED）**，不是 10x。
`feed.Rule.leverage = 20`；`CopyEngine._open()` 每次挂单前调
`set_leverage(symbol, 20)` + `set_margin_mode(symbol, "ISOLATED")`。
`gui.py` 构造 `Rule` 时不传 leverage，走这个默认值；
`vendor/chatdsn/config.py` 的 `DEFAULT_LEVERAGE` 也是 20，两端一致。

**改动**：`feed.py` 里 `Rule.leverage` 的注释仍写着「取他最近一笔仓位的实际
杠杆，不缩水」，那是早期方案的残留；现行代码不读带单员杠杆（
`position_history` 取杠杆的逻辑已移除）。注释改为「固定 20x 逐仓；只影响
保证金占用，不参与敞口比例」。`MAINTENANCE.md` 第 5 节补上默认值位置、
逐仓模式、以及「每次开仓会覆盖账户该品种已有杠杆设置」和改成可配的四处入口。

**文件**：`feed.py`、`MAINTENANCE.md`、`CHANGELOG.md`

**验证**：`py -3.12 main.py` →
`('set_leverage', 'ZECUSDT', 20)`、`('set_leverage', 'SPCXUSDT', 20)`，
四条 ENTRY 动作全部 `leverage: 20`；`py -3.12 run_all.py --full` → 9/9 PASS。

**遗留**：杠杆目前是代码常量，未做成界面配置项；是否可配由用户定。

---

### 2026-11-13 20:23 · 修复轮询间隔失效，扩展部分平仓能力

**触发**：用户把抓数间隔设为 30 秒，日志仍每 3 秒一条「跟进结果」；
同时提出平仓比例应与带单员一致。

**根因（轮询）**：`start_polling()` 用 `self.running.wait(wait)` 做间隔等待，
但 `running` 在整个轮询期间恒为 set，`threading.Event.wait()` 在已置位时
**立即返回**，worker 循环只受一次网络请求耗时限制（约 3 秒）。日志里
「轮询已启动，间隔 30 秒」说明 `interval` 读到了 30，只是等待没生效。

**改动**：
- 新增独立的 `_poll_wake` 唤醒事件：`start_polling()` clear、
  `stop_polling()` set、worker 用 `_poll_wake.wait(wait)`。停止轮询可立刻唤醒，
  不用等满一个间隔。
- `gui.py --selftest` 加 `poll_wait` 语义对比量测，量两种写法的实际等待时长。
- 平仓执行层扩展（**默认行为不变**）：
  `vendor/chatdsn/binance.py` 的 `place_market_order(reduce_only=...)` 与
  `close_position(symbol, quantity=None)`、`TradingExecutor.close_position`
  透传 `quantity`、`MemoryBroker` 同步支持部分平仓。
  `quantity=None` 仍是整仓平；部分平仓强制 `reduceOnly` 防反向开仓。
  这是按比例平仓的前置能力。

**文件**：`gui.py`、`vendor/chatdsn/binance.py`、`vendor/chatdsn/trading.py`、
`copy_engine.py`、`CHANGELOG.md`、`MAINTENANCE.md`

**验证**：
- `py -3.12 gui.py --selftest` →
  `poll_wait={'wake_ms': 203, 'running_ms': 0, 'separated': True}`
  （`_poll_wake.wait(0.2)` 真等 203ms，`running.wait(0.2)` 立即返回 0ms）。
- `py -3.12 copy_engine.py --selftest` → 10/10 PASS。
- `py -3.12 run_all.py --full` → 9/9 PASS。

**遗留**：平仓**目前仍是整仓平**，未按带单员部分平仓的比例缩仓。
公开 `bapi` 只有逐笔成交、没有带单员持仓快照，比例分母需靠账本重建；
两条实现路径与取舍已摆给用户，见待决策 #1，拍板后接上 `_close()` 的数量计算。

---

### 2026-11-13 20:18 · 修复默认 tab 内容与选中态错乱

**触发**：用户截图指出顶部 tab 选中「账号连接」，内容区却是「运行配置」
（跟单目标 / 抓数频率 / 事件日志）。默认应打开「账号连接」，这是 bug 不是设计。

**根因**：`TabStrip.add()` 只在添加第一页后调用 `select(0)`。所有页面
`grid(row=0, column=0)` 叠在同一个 cell，Tk 里后创建的 widget 默认压在上层，
于是最后添加的「运行配置」盖住第 0 页，而 `_current` 仍是 0。
预览图没暴露该问题，是因为 `_capture_preview.py` 显式调了 `select(0)`
把第 0 页抬回最前。

**改动**：
- `add()` 每加一页都重新 `select(self._current)`，保证刚加入的页面不盖住当前页。
- `select()` 记录 `_raised`，供自检断言。
- `gui.py --selftest` 新增 `default_tab` 字段（不显式 select，验证启动默认态）。
- `_capture_preview.py` 第一张截图故意不调 `select()`，用启动默认态，
  让这类错乱以后能被预览图直接抓到。

**文件**：`gui.py`、`_refs/_capture_preview.py`、`CHANGELOG.md`、`MAINTENANCE.md`

**验证**：
- `py -3.12 gui.py --selftest` →
  `default_tab={'current': 0, 'raised': 0, 'title': '账号连接', 'pages': 5}`。
- 启动默认态预览图目视确认：选中「账号连接」，内容为「运行概览 / 添加 API Key」。
- `py -3.12 run_all.py --full` → 9/9 PASS。

**遗留**：无。另确认 `gui-config.json` 已是 `mode=dry-run`；用户截图里的
`testnet` 是重启前的旧实例内存值，重启后即生效。

---

### 2026-11-13 20:14 · 定性 testnet -2015，执行模式默认回 dry-run

**触发**：用户开 TUN 后界面仍显示「连接失败 `/fapi/v2/account HTTP 401
{'code': -2015, 'msg': 'Invalid API-key, IP, or permissions for action'}`」，
怀疑是网络问题。

**实测（同一把 Key，分别打两个域名）**：
- `AccountProbe(testnet=False).check()` → `reachable=true`、
  `account_ok=true`、`can_trade=true`、钱包 148.59 USDT。
- `AccountProbe(testnet=True).check()` → **`reachable=true`**、
  `account_ok=false`、错误 `-2015`。

网络层是通的，401 是认证结果：这把 Key 在币安主站创建，测试网不认。
TUN 只影响传输，改变不了认证结果。日志里的「时钟差 -32 秒」也不是原因，
两侧都做了时钟补偿，且 `-2015` 与时间戳无关。

**改动**：
- `MODE_TEXT["testnet"]` 写明测试网需单独建 Key。
- `_render_account()` 把 `-2015` 翻译成中文原因，并区分测试网 / 实盘两种
  排查方向，避免再被误读成网络故障。
- `gui-config.json` 的 `mode` 从 `testnet` 改为 `dry-run`（可逆默认值：
  不发任何真实订单，且「保存并连接」能立刻通过）。要真实下单切 `live`，
  要测测试网需在测试网另建 Key。

**文件**：`gui.py`、`gui-config.json`、`CHANGELOG.md`、`MAINTENANCE.md`

**验证**：`py -3.12 run_all.py --full` → 9/9 PASS；
`py -3.12 gui.py --selftest` → GUI selftest OK。
`account.py --check --json` 与 `AccountProbe(testnet=True).check()` 输出见上。

**遗留**：最终执行模式仍由用户拍板，见待决策 #4。GUI 正在运行，
代码与配置文件改动需重启后生效；界面里直接切「执行模式」下拉可立即生效。

---

### 2026-11-13 20:05 · 双域名网络分层，取数不再依赖 TUN

**触发**：用户实测不开 TUN 时 GUI 报 `POST .../trade-history 网络失败`；
复核发现 19:23 条目的「直连可达」只测了 `fapi.binance.com`，被外推到了
`www.binance.com`。

**边界更正（实测）**：
- `POST www.binance.com/bapi/.../trade-history`：`curl --noproxy '*'` →
  8.0s 超时；`-x http://127.0.0.1:20122` → 200 / 0.18s。
- `GET fapi.binance.com/fapi/v1/time`：同一时刻强制直连也超时，走代理 200。
  直连可达性随本机代理 / TUN 状态变化，不能写成固定结论。

**改动**：
- `net.BinanceHttp` 新增 `proxy_first` 模式（代理 → 直连 → 代理）与
  `BINANCE_NET_PROXY` 显式端口；`proxied` 设了端口后 `trust_env=False`，
  不依赖系统 PAC 规则。
- `api_feed.LeadApi` 从固定 `requests.Session()` 改为
  `BinanceHttp(mode="proxy_first")`，每次请求先走代理，外层网络重试降到 2 轮。
- 新增 `_preferred`：记住上次成功的通道，后续请求不再每轮先撞 3 秒不通的路。
- `net._dotenv_value()` 让 CLI 与 GUI 读同一份网络配置（`api_feed.py` 单独跑时
  不经过 `account.load_env`）。
- `net.py --selftest` 的 PASS 改为只判纯逻辑，连通性只打印不参与判定。
- `vendor/chatdsn/binance.py` 对 testnet 401 输出中文原因。
- `.env` 追加 `BINANCE_NET_PROXY=http://127.0.0.1:20122`（原子写）。

**文件**：`net.py`、`api_feed.py`、`.env`、`.env.example`、
`vendor/chatdsn/binance.py`、`MAINTENANCE.md`、`run_all.py`

**验证**：
- `py -3.12 run_all.py --full` → 9/9 PASS，输出
  `强制直连 fapi: ConnectTimeout` / `走代理 fapi: status=200`。
- `env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY py -3.12 api_feed.py --rows 5`
  → `nickname=熬鹰资本 total=6000 returned=10`，证明显式代理生效、不依赖系统代理。

**遗留**：`gui-config.json` 的 `mode=testnet` 配了实盘 Key，
`testnet.binancefuture.com/fapi/v1/openOrders` 持续 401。见待决策 #4。
用户 GUI 仍在运行，代码与 `.env` 改动需重启后生效，未擅自重启。

---

### 2026-11-13 19:50 · 界面改版、维护文档首版、持久化缺口修复

**触发**：用户提出「不用开浏览器吧」「四个 tab 选中太突兀」「日志刷屏检测不要」
「两秒抓一次太快」「跟单员要能改」「写维护文档方便后续改造和换 agent」。

**改动**：
- 新增第五个 tab「运行配置」，配置项集中到 `gui-config.json`：
  `portfolio_id / lead_nickname / poll_interval_s / log_max_rows` 等 9 个键。
  `_switch_lead()` 换人时重建 `LeadApi`、清详情缓存、清内存与 SQLite 的
  `seen`、重置基线与昵称；`pending` 保留。`StateStore.clear_seen()` 新增。
- `TabStrip` 自绘等宽等高 tab 栏：ttk clam 的 selected tab 会换底色、加高亮
  边框并抬起，style 锁色也压不住；改用 5 个 `RoundedButton(flat=True)`，
  选中只有黄字 + 底部 2px 黄线。保留 `tabs()/tab()/select()` 兼容调用点。
- 开始 / 停止合成一个开关按钮；左上角副标题换成带单员昵称（从 `detail`
  取到后写回配置，换人清空）。
- 事件日志页去掉级别 / 关键字过滤与计数状态栏；`log_max_rows` 可配。
- 抓数间隔可配（默认 30s），UI 事件泵 `after(150)` → `after(300)`。
- **持久化缺口修复**：`resume_cutoff` 原来连 CLOSE 一起吞，关机超过 15 分钟
  且期间带单员平仓时，重启后我方仓位 / 挂单无人管理。改为只吞过老 OPEN，
  CLOSE 一律放行（`_close()` 幂等）。新增场景 J。
- 首版 `MAINTENANCE.md`。

**文件**：`gui.py`、`theme.py`、`state_store.py`、`copy_engine.py`、
`MAINTENANCE.md`、`_refs/_capture_preview.py`

**验证**：
- `py -3.12 copy_engine.py --selftest` → 10/10 PASS。
- `py -3.12 run_all.py --full` → 9/9 PASS。
- `gui.py --selftest` → `poll_toggle={'running':'停止轮询','idle':'开始轮询'}`
  `lead_header=熬鹰资本`。
- 6 张预览图重新生成，深色 / 浅色 / 容差态目视通过。

**遗留**：部分平仓按整仓平（#1）；手动持仓隔离（#2）；左栏参数是否并入
配置 tab（#3）。

---

### 2026-11-13 19:23 · 网络层双通道、断线闭环、限流降级

**触发**：用户问「没开 TUN 一定要开吗」「网络不稳定掉了重连设置了吗」。

**改动**：
- 新增 `net.py` / `BinanceHttp`：`direct.trust_env=False` 与跟随系统代理的
  `proxied` 两个 Session，`auto` 交替使用；超时拆 `(3.05, 8~10)`。
- `account.py` 只读请求 3 次重试；`vendor/chatdsn/binance.py` 的
  `_retry_attempts()` 对 GET / DELETE / 杠杆设置重试 3 次，**POST 下单一律
  不重试**，避免重复成交。
- 断线闭环：`rebuild_from_exchange()` 改为每轮 `process()` 都跑，认回
  「下单成功但响应丢失」的孤儿挂单；`_unsee()` 在 `place_limit_order` 返回
  None 时撤回事件，下一轮补开。
- `11012005` 忙码最多 6 轮退避；耗尽时 `api_feed.py` 返回 rc=2，
  `run_all.py` 标 SKIP，GUI 显示「带单本接口限流，N 秒后自动重连」。
- 轮询循环 `while running.is_set()` 无限重连 + 指数退避。

**文件**：`net.py`（新）、`account.py`、`vendor/chatdsn/binance.py`、
`copy_engine.py`、`api_feed.py`、`gui.py`、`run_all.py`、`.env.example`

**验证**：`copy_engine.py --selftest` 9/9；`run_all.py --full` 9/9 PASS。

**遗留**：该轮的「不需要开 TUN」结论于 20:05 被实测更正，见上一条。

---

### 2026-11-13 19:08 · 真实凭证接入与账户自检

**触发**：用户填了 API Key / Secret，界面报连接失败。

**改动 / 结论**：
- 那次失败是 `fapi.binance.com` 瞬时 ConnectTimeout 且账户自检无重试，
  不是凭证错误（19:23 轮补上重试）。
- `account.py --check --json` 实测 `account_ok=true` / `can_trade=true`：
  钱包 148.59 USDT、可用 108.92、未实现 +12.30；权限
  `enableReading/enableFutures=true`、`enableWithdrawals=false`、
  `ipRestrict=false`；多开了现货与万向划转权限。
- 本机时钟比交易所慢约 32 秒，自检侧与下单侧各自采样 3 次取最小 RTT
  做补偿，签名不会因此 `-1021`。

**遗留**：账户里有手动 20x 持仓（SOXLUSDT 6.26、BTCUSDT 0.009），
`_close()` 会平掉同品种仓位。见待决策 #2。

---

### 2026-11-13 18:30 · SQLite 持久化与币安双主题

**触发**：用户问「数据怎么保存，中间关掉程序会不会炸了」「加白天模式」
「学币安配色」「圆润一点」「主题按钮放右上角」。

**改动**：
- 新增 `state_store.py`：SQLite 三表 `meta / seen / pending`，单连接 +
  `threading.Lock()` + WAL。`CopyEngine._restore()` 恢复水位与在途挂单。
- `rebuild_from_exchange()` 按 `clientOrderId` 的 `copy_` 前缀认回本程序挂单，
  账户里手动挂的单不动。
- 杠杆固定 20x，移除 `position_history` 取杠杆与相关指标卡。
- `theme.py`：币安 DARK / LIGHT 双色板、Canvas 自绘圆角按钮与卡片、
  Windows DWM 标题栏染色、右上角主题切换。红涨绿跌保持不变。
- 更优幅度与可接受滑点合并为一个输入框，标签与说明随开关切换。
- 圆角卡片描边被 `inner` Frame 压住，改为 `padx/pady=8` + `fill=PANEL` +
  `outline=HOVER, width=2`。

**文件**：`state_store.py`（新）、`theme.py`（新）、`copy_engine.py`、
`gui.py`、`feed.py`、`run_all.py`、`_refs/_capture_preview.py`

**验证**：`copy_engine.py --selftest` 8/8；`run_all.py --full` 8/8 PASS；
4 张预览图目视通过。

---

### 2026-11-13 17:45 · 入场价开关、去重正确性与性能改造

**触发**：用户要求「更优幅度可开关」「保证性能」「避免幽灵单这种低级错误」
「用前端 skill 美化 UI」。

**改动**：
- `feed.entry_limit_price()` 成为入场价唯一真源；开关 ON = 更优价，
  OFF = 容差内挂限价（不追市价，超出容差宁可漏单）。
- `Fill.key` 改为 `symbol|side|action|秒级时间戳`，不含聚合后的 `qty/price`，
  修掉同秒逐笔成交分两轮返回时重复开平。
- `seen` 改 `dict + deque` 带 TTL 有界，`actions` 改 `deque(maxlen=500)`，
  `process()` 返回本轮新增动作。
- `api_feed.py` 统一 `_request()`，指数退避 + jitter，超时拆成
  `(connect, read)`，Session 复用，`detail` 60s 缓存。
- GUI 网络 / 引擎移出 Tk 主线程，日志改增量插入。
- 深色 Zinc 主题、左栏锁宽、Treeview 横向滚动、预览图按 150% DPI 换算。

**文件**：`feed.py`、`better_executor.py`、`copy_engine.py`、`api_feed.py`、
`gui.py`、`main.py`、`run_all.py`、`probe_apis.py`、`requirements.txt`

**验证**：`copy_engine.py --selftest` 6/6；`run_all.py --full` 7/7 PASS。

---

### 2026-11-13 14:00–16:30 · 基线：选型、数据源、账户接入收敛

**结论（现行代码的来源）**：
- 币安官方 Copy Trading REST 只有 `userStatus` / `leadSymbol`，读不到第三方
  带单员实时仓位；带单本页面背后的 `bapi` 接口未登录即可取逐笔成交。
- `portfolioId` 为 19 位 `1234567890123456789`；`trade-history` 是 **POST**，
  body `{portfolioId,page,rows}`；返回逐笔撮合成交，需按
  `(秒, 品种, 方向, 开平)` 聚合成页面上的一条操作记录。
- `feed.py` 正则同时兼容 App 版与网页版中文流水。
- 账户连接最终只保留 API Key + Secret 的 HMAC-SHA256 签名下单；
  扫码 / 浏览器登录 / Cookie / 网页抓取兜底等路径全部移除，因为 Cookie
  不能给 `/fapi/v1/order` 签名，也换不出 Secret。
- 等比口径：`我的名义敞口 = 我的权益 × 他的名义敞口 / 他的带单余额`；
  数量按 `stepSize` 向下取整。
- 参考仓库放在 `_refs/`（`lampts/chatdsn-python` 等），主链路只 fork 了
  `vendor/chatdsn/` 的交易所客户端。
- 本机默认 `python` 是 3.13（无 tkinter），统一用 `py -3.12` /
  `start-gui.bat`。

**文件**：`feed.py`、`api_feed.py`、`account.py`、`lead_provider.py`、
`main.py`、`gui.py`、`vendor/chatdsn/`、`probe_apis.py`、`start-gui.bat`

---

## 加功能标准流程

1. **定位层**：取数 → `api_feed.py`；解析 → `feed.py`；仓位重放口径 →
   `position_ledger.py`；跟单行为 → `copy_engine.py`；下单 →
   `better_executor.py` + `vendor/chatdsn/binance.py`；界面 → `gui.py` /
   `theme.py`。只动对应文件。
2. **新增配置项**必须同时改四处：`DEFAULTS`、界面控件、
   `_collect_config()`、`_load_config()` 的合法性校验，并在
   `MAINTENANCE.md` 第 4 节补表。否则旧配置文件会静默缺键。
3. **新增 tab**：`_build_widgets()` 里 `nb.add(...)`，页面 parent 用
   `nb.body`，容器用 `RoundedCard` 保持视觉一致。
4. **改引擎**必须跑 `py -3.12 copy_engine.py --selftest`（当前 26 个场景）与
   `position_ledger.py --selftest`（4 个），并确认 `_close()` 的无条件撤单没被动过。
5. **改视觉**跑 `py -3.12 _refs/_capture_preview.py`，目视确认深色 / 浅色 /
   日志 / 播报页；脚本会自动备份并还原 `gui-config.json`。
6. **收尾**：`py -3.12 run_all.py --full`（当前 11 项），追加本文件条目，
   同步 `MAINTENANCE.md`，把新出现的取舍写进「待决策清单」。
7. 涉及停服 / 重启 / 改运行中的服务器，每次先问，一次许可只管一次。

---

## 待决策清单

| # | 事项 | 现状 | 选项与代价 | 影响文件 |
|---|---|---|---|---|
| 1 | ~~部分平仓是否按比例缩仓~~ | **已定案并落地（21:53）**：采用方案 A 等效仓位账本，按数量比例缩仓，账本存 `state.db`，缺失退化整仓平 | 见 21:53 变更条目与 16/16 自检、4/4 真实回放 | `copy_engine.py` |
| 2 | 账户里手动持仓如何隔离 | `_close()` 会平掉同品种任何仓位 | 加「只管自己开的仓」过滤（按 `clientOrderId` / `source_key`）；或跟单账户专用（零改动但要换账户） | `copy_engine.py` |
| 3 | 左栏参数是否并入「运行配置」tab | 左栏保留入场价开关与执行模式，按操作台定位 | 并入：设置全部集中；不并入：高频参数随手可改 | `gui.py` |
| 4 | 执行模式最终选哪个 | 已默认回 `dry-run`（可逆、零真实下单）；实盘 Key 有效 | 切 `live`（真实下单）；保持 `dry-run`；或在测试网另建 Key 后再切 `testnet` | `gui-config.json`、`.env` |
| 5 | 500 条成交窗口外的平仓 | 挂单靠 `pending` + 每轮对账撤掉，已成交仓位不自动平 | 增加交易所持仓对账（需要可靠的带单员当前持仓源）；或接受该边界 | `api_feed.py`、`copy_engine.py` |
| 6 | `follow_start_wall` 同秒事件 | `<=` 吞掉与锚点同秒但不同品种的新成交 | 放宽为 `<` 需同时解决基线事件 TTL 淘汰后复活；或接受该边界 | `copy_engine.py` |
| 7 | 部分平仓的批次隔离 | FIFO 先核销历史批次，比例仍按总持仓算 | 按批次维护我方对应关系（改动较大）；或接受该边界 | `copy_engine.py`、`position_ledger.py` |
| 8 | 同品种双向持仓 | `pending` 按 `symbol` 单键，双向会互相撤单 | 改键为 `(symbol, side)`，需同步 `state_store` 主键与对账逻辑 | `copy_engine.py`、`state_store.py` |
| 9 | 当前执行模式 | `gui-config.json` 为 `mode=dry-run`（2026-11-16 核对） | 切 `live` 前先确认仓位管理页的应开数量、盈亏页已按 `mode` 分开记账；用户决定 | `gui-config.json` |
| 10 | `marginBalance` 是否含未实现盈亏 | 分解已按「含浮亏」实现，但他当前空仓无法验证 | 他持仓浮亏时跑 `wallet_probe.py --watch 30`：`marginBalance` 跌而 `wallet` 不动 = 成立；两者同向同幅 = 应把分母退回 `marginBalance` | `position_ledger.py`、`copy_engine.py` |
| 11 | `lead_provider.py` 是否删除 | 整模块无人 import（`grep lead_provider` 零命中） | 删：少 4.4KB 死代码，但**项目没有 git**，删了不可恢复；留：占目录 | `lead_provider.py` |
| 12 | `api_feed.py` 四个未调用方法 | `snapshot_fills` / `trade_history` / `position_history` / `fills_from` 在应用内零调用，`_refs/_diag_live.py` 还在用 `trade_history` | 删：干净但丢掉接口探测留底；留：当接口文档 | `api_feed.py` |
| 13 | 已实现盈亏要不要精确到交易所成交价 | 现在按平仓时刻标记价估算，误差来自滑点 | 上 `/fapi/v1/userTrades` 或 `income`：精确，但每轮多一次签名请求 | `pnl_tracker.py`、`copy_engine.py` |
| 14 | 盈亏页缺口径开关 | 默认按当前 `mode` 显示，切换模式曲线断开重画 | 加「对比带单员」叠加线：直观但两张曲线量纲不同，需要对数轴或百分比化 | `gui.py` |
| 15 | 手动平仓后如何抑制自动补仓 | 未实现（快捷平仓前置条件） | 按 `(品种, 方向)` 抑制到「带单员该方向归零后再开」：语义最准，但带单员加仓期间不跟；或按时间窗口抑制：简单，但同向行情里会偷偷补回；或只平当轮不抑制：按钮等于没用 | `copy_engine.py`、`state_store.py` |
| 16 | 仓位管理是否并入跟单盈亏 | 现为两个 tab | 合并成一个（上盈亏下持仓 + 快捷操作），需要先定 #15；不合并则保持「对账」与「记账」职责分离 | `gui.py` |

---

## 交接卡（换 agent 第一屏）

**项目**：币安合约外场跟单，路径 `E:/.AIGC/binance-lead-copy/`。
启动 `start-gui.bat`（`py -3.12 gui.py`）；默认 Python 3.13 没有 tkinter。

**当前状态**：`run_all.py --full` **11/11 PASS**，
`copy_engine.py --selftest` **26/26**，`position_ledger.py --selftest` 4/4，
`gui.py --selftest` 16 条断言，`_refs/_replay_check.py` 7/7。
`gui-config.json` 为 `mode=live`，用户已启用实盘；代码改动需重启 GUI 生效。
`state.db` 下次启动会自动识别 `lead_portfolio_id` 缺失并重建基线。

**四条红线**：
1. `CopyEngine._close()` 无条件 `cancel_all_orders(symbol)`，与入场价模式、
   我方有无仓位无关；撤单失败必须 `_unsee()` 让事件下轮重试。
2. `Fill.key` 不含聚合数值；`feed.entry_limit_price()` 是入场价唯一真源。
3. 下单 POST 不重试；丢单靠 `rebuild_from_exchange()` 认回、
   `_unsee()` 补开，不靠重复下单。
4. 仓位比例只认 `position_ledger.replay()` 的 `close_before` /
   `open_context`；名义按开仓价核销，不能用平仓名义。

**协作约定**：
- 有取舍先摆正反两面，不要自己挑一个就动手；别把自编规则标成「已定」。
- 禁止讨好、反感 AI 味、不要堆模板化排比。
- 不要一口气交付多个不相关项目。
- 文档只写现行状态，交付面含标题 / 章节名 / 状态栏 / 版本说明 / 待决策
  条目 / 文件名；历史取舍放本日志，不写回交付文档。
- 命名按功能名，不加项目名或插件名前缀。
- 停服 / 重启 / 改运行中的服务器，每次先问。

**第一件事**：读 `MAINTENANCE.md` 第 3 节数据流与第 5 节不变量，
再读本文件「待决策清单」，确认用户当前要推进哪一项，然后跑一次
`py -3.12 run_all.py --full` 建立基线。
