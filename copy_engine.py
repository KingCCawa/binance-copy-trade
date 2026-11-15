"""事件流跟单引擎：按成交事件的时间顺序处理，杜绝幽灵单。

为什么不用「持仓快照 diff」：
  快照只看净结果。若他在两次轮询之间开仓又止损平仓，快照里这个品种
  从没出现过，diff 不会产生任何平仓事件，我方挂着的入场限价单就不会被撤；
  等价格回落成交，就变成一笔他已经平掉、我却还开着的幽灵仓位。

本引擎的规则：
  1. 每条 OPEN 事件挂一笔入场限价单（更优价 / 容差价由开关决定），
     登记为 pending，不阻塞等待成交；
  2. 每条 CLOSE 事件**无条件撤掉该品种所有挂单**，
     不管我方有没有仓位、入场单有没有成交、入场价用的是哪种模式；
     有仓位才平仓；
  3. 按事件时间顺序处理，同一批里「开→平→再开」也能正确依次执行；
  4. 事件按 Fill.key 幂等去重（key = 秒/品种/方向/开平的事件桶标识），
     重复轮询、同一秒逐笔成交分两轮返回都不会重复开平；seen 带 TTL 有界；
  5. pending 单每轮 reconcile：已成交/已消失则销账，超时则撤单。

  py -3.12 copy_engine.py --selftest   # 八个场景的回归验证
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from math import floor
from typing import Any

from feed import Fill, Rule, entry_limit_price
from position_ledger import replay as replay_lead, wallet_state

# 入场单的 clientOrderId 前缀。重启对账只认这个标记，账户里手动挂的单不碰。
ORDER_TAG = "copy_"


@dataclass
class PendingOrder:
    symbol: str
    order_id: Any
    side: str
    qty: float
    limit: float
    leverage: int
    created_at: float
    source_key: str
    # 历史未平仓位的挂单不设 300s 超时：它等的是几小时前的开仓价，
    # 只有他平仓（CLOSE 无条件撤单）才该撤。
    historical: bool = False


def target_position(notional: float, qty: float, side: str, wallet: float,
                    equity: float, tick: float, step: float,
                    rule: Rule) -> dict:
    """把「带单员在某品种的仓位」等比折成「本程序应持多少」。

    单一真源：`CopyEngine.rebalance()` 和界面的「仓位管理」都调它。
    两边各写一遍的话，排查时看到的数和实际下单的数就可能是两套，
    等于白排查。

    - notional / qty：带单员的累计名义与累计数量（`_replay.totals` 或 `open_context`）
    - wallet：带单员钱包余额（已剔除未实现盈亏）
    - equity：本程序权益（钱包总额，不含浮亏）
    - tick / step：该品种最小价格变动 / 最小数量步进

    **按数量等比，不按名义等比**：带单员持 1 个 BTC、缩放系数 0.003，本程序就持
    0.003 个 BTC。名义口径（先算 USDT 敞口再除以限价）在限价与带单员成交价
    不一致时会多算或少算一份数量，那不算「份额一致」。价格相同时两种口径
    结果完全相同 —— 差异只出现在限价与带单员均价不同的时候。
    返回的 target_qty 已按 step 向下取整 —— 宁可少开一点，不能超；
    target_notional 是按限价估的占用名义，只用于展示与保证金估算。
    """
    avg = notional / qty if qty else 0.0
    limit = (entry_limit_price(avg, side, tick, rule.improve_pct,
                               rule.slippage_tol_pct,
                               rule.better_price_enabled)
             if avg > 0 else 0.0)
    # 占比（带单员名义 ÷ 带单员钱包）只用于展示杠杆
    ratio = notional / wallet if wallet > 0 else 0.0
    # 份额用「数量 × 资金缩放」，与限价无关
    scale = equity / wallet if wallet > 0 else 0.0
    target_qty = (floor(qty * scale / step) * step
                  if qty > 0 and step > 0 else 0.0)
    target_notional = target_qty * limit
    return {"avg": avg, "limit": limit, "ratio": ratio,
            "target_notional": target_notional, "target_qty": target_qty}


class CopyEngine:
    def __init__(self, executor, rule: Rule, logger=None, store=None,
                 portfolio_id: str = ""):
        self.executor = executor
        self.rule = rule
        self.portfolio_id = str(portfolio_id or "")
        # 跟单起点：这个时刻之前的成交（含带单员原有仓位）永久不算信号。
        # 用持久化的时间锚点，而不是只靠 seen —— seen 有 TTL，淘汰后历史
        # 事件会重新变成「新信号」，换人后就会把他的存量仓位开进来。
        self.follow_start_wall = 0.0
        self.needs_baseline = False
        # 跟单前他就持有、且至今未平的仓位：按他的开仓价挂更优价限价单，
        # 只有价格回到那个位置或更优才成交，不追高；已平掉的不跟。
        self.follow_history_positions = True
        # key -> 首次见到的 monotonic 时刻；配合 _seen_order 做有界淘汰
        self.seen: dict[str, float] = {}
        self._seen_order: deque[str] = deque()
        self.pending: dict[str, PendingOrder] = {}
        # 带单员等效仓位账本：(品种, 方向) -> 他还持有的数量。
        # 只统计本程序开始跟踪后的 OPEN/CLOSE（程序本来就不补开历史单）。
        # 用于把「他平掉多少比例」换算成我方该平多少，存进 state.db。
        self.ledger: dict[tuple[str, str], float] = {}
        # 「按事件累计已安排开仓量」。与真实持仓刻意分开：
        #   - 下单成功就计入（含未成交挂单），避免同一笔目标重复挂；
        #   - 带单员平仓时按比例缩减；
        #   - **用户手动平仓不动它** —— 这正是「我平掉的不再被补回来」的关键。
        # 目标累计 - 本表 = 还差多少要开。表里记的是「跟了多少」，
        # 不是「现在持多少」。
        self.filled: dict[tuple[str, str], float] = {}
        # 每个品种「上一次读到的持仓量」。只用来判断「持仓有没有减少」，
        # 从而区分「旧挂单没成交」和「用户手动平了仓」。不落库：重启后
        # 一律按 0 起算，宁可保守（不扣账本）也不重复开仓。
        self._pos_seen: dict[str, float] = {}
        # 带单员钱包余额推算（不含未实现盈亏）：敞口比例的分母。
        # 用 marginBalance 直接做分母，他浮亏越深比例越大，等于被动加杠杆；
        # 这里存剥掉浮亏后的钱包余额，只在已实现盈亏和出入金时变化。
        self.lead_wallet = 0.0
        self.wallet_state = None
        self._wallet_anchored = False
        self.fund_changes: deque[dict] = deque(maxlen=100)
        # 已计入 wallet 归因的 CLOSE 事件 key，防 _unsee 重试重复累加
        self._pnl_counted: deque[str] = deque(maxlen=1000)
        # actions 是定长 deque，装满后 len() 恒等于 maxlen —— 用「开始前的
        # 长度」切片会永久返回空列表，动作流和账本就断供了。这里改用单调
        # 序号：每 append 一次 +1，取「序号大于起点」的那些。
        self._action_seq = 0
        self.actions: deque[dict] = deque(maxlen=500)
        self.log = logger or logging.getLogger(__name__)
        self.store = store
        self.resume_cutoff = 0.0
        if store is not None:
            self._restore()

    @staticmethod
    def _stamp(ts: str) -> float:
        """流水时间戳 -> 本地挂钟秒；解析失败返回 0（不做年龄过滤）。"""
        try:
            return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            return 0.0

    def _restore(self) -> None:
        """重启后恢复去重水位与在途挂单；换带单员时重置跟单起点。"""
        now_wall = time.time()
        mono = time.monotonic()
        saved_pid = str(self.store.get("lead_portfolio_id", "") or "")
        changed = bool(self.portfolio_id) and saved_pid != self.portfolio_id
        if changed:
            # 换人 / 首次跟这个人：他账上原有的仓位不是我们的开仓信号。
            self.store.clear_seen()
            self.store.set("ledger_json", "")
            self.store.set("filled_json", "")
            self.store.set("last_run_wall", "0")
            self.store.set("follow_start_wall", "0")
            self.store.set("lead_portfolio_id", self.portfolio_id)
            self.needs_baseline = True
            self.log.info("跟单目标 %s -> %s：重置起点，历史仓位不跟",
                          saved_pid or "无", self.portfolio_id)
        else:
            self.follow_start_wall = float(
                self.store.get("follow_start_wall", 0) or 0)
            if self.portfolio_id and not saved_pid:
                self.store.set("lead_portfolio_id", self.portfolio_id)
            self.needs_baseline = not bool(
                float(self.store.get("last_run_wall", 0) or 0))
        ttl = max(600.0, float(getattr(self.rule, "max_signal_age_s", 900)) * 2.0)
        self.store.prune_seen(now_wall - ttl)
        for k, wall in self.store.load_seen().items():
            self.seen[k] = mono - max(0.0, now_wall - float(wall))
            self._seen_order.append(k)
        for row in self.store.load_pending():
            age = max(0.0, now_wall - float(row["created_wall"]))
            self.pending[row["symbol"]] = PendingOrder(
                symbol=row["symbol"], order_id=row["order_id"],
                side=row["side"], qty=float(row["qty"]),
                limit=float(row["limit"]), leverage=int(row["leverage"]),
                created_at=mono - age, source_key=row["source_key"] or "",
                historical=bool(row.get("historical", 0)))
        self._load_ledger()
        self._load_filled()
        self.lead_wallet = float(self.store.get("lead_wallet", 0) or 0)
        last = float(self.store.get("last_run_wall", 0) or 0)
        # 补单窗口 = 信号有效期。last 只用来区分首次运行，不能当下界，
        # 否则上次运行同一秒内产生、还没来得及处理的事件会被误吞。
        max_age = float(getattr(self.rule, "max_signal_age_s", 900))
        self.resume_cutoff = now_wall - max_age if last else 0.0
        self.log.info("状态已恢复：seen=%d pending=%d ledger=%d last_run=%s",
                      len(self.seen), len(self.pending), len(self.ledger),
                      f"{last:.0f}" if last else "首次运行")

    def _load_ledger(self) -> None:
        raw = self.store.get("ledger_json", "") if self.store else ""
        if not raw:
            return
        try:
            for k, v in json.loads(raw).items():
                symbol, side = str(k).split("|", 1)
                self.ledger[(symbol, side)] = float(v)
        except (ValueError, TypeError, AttributeError):
            self.log.warning("仓位账本解析失败，本轮平仓退化为整仓平")

    def _load_filled(self) -> None:
        raw = self.store.get("filled_json", "") if self.store else ""
        if not raw:
            return
        try:
            for k, v in json.loads(raw).items():
                symbol, side = str(k).split("|", 1)
                self.filled[(symbol, side)] = float(v)
        except (ValueError, TypeError, AttributeError):
            self.log.warning("已跟开仓账本解析失败，按 0 起算")

    def _set_filled(self, symbol: str, side: str, value: float) -> None:
        key = (symbol, side)
        v = max(0.0, float(value))
        if v <= 1e-12:
            self.filled.pop(key, None)
        else:
            self.filled[key] = v
        self._save_filled()

    def _save_filled(self) -> None:
        if self.store:
            self.store.set("filled_json", json.dumps(
                {f"{s}|{side}": round(v, 12)
                 for (s, side), v in self.filled.items() if abs(v) > 1e-12}))

    def _filled_of(self, symbol: str, side: str) -> float:
        return max(0.0, float(self.filled.get((symbol, side), 0.0)))

    def _add_filled(self, symbol: str, side: str, delta: float) -> None:
        key = (symbol, side)
        v = max(0.0, self.filled.get(key, 0.0) + float(delta))
        if v <= 1e-12:
            self.filled.pop(key, None)
        else:
            self.filled[key] = v
        self._save_filled()

    def _save_ledger(self) -> None:
        if self.store:
            self.store.set("ledger_json", json.dumps(
                {f"{s}|{side}": v for (s, side), v in self.ledger.items()}))

    def _update_wallet(self, ws, realized_delta: float = 0.0) -> dict | None:
        """更新带单员钱包余额，并识别出入金。

        钱包余额已经剔除了未实现盈亏，所以它只在两种情况下变：已实现盈亏、
        出入金。realized_delta 是本轮新处理 CLOSE 的已实现盈亏，那属于交易
        结果不是出入金，比较前先扣掉，否则每次平仓都会误报「保证金变动」。

        口径切换（空仓 <-> 有仓）那一轮不比较：推算值和精确值不可比。
        """
        prev = self.lead_wallet
        prev_anchored = self._wallet_anchored
        self.wallet_state = ws
        self._wallet_anchored = ws.anchored
        if ws.wallet > 0:
            self.lead_wallet = ws.wallet
            if self.store:
                self.store.set("lead_wallet", f"{ws.wallet:.8f}")

        # 只在「上一轮和这一轮都是空仓」时比较。
        # 持仓期间 wallet 是推算值，而推算只覆盖我们看得见的仓位 ——
        # 带单本 trade-history 一次只返回最近一个仓位的成交，他实际同时持有
        # 九个品种时我们只算得到一个的浮盈，差额全被当成「出入金」，
        # 于是每次价格波动都凭空播一条「资金变动」。宁可漏报，不能误报。
        if not ws.anchored or not prev_anchored:
            return None
        if prev <= 0 or ws.wallet <= 0:
            return None
        delta = ws.wallet - prev - realized_delta
        # 阈值：绝对值 50 USDT 或 0.1%，挡住手续费和推算抖动
        if abs(delta) < max(50.0, prev * 0.001):
            return None
        rec = {"type": "FUND_CHANGE", "amount": round(delta, 8),
               "wallet_before": round(prev, 8),
               "wallet_after": round(ws.wallet, 8),
               "margin_balance": round(ws.margin_balance, 8),
               "unrealized": round(ws.unrealized, 8),
               "anchored": ws.anchored, "estimate": not ws.anchored,
               "wall": round(time.time(), 3)}
        self.fund_changes.append(rec)
        self._record(rec)
        self.log.info("带单员资金变动 %+.4f USDT（钱包 %.4f -> %.4f，%s）",
                      delta, prev, ws.wallet,
                      "空仓精确" if ws.anchored else "持仓推算")
        return rec

    def begin_following(self, portfolio_id: str) -> None:
        """运行中换带单员：重置起点、清账本与水位，首轮重新建立基线。"""
        self.portfolio_id = str(portfolio_id)
        self.follow_start_wall = 0.0      # 由基线轮按窗口内最新成交时间确定
        self.seen.clear()
        self._seen_order.clear()
        self.ledger.clear()
        self.filled.clear()
        # 旧目标的挂单必须撤掉：historical 单永不超时，几小时后成交就是
        # 一笔账本为 0 的无人管理仓位。
        for _sym, _p in list(self.pending.items()):
            try:
                self.executor.binance.cancel_all_orders(_sym)
            except Exception as e:
                self.log.warning("换人时撤单失败 %s：%s", _sym, e)
        self.pending.clear()
        self.resume_cutoff = 0.0
        self.needs_baseline = True
        # 换人后旧的钱包余额和出入金记录都不属于新目标，一起清掉
        self.lead_wallet = 0.0
        self.wallet_state = None
        self._wallet_anchored = False
        self.fund_changes.clear()
        if self.store:
            self.store.clear_seen()
            self.store.set("ledger_json", "")
            self.store.set("filled_json", "")
            self.store.set("lead_wallet", "0")
            self.store.set("last_run_wall", "0")
            self.store.set("follow_start_wall", "0")
            self.store.set("lead_portfolio_id", self.portfolio_id)
        self.log.info("切换跟单目标 %s：历史仓位不跟，下轮建立基线",
                      self.portfolio_id)

    def drain(self, fills: list[Fill]) -> list[Fill]:
        ordered = sorted(fills, key=lambda f: (f.ts, f.action != "OPEN"))
        now = time.monotonic()
        # 运行中也要挡超龄 OPEN：接口偶发延迟时，一条几小时前的开仓会带着
        # 旧价格进窗口，拿去挂限价等于在完全错的位置接盘。CLOSE 不受此限，
        # 撤单/平仓是保护动作。0 = 关闭该检查（自检里用固定历史时间戳）。
        max_age = float(getattr(self.rule, "signal_max_age_s", 0) or 0)
        age_cutoff = time.time() - max_age if max_age > 0 else 0.0
        ttl = max(600.0, float(getattr(self.rule, "max_signal_age_s", 900)) * 2.0)
        expired = []
        while self._seen_order and now - self.seen.get(self._seen_order[0], now) > ttl:
            expired.append(self._seen_order.popleft())
        for k in expired:
            self.seen.pop(k, None)
        if self.store and expired:
            self.store.drop_seen(expired)

        fresh, swallowed = [], []
        for f in ordered:
            stamp = self._stamp(f.ts)
            # 跟单起点之前的成交一律忽略：带单员原有的仓位、换人前的旧流水
            # 都不是新信号。这个判定不依赖 seen，TTL 淘汰后也不会复活。
            if (self.follow_start_wall and stamp
                    and stamp <= self.follow_start_wall):
                continue
            if f.key in self.seen:
                continue
            if (age_cutoff and stamp and stamp < age_cutoff
                    and f.action == "OPEN"):
                swallowed.append(f.key)
                self.seen[f.key] = now
                self._seen_order.append(f.key)
                # 必须留痕：静默吞掉信号的话，用户只会看到「他没跟单」，
                # 事后翻日志也查不出是取数晚了还是引擎判断不跟。
                self.log.warning(
                    "跳过超龄开仓信号 %s %s（%s，已过 %.0f 分钟，"
                    "超过 %d 分钟有效期），不拿旧价挂单",
                    f.symbol, f.side, f.ts, (time.time() - stamp) / 60.0,
                    max_age / 60.0)
                continue
            if (self.resume_cutoff and stamp and stamp < self.resume_cutoff
                    and f.action == "OPEN"):
                # 过老的开仓信号只记水位、不补开，避免重启后补一堆历史单。
                # CLOSE 一律放行：撤单/平仓是保护动作。关掉程序期间带单员
                # 止损了，重启后若把这条也吞掉，我方就留下一笔无人管理的
                # 仓位或挂单。_close() 本身幂等，重复执行只会撤单。
                swallowed.append(f.key)
                self.seen[f.key] = now
                self._seen_order.append(f.key)
                self.log.warning(
                    "重启补单窗口外的开仓信号 %s %s（%s），只记水位不补开",
                    f.symbol, f.side, f.ts)
                continue
            fresh.append(f)
        for f in fresh:
            self.seen[f.key] = now
            self._seen_order.append(f.key)
        if self.store and (fresh or swallowed):
            self.store.mark_seen([f.key for f in fresh] + swallowed)
        return fresh

    def _unsee(self, key: str) -> None:
        """把下单失败的事件撤回 seen，下一轮重新尝试。

        _seen_order 里留一个陈旧条目无害：drain() 淘汰时用 seen.get(k, now)，
        并且不会重复 append（同一 key 下次进 fresh 时会再 append 一次，
        最多多一次空转淘汰）。
        """
        self.seen.pop(key, None)
        if self.store:
            self.store.drop_seen([key])

    def _record(self, action: dict) -> dict:
        """写入动作流并盖单调序号。所有新增动作都必须走这里。"""
        self._action_seq += 1
        action["_no"] = self._action_seq
        self.actions.append(action)
        return action

    def _actions_since(self, start_no: int) -> list[dict]:
        """取本轮新增的动作，去掉内部序号后返回。"""
        return [{k: v for k, v in a.items() if k != "_no"}
                for a in self.actions if a.get("_no", 0) > start_no]

    @staticmethod
    def _sym_side(order_side: str) -> str:
        """挂单方向（BUY/SELL）→ 持仓方向（LONG/SHORT）。"""
        return "LONG" if str(order_side).upper() == "BUY" else "SHORT"

    def _entry_side(self, side: str) -> str:
        return "BUY" if side == "LONG" else "SELL"

    def _open(self, f: Fill, lead_notional: float | None = None,
              lead_avg_price: float | None = None,
              historical: bool = False, tag: str = "") -> bool:
        symbol = f.symbol
        # 无论旧挂单还在不在，都要读实际持仓再补差额：旧单可能已全部成交
        # （reconcile 清掉 pending）或部分成交，按全量重挂会重复开。
        have = 0.0
        try:
            positions = self.executor.binance.get_positions()
            have = next((abs(float(p.get("quantity", 0))) for p in positions
                         if p["symbol"] == symbol), 0.0)
        except Exception as e:
            self.log.warning("%s 读取持仓失败，按未成交处理：%s", symbol, e)
            have = 0.0
        if symbol in self.pending:
            old = self.pending.pop(symbol, None)
            if old:
                try:
                    self.executor.binance.cancel_order(symbol, old.order_id)
                except Exception as e:
                    # 撤不掉就不能再挂新的：两张单叠加、全部成交会超配。
                    # 事件退回队列，下一轮重试撤单。
                    self.log.error("%s 撤旧挂单失败，本轮不重挂：%s",
                                   symbol, e)
                    self._unsee(f.key)
                    return False
                if self.store:
                    self.store.remove_pending(symbol)
                # 撤掉旧单后要不要把 old.qty 从账本里扣回来，取决于
                # 「持仓有没有非预期地减少」：
                #   - 持仓没少（含一直是 0）→ 旧单只是没成交，扣回去，
                #     这样本轮能按累计目标重挂，不会长期少开；
                #   - 持仓比上轮少 → 用户手动平过仓，**不能扣**，否则
                #     他下次加仓会把用户平掉的那份重新开回来。
                # 上轮持仓 `self._pos_seen` 在每次 _open 读持仓时更新。
                prev_pos = float(self._pos_seen.get(symbol, 0.0))
                if have + 1e-9 >= prev_pos:
                    self._add_filled(symbol, self._sym_side(old.side),
                                     -float(old.qty))
            self.log.info("%s 加仓：撤旧挂单，已成交 %.6f，补差额", symbol, have)

        # lead_notional / lead_avg_price 由 process() 用增量账本传入：
        # 同品种多次开仓时是累计值，历史未平仓位是该批次的原始值。
        signal = lead_avg_price if lead_avg_price else f.price
        notional = lead_notional if lead_notional else f.notional
        tick = self.executor.price_step_of(symbol)

        # 等比：份额一律走 `target_position()`，和「仓位管理」页、
        # 成交事件表的「应开」同一个函数。这里原来自己按
        # `floor(my_notional / limit / step)` 算了一遍名义口径 —— 限价偏离
        # 他的均价的幅度会全部变成数量偏差，界面上显示的应开数量就不是
        # 实盘真正会下的数量，等于让排查页骗人。
        base = self.lead_wallet if self.lead_wallet > 0 else self.rule.lead_balance
        _, qty_precision = self.executor.binance.get_symbol_precision(symbol)
        qty_step = 10.0 ** (-qty_precision) if qty_precision >= 0 else 1e-8
        lead_qty = (notional / signal) if signal > 0 else float(f.qty)
        t = target_position(notional, lead_qty, f.side, base,
                            self.rule.my_equity, tick, qty_step, self.rule)
        limit, target_qty, ratio = t["limit"], t["target_qty"], t["ratio"]
        # 差额按「事件累计已跟量」算，不看真实持仓：
        #   他开 100U 我跟了 X；我手动平掉 → 账本仍是 X，不会被补回来；
        #   他再开 100U → 目标 2X、账本 X，只补 X。
        # 真实持仓比账本多（用户手动加仓）时把差额并进账本，
        # 否则下一笔会按「目标 - 账本」再开一遍，等于重复。
        book = self._filled_of(symbol, f.side)
        # 只有「同向持仓」才能并进账本。反手持仓（手动反手/遗留仓）如果被
        # 当成已跟量，qty 会算成 0，该品种从此不再跟随。
        have_side = next((str(p.get("side") or "").upper() for p in positions
                          if p["symbol"] == symbol), "")
        self._pos_seen[symbol] = have
        if have > book + 1e-12 and have_side == f.side:
            self._add_filled(symbol, f.side, have - book)
            book = self._filled_of(symbol, f.side)
        qty = self.executor.binance.format_quantity(
            symbol, max(0.0, target_qty - book))
        if qty <= 0:
            if book > 0:
                self.log.info("%s 已按事件跟满 %.6f 张，不重复挂单",
                              symbol, book)
                return True
            self.log.warning("%s 计算数量 <= 0，跳过", symbol)
            return False

        leverage = self.rule.leverage
        try:
            self.executor.binance.set_leverage(symbol, leverage)
        except Exception as e:
            # 品种最大杠杆低于设定值时交易所会拒（-4028）。不能静默：
            # 用户以为用了 50x，实际还是交易所默认，保证金算错。
            self.log.warning("%s 设置 %dx 杠杆失败，沿用交易所现值：%s",
                             symbol, leverage, e)
            leverage = 0
        self.executor.binance.set_margin_mode(symbol, self.rule.margin_type)
        client_id = (ORDER_TAG + ("hist_" if historical else "")
                     + symbol.lower()[:8] + "_" + uuid.uuid4().hex[:10])
        order = self.executor.binance.place_limit_order(
            symbol, self._entry_side(f.side), qty, limit,
            client_order_id=client_id)
        if not order:
            # 网络断了 / 交易所没确认：这条事件不能算处理完，否则 seen 会把它
            # 永久吞掉，网络恢复后也不会补开。放回队列等下一轮。
            self._unsee(f.key)
            self.log.warning("%s 下单未确认，事件保留，下一轮自动补开", symbol)
            return False

        mode = self.executor.price_mode_text()
        self._add_filled(symbol, f.side, qty)
        self.pending[symbol] = PendingOrder(
            symbol=symbol, order_id=order.get("order_id"),
            side=self._entry_side(f.side), qty=qty, limit=limit,
            leverage=leverage, created_at=time.monotonic(),
            source_key=(tag + ":" if tag else
                        "hist:" if historical else "") + f.key,
            historical=historical)
        if self.store:
            self.store.upsert_pending(self.pending[symbol])
        self._record({"type": "ENTRY", "symbol": symbol,
                             "side": f.side, "qty": qty, "limit": limit,
                             "signal": signal, "leverage": leverage,
                             "price_mode": mode, "historical": historical,
                             "lead_notional": round(notional, 2),
                             "notional_ratio": round(ratio, 6)})
        # 与 ACTION 行重复，只在 DEBUG 留底；界面看 ACTION 就够了
        self.log.debug("开仓跟随 %s %s qty=%s limit=%s (基准 %s, 名义 %.2f, %s%s)",
                      symbol, f.side, qty, limit, signal, notional, mode,
                      "，历史未平仓位" if historical else "")
        return True

    def _close(self, f: Fill, ratio: float = 1.0,
               reason: str = "带单员平仓") -> bool:
        # 关键：无条件撤单。入场限价单常常还没成交对方就已止损，
        # 不撤掉它，价格回落时会自动成交成一笔无人管理的幽灵仓位。
        # 与入场价用更优价还是容差价完全无关，这一行两种模式都必须执行。
        symbol = f.symbol
        # 撤单失败必须让事件回到队列：否则 CLOSE 已进 seen、下轮不再处理，
        # 交易所却还挂着入场单，价格回落就成交成无人管理的幽灵仓位。
        try:
            cancelled = bool(self.executor.binance.cancel_all_orders(symbol))
        except Exception as e:
            cancelled = False
            self.log.error("%s 撤单异常：%s", symbol, e)
        if not cancelled:
            self._unsee(f.key)
            self.log.error("%s 撤单失败，平仓事件保留，下轮重试", symbol)
            return False
        had_pending = self.pending.pop(symbol, None) is not None
        if self.store:
            self.store.remove_pending(symbol)
        ratio = min(1.0, max(0.0, float(ratio)))

        try:
            positions = self.executor.binance.get_positions()
        except Exception as e:
            # get_positions 失败不能当成「无仓位」，否则只撤单不平仓
            self._unsee(f.key)
            self.log.error("%s 读取持仓失败，平仓事件保留，下轮重试：%s",
                           symbol, e)
            return False
        my_qty = next((abs(float(p.get("quantity", 0))) for p in positions
                       if p["symbol"] == symbol), 0.0)
        has_position = my_qty > 0
        qty_to_close = None
        if has_position and ratio < 1.0 - 1e-9:
            _, qp = self.executor.binance.get_symbol_precision(symbol)
            step = 10.0 ** (-qp)
            qty_to_close = self.executor.binance.format_quantity(
                symbol, floor(my_qty * ratio / step) * step)
            if qty_to_close <= 0:
                qty_to_close = None        # 数量不足最小步进，整仓平掉
                ratio = 1.0
        if has_position:
            try:
                closed_ok = bool(self.executor.close_position(
                    symbol, reason=reason, quantity=qty_to_close))
            except Exception as e:
                closed_ok = False
                self.log.error("%s 平仓下单异常：%s", symbol, e)
            if not closed_ok:
                self._unsee(f.key)
                self.log.error("%s 平仓失败，事件保留，下轮重试", symbol)
                return False
        # close_qty 必须写**实际平掉的数量**：整仓平时 API 参数是 None
        # （表示「全部」），但账本要的是数字 —— 写 None 会让盈亏账本这一笔
        # 记成 0，整本已实现盈亏永远长不大。
        # side 也必须在场：下游按方向定符号，空字符串会被当成空头。
        # 带单员平掉 ratio，我方「已跟量」也按同比例缩 —— 否则他平完
        # 再开同类仓位时目标会算高，多开一份。
        book = self._filled_of(symbol, f.side)
        if book > 0:
            self._add_filled(symbol, f.side, -book * ratio)
        self._record({"type": "CLOSE_FOLLOW", "symbol": symbol,
                             "side": f.side,
                             "cancelled_pending": had_pending,
                             "our_position": has_position,
                             "cancel_only": not has_position,
                             "close_ratio": round(ratio, 6),
                             "close_qty": (None if not has_position else
                                           (qty_to_close if qty_to_close
                                            is not None else my_qty))})
        if has_position:
            self.log.debug("平仓跟随 %s：比例=%.2f%%，平仓量=%s，"
                           "未成交入场单=%s",
                          symbol, ratio * 100,
                          qty_to_close if qty_to_close is not None else "全部",
                          had_pending)
        else:
            self.log.debug("仅撤单 %s：本程序无仓位，带单员平仓未产生平仓动作"
                           "（已撤挂单=%s）", symbol, "是" if had_pending else "否")
        return has_position

    def _lots_after_start(self, symbol: str, side: str) -> bool:
        """该品种是否有「跟单起点之后」开的存活批次。"""
        rep = self._replay
        if rep is None or not self.follow_start_wall:
            return False
        for lot in rep.surviving:
            if (lot.symbol == symbol and lot.side == side
                    and self._stamp(lot.ts) > self.follow_start_wall):
                return True
        return False

    def rebalance(self) -> int:
        """按账本把我方仓位补到目标 —— 只补不减。

        为什么需要它：引擎只在「新 OPEN 事件」时下单。一条事件如果被漏掉
        （超龄吞掉、下单被拒、程序没跑、数据源截断），账本是对的但仓位永久偏小，
        而且不会有任何后续事件来纠正。这里每轮拿账本目标和我方真实持仓比一次，
        差多少补多少。

        三条防线：
        - 该品种已有挂单就跳过，不重复下单；
        - 差额名义不到 max(5 USDT, 目标×5%) 就不动，避免 wallet 推算抖动
          引起反复调仓；
        - 我方方向和他相反时不自动反手，只记一条警告 —— 反手是另一套决策。
        """
        if not getattr(self.rule, "rebalance", True):
            return 0
        base = self.lead_wallet if self.lead_wallet > 0 else self.rule.lead_balance
        if base <= 0 or self.rule.my_equity <= 0 or self._replay is None:
            return 0
        try:
            positions = self.executor.binance.get_positions()
        except Exception as e:
            self.log.warning("对账补差：读取持仓失败，本轮跳过：%s", e)
            return 0
        mine = {p["symbol"]: (abs(float(p.get("quantity", 0))), p["side"])
                for p in positions}
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        done = 0
        for (symbol, side), t in self._replay.totals.items():
            qty = float(t.get("qty") or 0.0)
            notional = float(t.get("notional") or 0.0)
            if qty <= 0 or notional <= 0 or symbol in self.pending:
                continue
            have_qty, have_side = mine.get(symbol, (0.0, side))
            book = self._filled_of(symbol, side)
            # 真实持仓比账本多：外部加仓并进账本，避免重复开
            if have_qty > book + 1e-12 and have_side == side:
                self._add_filled(symbol, side, have_qty - book)
                book = self._filled_of(symbol, side)
            # 从没跟过这个方向、且存活批次都在跟单起点之前 → 不补
            if book <= 0 and not self._lots_after_start(symbol, side):
                # 跟单起点之前就持有的批次不算「漏单」—— 那是他的历史仓位，
                # 要不要镜像由 follow_history_positions 决定，不在这里补。
                continue
            if have_qty > 0 and have_side != side:
                self.log.warning("对账补差：%s 本程序为 %s、带单员为 %s，"
                                 "不自动反手",
                                 symbol, have_side, side)
                continue
            avg = notional / qty
            try:
                tick = self.executor.price_step_of(symbol)
                _, qp = self.executor.binance.get_symbol_precision(symbol)
            except Exception:
                continue
            step = 10.0 ** (-qp)
            if tick <= 0 or step <= 0:
                continue
            t = target_position(notional, qty, side, base,
                                self.rule.my_equity, tick, step, self.rule)
            limit = t["limit"]
            target_notional = t["target_notional"]
            target_qty = t["target_qty"]
            # 补差按「目标 - 已跟账本」：手动平掉的仓位账本没减，
            # 所以这里 gap 为 0，不会把用户平掉的仓偷偷补回来。
            gap = target_qty - book
            if gap <= 0:
                continue
            if gap * limit < max(5.0, target_notional * 0.05):
                continue
            synth = Fill(ts=now_ts, action="OPEN", side=side, symbol=symbol,
                         price=avg, qty=qty, notional=notional)
            self.log.info("对账补差 %s %s：目标 %s 张，已跟 %s 张，补 %s 张",
                          symbol, side, target_qty, book, gap)
            if self._open(synth, lead_notional=notional, lead_avg_price=avg,
                          tag="rebal"):
                done += 1
        return done

    def rebuild_from_exchange(self) -> bool:
        """把交易所里本程序挂的单反向登记进 pending（每轮 process 都跑）。

        state.db 丢了、换机器、或者上次退出时没来得及写库，本地 pending 会是空的，
        但交易所那笔限价单还挂着。不认回来就没人管它：超时撤单不会触发，
        带单员平仓时虽然 _close() 会无条件撤单兜底，但在这之前它一直敞着。

        只接管 clientOrderId 带 ORDER_TAG 前缀的单，账户里手动挂的单一律不动。
        重建的单 created_at 从现在算起（交易所不返回挂单创建时间），
        所以超时撤单会晚一个 fill_timeout_s，这是可接受的代价。
        """
        try:
            opens = self.executor.binance.get_open_orders()
        except Exception as e:
            # 每轮都会重试，别把日志刷爆：只报第一次和状态翻转。
            if not getattr(self, "_reconcile_warned", False):
                self.log.warning("挂单对账失败，后续自动重试：%s", e)
                self._reconcile_warned = True
            return False
        self._reconcile_warned = False
        if not isinstance(opens, list):
            return False
        now = time.monotonic()
        adopted = []
        for o in opens:
            cid = str(o.get("client_order_id") or "")
            if not cid.startswith(ORDER_TAG):
                continue
            symbol = o.get("symbol")
            if not symbol or symbol in self.pending:
                continue
            hist = cid.startswith(ORDER_TAG + "hist_")
            p = PendingOrder(
                symbol=symbol, order_id=o.get("order_id"),
                side=str(o.get("side") or ""),
                qty=float(o.get("quantity") or 0),
                limit=float(o.get("price") or 0),
                leverage=self.rule.leverage, created_at=now,
                source_key="hist:rebuilt" if hist else "",
                historical=hist)
            self.pending[symbol] = p
            if self.store:
                self.store.upsert_pending(p)
            adopted.append(symbol)
        if adopted:
            self._record({"type": "RECONCILE_ADOPT",
                                 "symbols": adopted})
            self.log.info("挂单对账：接管 %d 笔本程序遗留挂单 %s",
                          len(adopted), ",".join(adopted))
        return True

    def reconcile(self) -> None:
        now = time.monotonic()
        timeout = float(getattr(self.rule, "entry_timeout_s", 300))
        for symbol, p in list(self.pending.items()):
            try:
                opens = self.executor.binance.get_open_orders(symbol)
            except Exception:
                continue
            # 库里 order_id 存 TEXT、交易所返回 int，必须统一成 str 比较，
            # 否则每轮都误判「单已不在」并反复刷新计时。
            still_open = any(str(o.get("order_id")) == str(p.order_id)
                             for o in opens)
            if not still_open:
                try:
                    positions = self.executor.binance.get_positions()
                except Exception as e:
                    # 读不到持仓就下轮再说：这里抛出去会把本批已标 seen 的
                    # OPEN/CLOSE 全部吞掉（事件不会再回来）。
                    self.log.warning("%s 挂单结算时读持仓失败，下轮重试：%s",
                                     symbol, e)
                    continue
                filled = any(x["symbol"] == symbol for x in positions)
                self.pending.pop(symbol, None)
                if self.store:
                    self.store.remove_pending(symbol)
                # 单子离开挂单簿。这里只处理**部分成交**：持仓还在，
                # 把账本对齐到实际拿到的量，避免「账本永远偏高、后续不再补」。
                #
                # 持仓为 0 时**不动账本**，这一点是刻意的：那种情况分不清是
                # 「委托从没成交」还是「成交后被用户手动平掉」，而后者必须
                # 保住账本 —— 否则下一轮 rebalance 就会把他之前开的仓
                # 重新补回来，正是用户明确禁止的低级错误。
                # 代价是「从未成交」的委托会让账本偏高（后续少开），
                # 方向偏保守：宁可少开，不能多开。
                # 结算时**不**用真实持仓覆盖账本：成交后被用户平掉一部分
                # （30 秒窗口内完全可能），覆盖会把那部分从账本里抹掉，
                # 下一轮就按「应开 - 账本」把它重新开回来。
                # 与上一条同源：账本记「应持」，真实持仓只用于开仓时的
                # 同向校验，两者不互相覆盖。
                self._record({"type": "ENTRY_SETTLED", "symbol": symbol,
                                     "filled": filled})
                continue
            if p.historical:
                # 历史未平仓位等的是几小时前的开仓价，不能按 300s 超时撤；
                # 他平仓时 CLOSE 会无条件撤单。
                continue
            if timeout > 0 and now - p.created_at > timeout:
                self.executor.binance.cancel_order(symbol, p.order_id)
                self.pending.pop(symbol, None)
                if self.store:
                    self.store.remove_pending(symbol)
                self._add_filled(symbol, self._sym_side(p.side), -p.qty)
                self._record({"type": "ENTRY_TIMEOUT", "symbol": symbol})
                # 更优价挂单本来就可能挂不上（价格没回到更优的位置）。
                # 说清后果：这条信号已经标记已见，不会重试，这一单就是漏了；
                # 但他下次加仓时会按累计名义补差额，所以不是永久错位。
                self.log.warning(
                    "入场限价单 %s 挂 %.8g 超过 %d 秒没成交，已撤单 —— "
                    "这一单算漏了（价格没回到更优的位置），"
                    "带单员下次加仓时会按累计名义一次性补齐",
                    symbol, p.limit, timeout)

    def _historical_groups(self, rep) -> dict:
        """把重放后仍存活的历史批次按 (品种,方向) 汇总成可跟的一笔。"""
        groups: dict[tuple[str, str], dict] = {}
        if not self.follow_start_wall:
            # 没有跟单锚点（自检、首次启动）就不存在「历史仓位」这个概念，
            # 否则会把窗口里所有存活批次都当成历史，导致开仓量翻倍。
            return groups
        for lot in rep.surviving:
            if self._stamp(lot.ts) > self.follow_start_wall:
                continue
            key = (lot.symbol, lot.side)
            g = groups.setdefault(key, {"qty": 0.0, "notional": 0.0,
                                        "ts": lot.ts})
            frac = lot.left / lot.qty if lot.qty else 0.0
            g["qty"] += lot.left
            g["notional"] += lot.notional * frac
        return groups

    def process(self, fills: list[Fill], lead_balance: float | None = None,
                marks: dict | None = None) -> dict:
        # 每轮对账，不只首轮：既覆盖重启恢复，也兜住「下单请求超时、交易所
        # 其实已经挂上，但本地没登记」的孤儿单。下单 POST 故意不重试，
        # 靠带 copy_ 前缀的 clientOrderId 在这里认回来。
        self.rebuild_from_exchange()
        rep = replay_lead(fills)
        self._replay = rep
        # 账本每轮由全量重放覆盖，不再靠增量累加（那样会漏历史仓、被加仓打乱）
        self.ledger = {k: t["qty"] for k, t in rep.totals.items()}
        self._save_ledger()
        start_no = self._action_seq

        # 敞口比例的分母用「钱包余额」而不是 marginBalance：后者含未实现
        # 盈亏，他浮亏越深分母越小，同一笔开仓算出的比例越大，跟单人会在
        # 最不该加仓的位置被动加杠杆。空仓时两者相等，是每次的校准点。
        balance = (float(lead_balance) if lead_balance is not None
                   else float(self.rule.lead_balance or 0.0))
        self._update_wallet(wallet_state(rep, balance, marks or {}), 0.0)

        if self.needs_baseline:
            self.needs_baseline = False
            stamps = [self._stamp(f.ts) for f in fills]
            stamps = [s for s in stamps if s > 0]
            self.follow_start_wall = max(stamps) if stamps else time.time()
            now = time.monotonic()
            keys = [f.key for f in fills]
            for k in keys:
                self.seen[k] = now
                self._seen_order.append(k)
            if self.store:
                self.store.set("follow_start_wall", str(self.follow_start_wall))
                self.store.set("last_run_wall", time.time())
                if keys:
                    self.store.mark_seen(keys)
            # 历史未平仓位：按他的原始开仓价挂更优价限价单。限价单天然只在
            # 价格回到该位置或更优时成交，不会追高；已平批次已被核销，不跟。
            opened = 0
            if self.follow_history_positions:
                for (symbol, side), g in self._historical_groups(rep).items():
                    avg = g["notional"] / g["qty"] if g["qty"] else 0.0
                    if avg <= 0:
                        continue
                    synth = Fill(ts=g["ts"], action="OPEN", side=side,
                                 symbol=symbol, price=avg, qty=g["qty"],
                                 notional=g["notional"])
                    if self._open(synth, lead_notional=g["notional"],
                                  lead_avg_price=avg, historical=True):
                        opened += 1
            self.rebalance()
            return {"events": 0, "opened": opened, "closed": 0,
                    "cancel_only": 0, "skipped": 0,
                    "pending": len(self.pending),
                    "actions": self._actions_since(start_no),
                    "signals": [],       # 基线轮不播报历史事件
                    "baseline": True, "baseline_count": len(fills)}

        events = self.drain(fills)
        # 已实现盈亏本身就会让钱包余额变化，那是交易结果不是出入金；
        # 扣掉它才能把「他动了保证金」分离出来。按 key 去重，
        # _unsee 重试的同一笔 CLOSE 不会重复计入。
        realized = 0.0
        for f in events:
            if (f.action == "CLOSE" and f.realized_pnl is not None
                    and f.key not in self._pnl_counted):
                realized += float(f.realized_pnl)
                self._pnl_counted.append(f.key)
        if realized:
            self._update_wallet(wallet_state(rep, balance, marks or {}),
                                realized)
        self.reconcile()
        opened = closed = cancel_only = skipped = 0
        for f in events:
            if f.action == "OPEN":
                # 累计量来自全量重放里「这笔 OPEN 发生当时」的持仓快照，
                # 跨轮加仓也准，不依赖本轮 seen 里剩了几个事件。
                # open_context 由全量重放得出，本身已含历史未平批次，
                # 不能再叠加一次历史名义，否则会把已挂过的仓重复开。
                ctx = rep.open_context.get(f.key, {})
                notional = ctx.get("notional", f.notional)
                avg = ctx.get("avg", f.price)
                if self._open(f, lead_notional=notional,
                              lead_avg_price=avg):
                    opened += 1
                else:
                    skipped += 1
            else:
                # 平仓比例来自全量重放：这笔 CLOSE 发生前他还持有多少。
                before = float(rep.close_before.get(f.key, 0.0))
                ratio = min(float(f.qty) / before, 1.0) if before > 1e-9 else 1.0
                if self._close(f, ratio=ratio):
                    closed += 1
                else:
                    cancel_only += 1
        if self.store:
            self.store.set("last_run_wall", time.time())
        self.rebalance()
        signals = [{"ts": f.ts, "action": f.action, "side": f.side,
                    "symbol": f.symbol, "price": round(f.price, 8),
                    "qty": f.qty, "notional": round(f.notional, 2),
                    "realized_pnl": f.realized_pnl}
                   for f in events]
        return {"events": len(events), "opened": opened, "closed": closed,
                "cancel_only": cancel_only, "skipped": skipped,
                "pending": len(self.pending),
                "actions": self._actions_since(start_no),
                "signals": signals}

    def state(self) -> dict:
        ws = self.wallet_state
        return {"pending": [
            {"symbol": p.symbol, "side": p.side, "qty": p.qty,
             "limit": p.limit, "leverage": p.leverage,
             "age_s": round(time.monotonic() - p.created_at, 1)}
            for p in self.pending.values()],
            "seen_events": len(self.seen),
            "lead_wallet": self.lead_wallet,
            "wallet": ({"margin_balance": ws.margin_balance,
                        "unrealized": ws.unrealized,
                        "wallet": ws.wallet,
                        "anchored": ws.anchored,
                        "missing": list(ws.missing),
                        "lots": list(ws.lots)} if ws else None),
            "fund_changes": list(self.fund_changes)}


class MemoryBroker:
    """内存交易所：dry-run 的模拟撮合，也是全部引擎自检的下单端。

    三种模式必须分清，混用会让「模拟仓一挂就成交」，等于骗人：

    - `unfilled=True`：任何单都不成交（测「挂了但没成交」的路径）；
    - `auto_fill=True`（默认）：限价单**立即按限价成交**。这是给自检用的
      确定性行为 —— 测的是引擎逻辑，不是撮合；
    - `auto_fill=False`：真实撮合语义。先挂进 `open_orders`，每轮用标记价
      `match_orders()` 判断能否成交：买单要 `mark <= 限价`、卖单要
      `mark >= 限价`。dry-run 界面必须用这个，否则「更优价」永远等不到
      也能成交，看着像是捡钱。
    """

    def __init__(self, unfilled: bool = False, fail_orders: bool = False,
                 auto_fill: bool = True):
        self.unfilled = unfilled
        self.fail_orders = fail_orders
        self.auto_fill = bool(auto_fill)
        self.open_orders: list[dict] = []
        self.positions: dict[str, float] = {}
        # 开仓均价。只记数量的话 dry-run 的持仓没有成本，浮盈永远算不出来，
        # 「跟单盈亏」页在模拟模式下就只能是空白。
        self.entry: dict[str, float] = {}
        self.calls: list[tuple] = []
        self._next_id = 1

    def _oid(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def get_symbol_precision(self, symbol):
        return 5, 3

    def format_quantity(self, symbol, quantity):
        return round(quantity, 3)

    def format_price(self, symbol, price):
        return round(price, 5)

    def set_leverage(self, symbol, leverage):
        self.calls.append(("set_leverage", symbol, leverage))
        return True

    def set_margin_mode(self, symbol, mode):
        self.calls.append(("set_margin_mode", symbol, mode))
        return True

    def _apply_fill(self, symbol: str, side: str, quantity: float,
                    price: float) -> None:
        """把一笔成交记进净持仓。

        累加而不是覆盖：真交易所是净持仓，覆盖会让「加仓只补差额」这类逻辑
        在自检里变成假的（have 永远是上一笔的量）。存带符号的净持仓
        （正=多、负=空），get_positions 再折成 side + 绝对值。
        """
        delta = quantity if side.upper() == "BUY" else -quantity
        prev = self.positions.get(symbol, 0.0)
        net = prev + delta
        if abs(net) < 1e-12:
            self.positions.pop(symbol, None)
            self.entry.pop(symbol, None)
            return
        same_side = abs(prev) < 1e-12 or (prev > 0) == (net > 0)
        old = self.entry.get(symbol, price)
        # 同向加仓按数量加权；反手（净持仓变号）以本次成交价为新成本
        self.entry[symbol] = (
            (abs(prev) * old + quantity * price) / (abs(prev) + quantity)
            if same_side else price)
        self.positions[symbol] = net

    def place_limit_order(self, symbol, side, quantity, price,
                          reduce_only=False, client_order_id=None):
        oid = self._oid()
        self.calls.append(("LIMIT", symbol, side, quantity, price))
        if self.fail_orders:
            return None
        if not self.unfilled and self.auto_fill:
            self._apply_fill(symbol, side, quantity, price)
        else:
            self.open_orders.append({"order_id": oid, "symbol": symbol,
                                     "side": side, "type": "LIMIT",
                                     "price": price, "quantity": quantity,
                                     "client_order_id": client_order_id or ""})
        return {"order_id": oid, "symbol": symbol, "side": side,
                "quantity": quantity, "price": price, "status": "NEW",
                "client_order_id": client_order_id or ""}

    def match_orders(self, marks: dict) -> list[str]:
        """按标记价撮合挂着的限价单，返回本轮成交的品种。

        语义和真交易所一致：**买单要价格跌到限价才成交**（`mark <= 限价`），
        **卖单要价格涨到限价才成交**（`mark >= 限价`）。「更优价」挂单本来
        就是等价格回到更好的位置，价格没到就不该成交 —— 一挂就成交的模拟仓
        会让人以为这套逻辑稳赚。
        """
        if self.unfilled or self.auto_fill or not self.open_orders:
            return []
        marks = marks or {}
        filled: list[str] = []
        keep: list[dict] = []
        for o in self.open_orders:
            mark = float(marks.get(o["symbol"]) or 0.0)
            buy = str(o["side"]).upper() == "BUY"
            hit = mark > 0 and (mark <= o["price"] if buy else mark >= o["price"])
            if not hit:
                keep.append(o)
                continue
            self._apply_fill(o["symbol"], o["side"], float(o["quantity"]),
                             float(o["price"]))
            self.calls.append(("FILL", o["symbol"], o["side"],
                               o["quantity"], o["price"]))
            filled.append(o["symbol"])
        self.open_orders = keep
        return filled

    def get_open_orders(self, symbol=None):
        return [o for o in self.open_orders
                if symbol is None or o["symbol"] == symbol]

    def cancel_all_orders(self, symbol):
        removed = [o for o in self.open_orders if o["symbol"] == symbol]
        self.open_orders = [o for o in self.open_orders if o["symbol"] != symbol]
        self.calls.append(("CANCEL_ALL", symbol, len(removed)))
        return True

    def cancel_order(self, symbol, order_id):
        self.open_orders = [o for o in self.open_orders
                            if not (o["symbol"] == symbol
                                    and str(o["order_id"]) == str(order_id))]
        return True

    def get_positions(self):
        # unrealized_pnl 给 None 而不是 0：模拟仓不知道标记价，交回调用方
        # 按「标记价 - 开仓价」算；写 0 会被当成「真的不赚不亏」。
        return [{"symbol": s, "side": "LONG" if q > 0 else "SHORT",
                 "quantity": abs(q),
                 "entry_price": self.entry.get(s, 0.0),
                 "unrealized_pnl": None, "leverage": 20}
                for s, q in self.positions.items() if q]

    def place_market_order(self, symbol, side, quantity, reduce_only=False):
        self.calls.append(("MARKET", symbol, side, quantity))
        return {"order_id": self._oid(), "symbol": symbol}

    def close_position(self, symbol, quantity=None):
        have = self.positions.get(symbol, 0.0)
        qty = abs(have) if quantity is None else min(abs(have), float(quantity))
        left = abs(have) - qty
        if left > 1e-9:
            self.positions[symbol] = left if have > 0 else -left
        else:
            self.positions.pop(symbol, None)
            self.entry.pop(symbol, None)
            self.open_orders = [o for o in self.open_orders
                                if o["symbol"] != symbol]
        self.calls.append(("CLOSE", symbol, qty))
        return True


def _fill(action: str, ts: str, price: float = 68000.0,
          qty: float = 1.5, symbol: str = "BTCUSDT", side: str = "LONG") -> Fill:
    return Fill(ts=ts, action=action, side=side, symbol=symbol,
                price=price, qty=qty, notional=price * qty)


def _make(unfilled: bool, better_price_enabled: bool = True,
          slippage_tol_pct: float = 0.003, store=None,
          fail_orders: bool = False, portfolio_id: str = "",
          broker=None):
    from better_executor import BetterPriceExecutor
    if broker is None:
        broker = MemoryBroker(unfilled=unfilled, fail_orders=fail_orders)
    executor = BetterPriceExecutor(
        broker, None, None, dry_run=False, improve_pct=0.003,
        slippage_tol_pct=slippage_tol_pct,
        better_price_enabled=better_price_enabled,
        fill_timeout_s=300, poll_sec=1, tp_roe_pct=None, sl_roe_pct=None)
    rule = Rule(lead_balance=300000.0, my_equity=10000.0,
                leverage=20, improve_pct=0.003,
                slippage_tol_pct=slippage_tol_pct,
                better_price_enabled=better_price_enabled)
    # 自检用固定历史时间戳，默认关闭运行时年龄检查；场景 R 会显式打开。
    rule.signal_max_age_s = 0
    # 对账补差默认关：现有场景都在数「下了几笔单」，开着会被补差单干扰。
    # 场景 U 单独打开它验证行为。
    rule.rebalance = False
    return CopyEngine(executor, rule, store=store,
                      portfolio_id=portfolio_id), broker


def selftest() -> int:
    opened = _fill("OPEN", "2026-11-13 14:00:00")
    closed = _fill("CLOSE", "2026-11-13 14:05:00")
    reopened = _fill("OPEN", "2026-11-13 14:10:00", price=68100.0)
    results: list[tuple[str, bool, str]] = []

    # A：他开仓后价格上涨、我的更优限价单没成交，随后他止损平仓
    eng, broker = _make(unfilled=True)
    r1 = eng.process([opened, closed])
    r2 = eng.process([opened, closed])          # 下一轮重复拉到同样数据
    ok = (r1["events"] == 2 and r2["events"] == 0
          and not broker.open_orders and not eng.pending
          and not broker.positions
          and any(c[0] == "LIMIT" for c in broker.calls)
          and any(c[0] == "CANCEL_ALL" for c in broker.calls))
    results.append(("开仓未成交 + 带单员止损平仓 -> 撤单且不补开", ok,
                    f"r1={r1} r2_events={r2['events']} "
                    f"open_orders={len(broker.open_orders)} "
                    f"my_positions={len(broker.positions)}"))

    # B：同一条开仓事件重复拉取，不能重复挂单
    eng2, broker2 = _make(unfilled=True)
    eng2.process([opened])
    first = len([c for c in broker2.calls if c[0] == "LIMIT"])
    eng2.process([opened])
    second = len([c for c in broker2.calls if c[0] == "LIMIT"])
    ok = first == 1 and second == 1
    results.append(("重复轮询不重复挂单", ok, f"LIMIT 次数 {first} -> {second}"))

    # C：开 -> 平 -> 再开，按时间顺序依次执行
    eng3, broker3 = _make(unfilled=True)
    r3 = eng3.process([opened, closed, reopened])
    ok = (r3["events"] == 3 and len(eng3.pending) == 1
          and len(broker3.open_orders) == 1)
    results.append(("开-平-再开顺序正确", ok,
                    f"{r3} pending={len(eng3.pending)}"))

    # D：入场限价单一直没成交，超时后自动撤单
    eng4, broker4 = _make(unfilled=True)
    eng4.process([opened])
    eng4.pending["BTCUSDT"].created_at -= 400
    eng4.process([])
    ok = not eng4.pending and not broker4.open_orders
    results.append(("超时自动撤单", ok,
                    f"pending={len(eng4.pending)} open_orders={len(broker4.open_orders)}"))

    # E：关掉更优开关 -> 容差内挂限价（多单更高/空单更低），平仓仍无条件撤单
    tol = 0.003
    eng5, broker5 = _make(unfilled=True, better_price_enabled=False,
                          slippage_tol_pct=tol)
    r5 = eng5.process([opened])
    long_limit = [c for c in broker5.calls if c[0] == "LIMIT"][0][4]
    eng5.process([closed])
    ghost_ok = (not broker5.open_orders and not eng5.pending
                and any(c[0] == "CANCEL_ALL" for c in broker5.calls))
    eng6, broker6 = _make(unfilled=True, better_price_enabled=False,
                          slippage_tol_pct=tol)
    short_open = _fill("OPEN", "2026-11-13 14:00:00", side="SHORT")
    eng6.process([short_open])
    short_limit = [c for c in broker6.calls if c[0] == "LIMIT"][0][4]
    ok = (long_limit > 68000.0 and short_limit < 68000.0 and ghost_ok
          and len(r5["actions"]) == 1)
    results.append(("关闭更优开关 -> 容差挂价 + 平仓仍撤单", ok,
                    f"多单限价={long_limit} 空单限价={short_limit} "
                    f"撤单={ghost_ok} 本轮动作={len(r5['actions'])}"))

    # F：同一秒的逐笔撮合分两轮返回（聚合结果不同）也不能重复开平
    eng7, broker7 = _make(unfilled=True)
    partial = _fill("OPEN", "2026-11-13 14:00:00", qty=1.0)
    full = _fill("OPEN", "2026-11-13 14:00:00", qty=2.0)
    r7a = eng7.process([partial])
    r7b = eng7.process([full])
    ok = r7a["events"] == 1 and r7b["events"] == 0
    results.append(("跨轮同秒成交不重复开平", ok,
                    f"r7a_events={r7a['events']} r7b_events={r7b['events']}"))

    # G：模拟关掉程序再开 —— seen / pending 从 SQLite 恢复，旧单不重开、新单不漏
    import tempfile
    from pathlib import Path
    from state_store import StateStore
    with tempfile.TemporaryDirectory() as _d:
        db = Path(_d) / "state.db"
        s1 = StateStore(db)
        # 预置 last_run_wall：模拟「已经跟了一段时间后关机」，否则引擎会
        # 按首次跟单走基线、这一轮不开仓，测的就不是重启恢复了。
        s1.set("last_run_wall", time.time())
        # 用当前时间做信号，避免被「重启后 15 分钟外的老开仓不补开」规则吞掉
        g_open = _fill("OPEN", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        eng8, _ = _make(unfilled=True, store=s1)
        eng8.process([g_open])
        s1.close()

        s2 = StateStore(db)
        eng9, _ = _make(unfilled=True, store=s2)
        restored_pending = len(eng9.pending)
        r9 = eng9.process([g_open])
        fresh_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fresh = _fill("OPEN", fresh_ts, symbol="ETHUSDT")
        r10 = eng9.process([fresh])
        s2.close()
    ok = restored_pending == 1 and r9["events"] == 0 and r10["events"] == 1
    results.append(("重启恢复状态：旧单不重开、新单不漏", ok,
                    f"restored_pending={restored_pending} "
                    f"old_events={r9['events']} new_events={r10['events']}"))

    # H：库是空的但交易所还挂着本程序的单 —— 反向接管，且只接管自己的单。
    #     接管后超时撤单要能生效；账户里手动挂的 ETH 单不能被撤。
    with tempfile.TemporaryDirectory() as _d2:
        s3 = StateStore(Path(_d2) / "state.db")
        eng10, br10 = _make(unfilled=True, store=s3)
        br10.open_orders = [
            {"order_id": 9001, "symbol": "BTCUSDT", "side": "BUY",
             "type": "LIMIT", "price": 67795.99999, "quantity": 0.05,
             "client_order_id": "copy_btcusdt_deadbeef"},
            {"order_id": 9002, "symbol": "ETHUSDT", "side": "SELL",
             "type": "LIMIT", "price": 2000.0, "quantity": 1.0,
             "client_order_id": ""},
        ]
        eng10.process([])
        adopted = sorted(eng10.pending)
        for p in eng10.pending.values():
            p.created_at -= 400
        eng10.process([])
        left = sorted(o["symbol"] for o in br10.open_orders)
        s3.close()

    # 新挂的入场单必须自带标记，否则下次重启就认不回来
    eng11, br11 = _make(unfilled=True)
    eng11.process([_fill("OPEN", "2026-11-13 14:00:00")])
    tagged = bool(br11.open_orders) and all(
        str(o["client_order_id"]).startswith(ORDER_TAG)
        for o in br11.open_orders)
    ok = (adopted == ["BTCUSDT"] and left == ["ETHUSDT"]
          and not eng10.pending and tagged)
    results.append(("交易所挂单对账：只接管本程序单并纳入超时撤单", ok,
                    f"adopted={adopted} 撤后剩余={left} "
                    f"pending={len(eng10.pending)} 新单带标记={tagged}"))

    # I：下单请求失败（网络断）不能被 seen 吞掉，恢复后下一轮必须补开
    eng12, br12 = _make(unfilled=True, fail_orders=True)
    r12 = eng12.process([opened])
    br12.fail_orders = False
    r13 = eng12.process([opened])
    ok = (r12["events"] == 1 and r12["opened"] == 0
          and r13["events"] == 1 and r13["opened"] == 1)
    results.append(("下单失败不吞事件：网络恢复后下一轮补开", ok,
                    f"首轮={r12['events']}/{r12['opened']} "
                    f"次轮={r13['events']}/{r13['opened']}"))

    # J：关机两小时、期间带单员平仓 —— 重启后过老的 CLOSE 必须仍然执行，
    #     但过老的 OPEN 依旧不能补开。
    with tempfile.TemporaryDirectory() as _d3:
        s4 = StateStore(Path(_d3) / "state.db")
        eng13, _ = _make(unfilled=True, store=s4)
        eng13.process([opened])
        s4.set("last_run_wall", time.time() - 7200)
        s4.close()

        s5 = StateStore(Path(_d3) / "state.db")
        eng14, br14 = _make(unfilled=True, store=s5)
        stale_close = _fill("CLOSE", "2026-11-13 14:05:00")
        r14 = eng14.process([stale_close])
        stale_open = _fill("OPEN", "2026-11-13 14:00:00", symbol="ETHUSDT")
        r15 = eng14.process([stale_open])
        s5.close()
    closed_ok = (r14["cancel_only"] == 1
                 and any(c[0] == "CANCEL_ALL" for c in br14.calls))
    ok = closed_ok and r15["events"] == 0
    results.append(("关机期间带单员平仓：重启仍撤单，老开仓仍不补", ok,
                    f"仅撤单={r14['cancel_only']} "
                    f"stale_open_events={r15['events']}"))

    # ---- 以下用新交易员「黑石策略」真实流水样本：换人 + 部分平仓多场景 ----
    # 真实接口 2026-11-13 21:53 拉到的三条聚合事件（数量/价格/名义均为实值）。
    real_history = [
        Fill(ts="2026-11-13 20:06:12", action="OPEN", side="LONG",
             symbol="SNDKUSDT", price=945.91, qty=0.24, notional=227.02),
        Fill(ts="2026-11-13 20:41:56", action="CLOSE", side="LONG",
             symbol="ETHUSDT", price=1959.08, qty=10.0, notional=19590.80),
        Fill(ts="2026-11-13 21:38:14", action="CLOSE", side="LONG",
             symbol="ETHUSDT", price=1984.07, qty=20.0, notional=39681.40),
        # 历史已平的仓位：开 + 平配对，重放后不应残留、不能跟
        Fill(ts="2026-11-13 20:30:00", action="OPEN", side="SHORT",
             symbol="ZECUSDT", price=900.0, qty=10.0, notional=9000.0),
        Fill(ts="2026-11-13 20:50:00", action="CLOSE", side="SHORT",
             symbol="ZECUSDT", price=910.0, qty=10.0, notional=9100.0),
    ]
    new_open = Fill(ts="2026-11-13 21:39:00", action="OPEN", side="LONG",
                    symbol="BTCUSDT", price=68000.0, qty=1.0,
                    notional=68000.0)

    # K：换带单员首轮 —— 历史未平仓位按更优价挂单跟进，已平仓位不跟
    with tempfile.TemporaryDirectory() as _d4:
        s6 = StateStore(Path(_d4) / "state.db")
        s6.set("last_run_wall", time.time())
        s6.set("lead_portfolio_id", "1111111111111111111")   # 旧交易员
        s6.mark_seen(["ETHUSDT|LONG|CLOSE|2026-11-13 18:30:24"])
        eng15, br15 = _make(unfilled=False, store=s6,
                            portfolio_id="2222222222222222222")
        r16 = eng15.process(real_history)
        first_calls = [c for c in br15.calls if c[0] == "LIMIT"]
        hist = [a for a in r16["actions"] if a.get("historical")]
        # L：模拟 seen 被 TTL 淘汰，历史仓位也不能重复挂单
        for k in list(eng15.seen):
            eng15.seen[k] = time.monotonic() - 10000
        r17 = eng15.process(real_history)
        revive_calls = len([c for c in br15.calls if c[0] == "LIMIT"])
        # M：换人之后出现的全新开仓，仍然要正常跟
        r18 = eng15.process(real_history + [new_open])
        new_calls = len([c for c in br15.calls if c[0] == "LIMIT"])
        s6.close()
    ok = (r16.get("baseline") is True and r16["opened"] == 1
          and len(hist) == 1 and hist[0]["symbol"] == "SNDKUSDT"
          and hist[0]["limit"] < 945.91        # 多单只挂更优价，不追高
          and len(first_calls) == 1
          and r17["opened"] == 0 and revive_calls == 1
          and r18["opened"] == 1 and new_calls == 2)
    results.append(("换人：历史未平仓位按更优价跟、已平不跟、新单照跟", ok,
                    f"历史单={r16['opened']} 品种={hist[0]['symbol'] if hist else '-'} "
                    f"limit={hist[0]['limit'] if hist else '-'} "
                    f"淘汰后={r17['opened']} 新开={r18['opened']} "
                    f"LIMIT={new_calls}"))

    # N：他开 1000u 的 BTC 多单、平 200u —— 我方只平 20%，不能整仓平
    eng16, br16 = _make(unfilled=False)
    big = _fill("OPEN", "2026-11-13 14:00:00", price=1000.0, qty=1.0)
    eng16.process([big])
    my0 = br16.positions.get("BTCUSDT", 0.0)
    part = _fill("CLOSE", "2026-11-13 14:05:00", price=1000.0, qty=0.2)
    # 每轮传全量窗口（真实轮询如此），重放才能算出这笔 CLOSE 的平前持仓
    r19 = eng16.process([big, part])
    my1 = br16.positions.get("BTCUSDT", 0.0)
    act19 = [a for a in r19["actions"] if a["type"] == "CLOSE_FOLLOW"][0]
    ratio_ok = (my0 > 0 and abs((my0 - my1) / my0 - 0.2) < 0.02
                and abs(act19["close_ratio"] - 0.2) < 1e-6
                and act19["close_qty"] is not None)
    results.append(("部分平仓：带单员平 20% 本程序只平 20%", ratio_ok,
                    f"我方 {my0:.4f} -> {my1:.4f} "
                    f"ratio={act19['close_ratio']} qty={act19['close_qty']}"))

    # O：连续部分平仓 + 最后全平，账本要跟着递减，累计比例对齐
    eng17, br17 = _make(unfilled=False)
    eng17.process([big])
    my_start = br17.positions.get("BTCUSDT", 0.0)
    c1 = _fill("CLOSE", "2026-11-13 14:05:00", price=1000.0, qty=0.2)
    c2 = _fill("CLOSE", "2026-11-13 14:10:00", price=1000.0, qty=0.3)
    c3 = _fill("CLOSE", "2026-11-13 14:15:00", price=1000.0, qty=0.5)
    # 每轮都传累积的全量窗口，重放才拿得到每笔 CLOSE 的平前持仓
    r20 = eng17.process([big, c1])
    r21 = eng17.process([big, c1, c2])
    left2 = br17.positions.get("BTCUSDT", 0.0)
    r22 = eng17.process([big, c1, c2, c3])
    left3 = br17.positions.get("BTCUSDT", 0.0)
    a21 = [a for a in r21["actions"] if a["type"] == "CLOSE_FOLLOW"][0]
    a22 = [a for a in r22["actions"] if a["type"] == "CLOSE_FOLLOW"][0]
    ok = (my_start > 0 and left2 > 0 and left3 == 0
          and abs(a21["close_ratio"] - 0.375) < 1e-6
          and a22["close_ratio"] == 1.0
          # 整仓平的 API 参数是 None（=全部），但账本必须记实际数量
          and abs(a22["close_qty"] - left2) < 1e-6
          and a22["side"] == "LONG")
    results.append(("连续部分平仓后全平：比例递减且最终清仓", ok,
                    f"二次ratio={a21['close_ratio']} 剩余={left2:.4f} "
                    f"末次ratio={a22['close_ratio']} 清仓={left3 == 0} "
                    f"末次数量={a22['close_qty']}"))

    # P：账本缺失（老库 / 手动持仓）时退化为整仓平，不留残渣
    eng18, br18 = _make(unfilled=False)
    br18.positions["BTCUSDT"] = 1.0
    r23 = eng18.process([_fill("CLOSE", "2026-11-13 14:05:00",
                               price=1000.0, qty=0.2)])
    a23 = [a for a in r23["actions"] if a["type"] == "CLOSE_FOLLOW"][0]
    ok = (br18.positions.get("BTCUSDT", 0.0) == 0
          and a23["close_ratio"] == 1.0 and (a23["close_qty"] or 0) > 0)
    results.append(("账本缺失：整仓平兜底，不按残缺比例留仓", ok,
                    f"剩余={br18.positions.get('BTCUSDT', 0.0)} "
                    f"ratio={a23['close_ratio']}"))

    # Q：运行中点「换带单员」，同样重新建立基线
    with tempfile.TemporaryDirectory() as _d5:
        s7 = StateStore(Path(_d5) / "state.db")
        s7.set("last_run_wall", time.time())
        eng19, br19 = _make(unfilled=False, store=s7)
        eng19.begin_following("9876543210987654321")
        r24 = eng19.process(real_history)
        calls24 = len([c for c in br19.calls if c[0] == "LIMIT"])
        pid = s7.get("lead_portfolio_id", "")
        s7.close()
    ok = (r24.get("baseline") is True and r24["opened"] == 1
          and calls24 == 1 and pid == "9876543210987654321")
    results.append(("运行中换人：重置起点，历史未平仓位按更优价跟", ok,
                    f"baseline={r24.get('baseline')} 历史单={calls24} pid={pid}"))

    # R：运行中接口延迟补回超龄 OPEN —— 不能拿旧价格挂单；同批 CLOSE 仍放行
    eng20, br20 = _make(unfilled=False)
    eng20.rule.signal_max_age_s = 900
    stale_open = _fill("OPEN", "2026-11-13 14:00:00", price=1000.0, qty=1.0)
    stale_close = _fill("CLOSE", "2026-11-13 14:05:00", price=1000.0,
                        qty=0.2)
    r25 = eng20.process([stale_open, stale_close])
    limits25 = [c for c in br20.calls if c[0] == "LIMIT"]
    cancels25 = [c for c in br20.calls if c[0] == "CANCEL_ALL"]
    fresh2 = _fill("OPEN", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   symbol="ETHUSDT")
    r26 = eng20.process([fresh2])
    ok = (not limits25 and cancels25 and r25["opened"] == 0
          and r25["cancel_only"] == 1 and r26["opened"] == 1)
    results.append(("超龄开仓信号不挂旧价，平仓信号仍执行撤单", ok,
                    f"超龄opened={r25['opened']} 仅撤单={r25['cancel_only']} "
                    f"新信号opened={r26['opened']}"))

    # U：漏掉一条 OPEN 之后，对账补差要把仓位补回等比目标
    eng25, br25 = _make(unfilled=False)
    eng25.rule.rebalance = True
    eng25.rule.signal_max_age_s = 900        # 让 14:00 的开仓变成超龄信号
    eng25.needs_baseline = False             # 不建基线，直接走事件链路
    eng25.follow_start_wall = 1794500000.0   # 跟单起点早于 11-13 14:00
    r34 = eng25.process([opened])            # 第一轮：被吞，不下单
    lim1 = len([c for c in br25.calls if c[0] == "LIMIT"])
    r35 = eng25.process([opened])            # 第二轮：账本有仓、我方空 -> 补差
    lim2 = len([c for c in br25.calls if c[0] == "LIMIT"])
    qty_after = br25.positions.get("BTCUSDT", 0.0)
    eng25.process([opened])                  # 第三轮：已对齐，不该再下单
    lim3 = len([c for c in br25.calls if c[0] == "LIMIT"])
    # 事件被吞（opened=0），但同一轮的对账补差把仓位补上了；之后不再重复补
    ok = (r34["opened"] == 0 and lim1 == 1
          and abs(qty_after) > 0 and lim2 == 1 and lim3 == 1)
    results.append(("超龄漏单：事件不跟，但对账补差把仓位补回且不重复", ok,
                    f"opened={r34['opened']} LIMIT={lim1}->{lim2}->{lim3} "
                    f"补到 {qty_after:.4f} 张"))

    # ---- S：币安「最新操作记录」截图里的真实数字 ----
    btc1 = Fill(ts="2026-11-13 14:01:11", action="OPEN", side="LONG",
                symbol="BTCUSDT", price=68025.48, qty=6.305,
                notional=428900.66)
    btc2 = Fill(ts="2026-11-13 14:04:34", action="OPEN", side="LONG",
                symbol="BTCUSDT", price=68170.10, qty=8.532,
                notional=581627.30)
    btc_close = Fill(ts="2026-11-13 21:38:50", action="CLOSE", side="LONG",
                     symbol="BTCUSDT", price=69984.80, qty=14.837,
                     notional=1038364.52)

    # S1：先后两次开多 BTC，第二笔是加仓，不能撤掉第一笔的量只跟最后一笔
    eng21, br21 = _make(unfilled=False)
    r27 = eng21.process([btc1])
    lim1 = [c for c in br21.calls if c[0] == "LIMIT"][-1]
    r28 = eng21.process([btc1, btc2])
    lim2 = [c for c in br21.calls if c[0] == "LIMIT"][-1]
    cancels = [c for c in br21.calls if c[0] == "CANCEL_ALL"]
    ok = (r27["opened"] == 1 and r28["opened"] == 1
          and lim2[3] > lim1[3]                 # 累计数量更大
          and abs(lim2[4] - 68108.64 * 0.997) < 1.0   # 基准=加权均价
          and not cancels)                     # 加仓只撤自己那笔，不撤全部
    results.append(("真实数字·两次开多 BTC 加仓不丢量", ok,
                    f"首笔qty={lim1[3]} 累计qty={lim2[3]} "
                    f"累计基准价limit={lim2[4]:.2f} 全撤次数={len(cancels)}"))

    # S2：一次平多 14.837 = 6.305 + 8.532，按累计仓位整平
    mine_before = br21.positions.get("BTCUSDT", 0.0)   # 本程序实际持有的量
    r29 = eng21.process([btc1, btc2, btc_close])
    a29 = [a for a in r29["actions"] if a["type"] == "CLOSE_FOLLOW"][0]
    ok = (abs(btc1.qty + btc2.qty - btc_close.qty) < 1e-6
          and r29["opened"] == 0 and a29["close_ratio"] == 1.0
          # 账本记的必须是「我们自己的持仓量」，不是带单员的 14.837
          and mine_before > 0
          and abs(a29["close_qty"] - mine_before) < 1e-9
          and br21.positions.get("BTCUSDT", 0.0) == 0)
    results.append(("真实数字·一次全平数量精确对齐并清仓", ok,
                    f"6.305+8.532={btc1.qty + btc2.qty:.3f} "
                    f"平仓={btc_close.qty} ratio={a29['close_ratio']} "
                    f"我方={mine_before:.4f} 记账={a29['close_qty']:.4f} "
                    f"清仓={br21.positions.get('BTCUSDT', 0.0) == 0}"))

    # S3：ZEC 分两次平空，比例分别约 50% 和 100%，且方向是 SHORT
    zec_open = Fill(ts="2026-11-13 14:00:00", action="OPEN", side="SHORT",
                    symbol="ZECUSDT", price=900.0, qty=399.789,
                    notional=359810.10)
    zec_c1 = Fill(ts="2026-11-13 14:03:58", action="CLOSE", side="SHORT",
                  symbol="ZECUSDT", price=914.94, qty=199.894,
                  notional=182891.18)
    zec_c2 = Fill(ts="2026-11-13 14:04:03", action="CLOSE", side="SHORT",
                  symbol="ZECUSDT", price=920.68, qty=199.895,
                  notional=184039.98)
    eng22, br22 = _make(unfilled=False)
    eng22.process([zec_open])
    r30 = eng22.process([zec_open, zec_c1])
    a30 = [a for a in r30["actions"] if a["type"] == "CLOSE_FOLLOW"][0]
    r31 = eng22.process([zec_open, zec_c1, zec_c2])
    a31 = [a for a in r31["actions"] if a["type"] == "CLOSE_FOLLOW"][0]
    ok = (abs(a30["close_ratio"] - 0.5) < 0.01
          and a31["close_ratio"] == 1.0
          and br22.positions.get("ZECUSDT", 0.0) == 0)
    results.append(("真实数字·ZEC 分两次平空，比例 50% → 100%", ok,
                    f"首平ratio={a30['close_ratio']} "
                    f"次平ratio={a31['close_ratio']} "
                    f"清仓={br22.positions.get('ZECUSDT', 0.0) == 0}"))

    # S4：无我方仓位时他的平仓只撤单，不能记成「平了仓」
    eng23, br23 = _make(unfilled=True)
    r32 = eng23.process([btc_close])
    ok = (r32["closed"] == 0 and r32["cancel_only"] == 1
          and not any(c[0] in ("CLOSE", "LIMIT") for c in br23.calls))
    results.append(("无我方仓位：平仓事件只撤单，不计入 closed", ok,
                    f"closed={r32['closed']} cancel_only={r32['cancel_only']} "
                    f"下单调用={[c[0] for c in br23.calls]}"))

    # S5：历史未平仓位按更优价挂、已平仓位不跟（用真实 BTC/ZEC 数字）
    with tempfile.TemporaryDirectory() as _d6:
        s8 = StateStore(Path(_d6) / "state.db")
        s8.set("last_run_wall", time.time())
        s8.set("lead_portfolio_id", "OLD")
        eng24, br24 = _make(unfilled=False, store=s8, portfolio_id="NEW")
        r33 = eng24.process([btc1, btc2, zec_open, zec_c1, zec_c2])
        hist = [a for a in r33["actions"] if a.get("historical")]
        syms = sorted(a["symbol"] for a in hist)
        s8.close()
    ok = (r33.get("baseline") is True and syms == ["BTCUSDT"]
          and hist[0]["limit"] < 68108.64       # 多单只挂更优价
          and hist[0]["lead_notional"] > 1000000)
    results.append(("真实数字·历史未平 BTC 按更优价跟、已平 ZEC 不跟", ok,
                    f"历史单品种={syms} limit={hist[0]['limit'] if hist else '-'} "
                    f"累计名义={hist[0]['lead_notional'] if hist else '-'}"))

    # ---- T：agent 复查找出的失败路径，必须用用例锁住 ----
    class FragileBroker(MemoryBroker):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.fail_cancel = False
            self.fail_positions = False

        def cancel_all_orders(self, symbol):
            if self.fail_cancel:
                return False
            return super().cancel_all_orders(symbol)

        def get_positions(self):
            if self.fail_positions:
                raise RuntimeError("simulated position query failure")
            return super().get_positions()

    # T1：撤单失败不能把 CLOSE 永久吞掉，下轮必须重试
    fb1 = FragileBroker(unfilled=False)
    eng25, br25 = _make(False, broker=fb1)
    t_close = _fill("CLOSE", "2026-11-13 14:05:00")
    fb1.fail_cancel = True
    r34 = eng25.process([t_close])
    fb1.fail_cancel = False
    r35 = eng25.process([t_close])
    n_cancel = sum(1 for c in br25.calls if c[0] == "CANCEL_ALL")
    # 失败的撤单请求不会记调用，成功那轮记 1 次；关键是下一轮事件还在
    ok = (r34["cancel_only"] == 1 and r35["events"] == 1
          and r35["cancel_only"] == 1 and n_cancel == 1)
    results.append(("撤单失败：CLOSE 事件保留并在下轮重试", ok,
                    f"首轮={r34['cancel_only']} 次轮events={r35['events']} "
                    f"成功撤单={n_cancel}"))

    # T2：get_positions 异常不能误判成「无仓位」
    fb2 = FragileBroker(unfilled=False)
    eng26, br26 = _make(False, broker=fb2)
    br26.positions["BTCUSDT"] = 1.0
    fb2.fail_positions = True
    r36 = eng26.process([t_close])
    fb2.fail_positions = False
    r37 = eng26.process([t_close])
    ok = (r36["closed"] == 0 and r37["closed"] == 1
          and br26.positions.get("BTCUSDT", 0.0) == 0)
    results.append(("读取持仓失败：事件保留，下轮补平而不是只撤单", ok,
                    f"首轮closed={r36['closed']} 次轮closed={r37['closed']} "
                    f"清仓={br26.positions.get('BTCUSDT', 0.0) == 0}"))

    # T3：历史未平仓位的挂单不受 300s 超时影响
    with tempfile.TemporaryDirectory() as _d7:
        s9 = StateStore(Path(_d7) / "state.db")
        s9.set("last_run_wall", time.time())
        s9.set("lead_portfolio_id", "OLD")
        eng27, br27 = _make(False, store=s9,
                            portfolio_id="9876543210987654321",
                            broker=MemoryBroker(unfilled=True))
        r38 = eng27.process([btc1, btc2])
        for p in eng27.pending.values():
            p.created_at -= 400
        eng27.process([btc1, btc2])
        alive = (len(eng27.pending) == 1
                 and getattr(next(iter(eng27.pending.values())),
                             "historical", False))
        s9.close()
    results.append(("历史未平仓位挂单：超过 300s 也不被超时撤掉", alive,
                    f"基线挂单={r38['opened']} 超时后仍在={len(eng27.pending)}"))

    # T4：旧挂单已部分成交时，加仓只补差额，不按全量重复开
    eng28, br28 = _make(unfilled=False)
    eng28.process([btc1])
    br28.positions["BTCUSDT"] = 0.10      # 模拟旧单成交了 0.10
    r39 = eng28.process([btc1, btc2])
    last_qty = [c for c in br28.calls if c[0] == "LIMIT"][-1][3]
    # 账本按「事件应持」记账后，这里刻意不把账本降到真实持仓：
    # 那与「手动平仓不补回」不可区分（都是持仓比账本少）。
    # 于是差额只按账本算 —— 方向偏保守：宁可少开，不许多开。
    ok = (r39["opened"] == 1 and last_qty < 0.496
          and last_qty + 0.21 <= 0.496 + 1e-6)
    results.append(("旧单部分成交：只按账本差额补，绝不超过累计目标", ok,
                    f"目标累计=0.496 已成交=0.10 本次挂单={last_qty}"))

    # T5：平仓价与开仓价不同，累计基准价不能被平仓名义拉偏
    p_open = _fill("OPEN", "2026-11-13 14:00:00", price=1000.0, qty=1.0)
    p_close = _fill("CLOSE", "2026-11-13 14:05:00", price=2000.0, qty=0.5)
    p_open2 = _fill("OPEN", "2026-11-13 14:10:00", price=1000.0, qty=1.0)
    from position_ledger import replay as _replay
    ctx = _replay([p_open, p_close, p_open2]).open_context[p_open2.key]
    ok = abs(ctx["avg"] - 1000.0) < 1e-6
    results.append(("平仓价≠开仓价：累计名义按开仓价核销，基准不漂移", ok,
                    f"加仓基准={ctx['avg']:.2f}（应=1000.00）"))

    # T5：他浮亏时加仓，分母用钱包余额（剔除未实现盈亏），仓位不放大
    eng29, br29 = _make(unfilled=True)
    eng29.process([btc1], lead_balance=300000.0, marks={"BTCUSDT": 68025.48})
    qty0 = [c for c in br29.calls if c[0] == "LIMIT"][-1][3]
    # 价格跌到 60000，他浮亏 12 万，marginBalance 同步缩水；钱包余额不变
    unreal = ((60000.0 - 68025.48) * 6.305
              + (60000.0 - 68170.10) * 8.532)
    margin_after = 300000.0 + unreal
    eng29.process([btc1, btc2], lead_balance=margin_after,
                  marks={"BTCUSDT": 60000.0})
    qty1 = [c for c in br29.calls if c[0] == "LIMIT"][-1][3]
    # 若按 marginBalance 做分母，目标会放大到 5.6 倍，qty 约 0.82
    wrong = 1010527.96 / margin_after * 10000.0 / 67904.32
    ok = (abs(qty1 - 0.496) < 0.01 and qty1 < wrong * 0.7
          and abs(eng29.lead_wallet - 300000.0) < 1e-3
          and not eng29.fund_changes)
    results.append(("带单员浮亏 12 万时加仓：分母仍为钱包余额，仓位不放大", ok,
                    f"首笔={qty0} 加仓后={qty1} 按marginBalance会={wrong:.3f} "
                    f"wallet={eng29.lead_wallet:.2f} 误报资金变动={len(eng29.fund_changes)}"))

    # T6：出入金播报；浮亏与已实现盈亏都不算「资金变动」
    eng30, br30 = _make(unfilled=True)
    eng30.process([], lead_balance=300000.0, marks={})
    eng30.process([], lead_balance=250000.0, marks={})
    eng30.process([], lead_balance=249999.0, marks={})
    funds = list(eng30.fund_changes)
    ok = (len(funds) == 1 and abs(funds[0]["amount"] + 50000.0) < 1e-6
          and funds[0]["anchored"])
    results.append(("带单员提走 5 万保证金：播报一次，1 USDT 抖动不报", ok,
                    f"条数={len(funds)} "
                    f"金额={funds[0]['amount'] if funds else None} "
                    f"钱包={funds[0]['wallet_before'] if funds else None}"
                    f"->{funds[0]['wallet_after'] if funds else None}"))

    # T7：平仓实现盈亏改变钱包余额，但不算出入金
    eng31, br31 = _make(unfilled=False)
    eng31.process([btc1], lead_balance=300000.0, marks={"BTCUSDT": 68025.48})
    # 他平仓赚了 2 万：钱包余额 32 万，marginBalance 同步到 32 万
    eng31.process([btc1, btc_close], lead_balance=320000.0,
                  marks={"BTCUSDT": 68025.48})
    ok = (not eng31.fund_changes and abs(eng31.lead_wallet - 320000.0) < 1e-3)
    results.append(("平仓已实现盈亏：钱包余额跟随，但不误报为出入金", ok,
                    f"资金变动={len(eng31.fund_changes)} "
                    f"wallet={eng31.lead_wallet:.2f}"))

    # T6：dry-run 的真实撮合语义 —— 价格没到限价就不许成交
    br32 = MemoryBroker(unfilled=False, auto_fill=False)
    br32.place_limit_order("BTCUSDT", "BUY", 0.5, 67796.0)
    br32.place_limit_order("ZECUSDT", "SELL", 2.0, 950.0)
    got_hi = br32.match_orders({"BTCUSDT": 69480.0, "ZECUSDT": 900.0})
    still = len(br32.open_orders)
    got_lo = br32.match_orders({"BTCUSDT": 67700.0, "ZECUSDT": 960.0})
    ok = (got_hi == [] and still == 2 and sorted(got_lo) == ["BTCUSDT", "ZECUSDT"]
          and abs(br32.positions.get("BTCUSDT", 0.0) - 0.5) < 1e-9
          and abs(br32.positions.get("ZECUSDT", 0.0) + 2.0) < 1e-9
          and abs(br32.entry["BTCUSDT"] - 67796.0) < 1e-9
          and not br32.open_orders)
    results.append(("模拟撮合：买单价跌到限价才成交、卖单涨到才成交", ok,
                    f"价格未到={got_hi} 挂着={still} 成交={got_lo} "
                    f"持仓={br32.positions}"))

    # 撮合成交后才算「已有仓位」：同价位重复挂单只补差额
    br33 = MemoryBroker(unfilled=False, auto_fill=False)
    br33.place_limit_order("BTCUSDT", "BUY", 0.5, 67796.0)
    before = len(br33.open_orders)
    br33.match_orders({"BTCUSDT": 67796.0})
    after = len(br33.open_orders)
    ok = (before == 1 and after == 0
          and abs(br33.positions["BTCUSDT"] - 0.5) < 1e-9)
    results.append(("撮合后挂单出簿、持仓进账（引擎读得到 have）", ok,
                    f"挂单 {before}->{after} 持仓={br33.positions['BTCUSDT']}"))

    # T7：actions 是定长 deque，装满后 len() 恒等于 maxlen。
    # 用「开始前的长度」切片会永久返回空 —— 动作流和盈亏账本一起断供。
    eng35, br35 = _make(unfilled=True)
    cap = eng35.actions.maxlen
    for i in range(cap + 10):
        eng35._record({"type": "NOISE", "symbol": "NOISE", "i": i})
    r41 = eng35.process([btc1])
    ok = (len(eng35.actions) == cap and len(r41["actions"]) >= 1
          and r41["actions"][0]["type"] == "ENTRY"
          and "_no" not in r41["actions"][0])
    results.append(("动作流装满上限后仍能取到本轮动作（不靠长度切片）", ok,
                    f"容量={cap} 当前={len(eng35.actions)} "
                    f"本轮={len(r41['actions'])} 类型="
                    f"{[a['type'] for a in r41['actions']]}"))

    # T8：按「事件」记账，手动平掉的不许补回来
    eng36, br36 = _make(unfilled=False)
    eng36.process([btc1])                       # 他开第一笔，我跟一份
    first = br36.positions.get("BTCUSDT", 0.0)
    br36.close_position("BTCUSDT")              # 用户手动平掉
    eng36.process([btc1])                       # 没有新事件，只跑 rebalance
    after_flat = br36.positions.get("BTCUSDT", 0.0)
    r43 = eng36.process([btc1, btc2])           # 他又开一笔（8.532 张）
    after_add = br36.positions.get("BTCUSDT", 0.0)
    # 只按「这一笔事件自己的份额」开，不补到累计目标：
    # 8.532 × (10000 / 300000) ≈ 0.284，而不是累计目标 0.494
    expect = btc2.qty * 10000.0 / 300000.0
    ok = (first > 0 and after_flat == 0
          and abs(after_add - expect) < 0.005
          and after_add < 0.49)
    results.append(("手动平仓后不补仓；他再开只按该笔事件的份额开", ok,
                    f"首份={first:.4f} 手动平后={after_flat:.4f} "
                    f"他再开后={after_add:.4f}（应≈{expect:.4f}，"
                    f"不是累计目标 0.494）"))

    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"copy_engine selftest: {passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    return selftest() if args.selftest else 0


if __name__ == "__main__":
    raise SystemExit(main())
