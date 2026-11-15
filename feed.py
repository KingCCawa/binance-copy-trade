"""币安合约带单本「最新交易记录」-> 结构化信号。

只做解析、聚合、意图生成，不联网、不下单。

数据口径（全部可在 Rule 里改）：
  - 敞口按 notional / lead_balance 的比例复刻，绕开「逐笔杠杆不公开」的缺口；
  - 我方下单杠杆固定 20x 逐仓，只影响保证金占用与强平价，不参与敞口比例；
  - 同一秒、同 symbol+side+开平的逐笔撮合成交聚合为一条操作记录；
  - 公开流水里没有预设止盈止损价，出场靠「平多/平空」记录跟随。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from math import ceil, floor
from typing import Dict, List, Optional, Tuple

# 同时兼容两种文案：
#   App  「2026-11-13 04:09 开空 / 以均价为140.30237开启做空SPCXUSDT 永续仓位，...总价值为...USDT。」
#   网页「11-13 04:09:46 开空 / 以均价为140.30237USDT 卖出开空SPCXUSDT永续合约，...总价值为...USDT。」
# 差异：网页版时间戳带秒不带年、动词是「卖出开空/买入平空」、均价后带 USDT、
#       用「永续合约」、总价值精度 5 位、盈亏用逗号连接。
RECORD = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?|\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<tag>开空|开多|平空|平多)\s*"
    r"以均价为(?P<price>[\d,.]+)(?:USDT)?\s*"
    r"(?P<phrase>[^，]{0,24}?)"
    r"(?P<dir>空|多)"
    r"(?P<symbol>[A-Z0-9]+USDT)\s*永续(?:仓位|合约)，"
    r"成交数量为(?P<qty>[\d,.]+)[A-Z0-9]*，"
    r"总价值为(?P<notional>[\d,.]+)USDT[。，,]\s*"
    r"(?:已实现盈亏为(?P<pnl>-?[\d,.]+)USDT[。.]?)?",
    re.S,
)


def _num(raw: str) -> float:
    return float(raw.replace(",", ""))


def entry_limit_price(signal: float, side: str, step: float,
                      improve_pct: float, slippage_tol_pct: float,
                      better_price_enabled: bool) -> float:
    """按 tick 取整的入场限价。GUI 链路与离线意图共用这一处计算。

    开关开：多单挂低、空单挂高，只在优于信号价的位置成交。
    开关关：允许相对信号价最多 slippage_tol_pct 的恶化（多单可挂更高、
            空单可挂更低），仍用限价单把成交价封顶，不用市价单。
            取整方向不变——多单 floor 保证挂价不高于上限，
            空单 ceil 保证挂价不低于下限。

    tick 取整统一用 delta 的符号切换，避免"同一字段在不同模式下语义反转"：
      delta > 0 => 让利（更优价）；delta < 0 => 容忍恶化（容差价）。
    """
    s = 1 if side == "LONG" else -1
    delta = improve_pct if better_price_enabled else -slippage_tol_pct
    raw = signal * (1 - s * delta) / step
    return round((floor(raw) if s == 1 else ceil(raw)) * step, 8)


@dataclass(frozen=True)
class Fill:
    ts: str
    action: str                      # OPEN / CLOSE
    side: str                        # LONG / SHORT
    symbol: str
    price: float
    qty: float
    notional: float
    realized_pnl: Optional[float] = None

    @property
    def minute(self) -> str:
        return self.ts[:16]

    @property
    def key(self) -> str:
        # 事件桶标识 = 聚合口径（秒、品种、方向、开平）。
        # 不能带聚合后的 qty/price：同一秒的撮合若分两轮返回，两轮聚合出的
        # 数量与均价不同，带数值的 key 会放行两条 identical 事件，造成重复开平。
        # App 版只到分钟，ts[:19] 自然退化为完整 ts。
        stamp = self.ts[:19] if len(self.ts) >= 19 else self.ts
        return f"{self.symbol}|{self.side}|{self.action}|{stamp}"

    @property
    def notional_gap(self) -> float:
        return abs(self.price * self.qty - self.notional) / self.notional


def parse(text: str, default_year: int | None = None) -> List[Fill]:
    """把中文流水解析成逐笔成交，并校验 均价×数量≈总价值。

    网页版时间戳不带年（11-13 04:09:46），年份优先从文本里的日期范围
    「2026-11-07 → 2026-11-13」提取，否则用当前年。
    """
    if default_year is None:
        year_match = re.search(r"(20\d{2})-\d{2}-\d{2}", text)
        default_year = int(year_match.group(1)) if year_match else datetime.now().year

    out: List[Fill] = []
    for m in RECORD.finditer(text):
        d = m.groupdict()
        tag, direction = d["tag"], d["dir"]
        side = "SHORT" if direction == "空" else "LONG"
        if (side == "SHORT") != tag.endswith("空"):
            raise ValueError(f"tag/正文方向不一致: {tag} vs 做{direction}")

        ts = " ".join(d["ts"].split())
        if not ts[:4].isdigit():                 # 网页版 MM-DD HH:MM:SS
            ts = f"{default_year}-{ts}"
        f = Fill(
            ts=ts,
            action="OPEN" if tag.startswith("开") else "CLOSE",
            side=side,
            symbol=d["symbol"],
            price=_num(d["price"]),
            qty=_num(d["qty"]),
            notional=_num(d["notional"]),
            realized_pnl=_num(d["pnl"]) if d["pnl"] else None,
        )
        # OCR / 复制错字 / 网页版四舍五入会用这条兜住
        if f.notional_gap > 0.002:
            raise ValueError(f"均价×数量与总价值偏差 {f.notional_gap:.4%}: {f.key}")
        out.append(f)
    return out


@dataclass(frozen=True)
class Position:
    symbol: str
    side: str
    minute: str
    qty: float
    avg_price: float
    notional: float
    fills: int

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.side}|{self.minute}|{self.qty}|{self.avg_price:.8f}"


# 占位用的带单员 ID：只是为了让 argparse 默认值和文档命令能跑通。
# 真实 ID 从带单本链接里取（末尾那串 19 位数字），配置在 gui-config.json。
EXAMPLE_PORTFOLIO_ID = "1234567890123456789"


def group_opens(fills: List[Fill]) -> List[Position]:
    buckets: Dict[Tuple[str, str, str], List[Fill]] = {}
    for f in fills:
        if f.action == "OPEN":
            buckets.setdefault((f.symbol, f.side, f.minute), []).append(f)
    out = []
    for (symbol, side, minute), fs in buckets.items():
        qty = sum(f.qty for f in fs)
        notional = sum(f.notional for f in fs)
        out.append(Position(symbol, side, minute, qty, notional / qty, notional, len(fs)))
    return out


def closes(fills: List[Fill]) -> List[Fill]:
    return [f for f in fills if f.action == "CLOSE"]


@dataclass
class Rule:
    lead_balance: float = 100000.0       # 带单余额 USDT，敞口比例的分母（运行时覆盖）
    my_equity: float = 10000.0           # 我的合约账户权益
    improve_pct: float = 0.003           # 开关开：更优价幅度
    slippage_tol_pct: float = 0.003      # 开关关：可接受滑点上限（相对信号价）
    better_price_enabled: bool = True    # 开 = 只在更优价挂单；关 = 容差内挂限价
    leverage: int = 20                   # 固定 20x；只影响保证金占用，不参与敞口比例
    # 保证金模式跟带单员保持一致：全仓。逐仓会让单笔被单独强平，跟我们
    # 「照抄他的仓位、跟着他止损」的口径不是一回事。CROSSED 是币安的写法。
    margin_type: str = "CROSSED"
    price_step: float = 0.00001          # 运行时由 exchangeInfo 覆盖
    qty_step: float = 0.001              # 运行时由 exchangeInfo 覆盖
    max_signal_age_s: int = 900          # 记录晚于 15 分钟即放弃
    # 入场限价单挂多久没成交就撤。0 = 不撤，一直挂到他平仓。
    # 更优价本来就意味着「等价格回到更好的位置」，撤了就等于主动放弃这一单。
    entry_timeout_s: int = 300
    # 每轮按账本对账补差：漏掉一条 OPEN 后仓位会永久偏小，靠它自愈。
    # 自检里默认关掉（场景要可复现），GUI 从配置读，默认开。
    rebalance: bool = True


def entry_intent(pos: Position, rule: Rule, now: datetime) -> dict:
    age_s = (now - datetime.strptime(pos.minute, "%Y-%m-%d %H:%M")).total_seconds()
    if age_s > rule.max_signal_age_s:
        return dict(action="SKIP_STALE", key=pos.key, age_s=round(age_s))

    ratio = pos.notional / rule.lead_balance
    my_notional = rule.my_equity * ratio
    limit = entry_limit_price(pos.avg_price, pos.side, rule.price_step,
                              rule.improve_pct, rule.slippage_tol_pct,
                              rule.better_price_enabled)
    qty = round(int(my_notional / limit / rule.qty_step) * rule.qty_step, 8)
    if qty <= 0:
        return dict(action="SKIP_MIN_QTY", key=pos.key)
    return dict(
        action="PLACE_LIMIT",
        key=pos.key,
        symbol=pos.symbol,
        side=pos.side,
        limit=limit,
        qty=qty,
        notional_ratio=round(ratio, 6),
        my_notional=round(my_notional, 2),
        leverage=rule.leverage,
        merged_fills=pos.fills,
        age_s=round(age_s),
        price_mode=("更优 %.2f%%" % (rule.improve_pct * 100)
                    if rule.better_price_enabled
                    else "容差 %.2f%%" % (rule.slippage_tol_pct * 100)),
    )


# 用户提供的币安 App「合约带单本 / 最新交易记录」截图，2026-11-13 12:30
SAMPLE = """2026-11-13 10:19 平空
以均价为1,997.54关闭做空ETHUSDT 永续仓位，成交数量为518.692ETH，总价值为1,036,109.59450368USDT。已实现盈亏为44,977.25697823USDT。
2026-11-13 04:09 开空
以均价为140.30237开启做空SPCXUSDT 永续仓位，成交数量为356.325PCX，总价值为49,992.54197494USDT。
2026-11-13 04:09 开空
以均价为140.40226开启做空SPCXUSDT 永续仓位，成交数量为712.095PCX，总价值为99,979.0437568USDT。
2026-11-13 04:09 开空
以均价为140.11059开启做空SPCXUSDT 永续仓位，成交数量为356.765PCX，总价值为49,985.85419543USDT。
"""

# 用户提供的币安网页版「带单员详情 / 最新操作记录」截图，2026-11-13 12:59
# 时间戳精确到秒、不带年份；动词是「卖出开空 / 买入平空」；总价值 5 位小数。
WEB_SAMPLE = """2026-11-07 → 2026-11-13
11-13 10:19:42 平空
以均价为1,997.54USDT 买入平空ETHUSDT永续合约，成交数量为518.692ETH，总价值为1,036,109.59USDT，已实现盈亏为44,977.26USDT。
11-13 04:09:46 开空
以均价为140.30237USDT 卖出开空SPCXUSDT永续合约，成交数量为356.325PCX，总价值为49,992.54197USDT。
11-13 04:09:42 开空
以均价为140.40226USDT 卖出开空SPCXUSDT永续合约，成交数量为712.095PCX，总价值为99,979.04376USDT。
11-13 04:09:37 开空
以均价为140.11059USDT 卖出开空SPCXUSDT永续合约，成交数量为356.765PCX，总价值为49,985.85420USDT。
11-12 00:42:31 开空
以均价为1,006.95USDT 卖出开空ZECUSDT永续合约，成交数量为49.642ZEC，总价值为49,988.77USDT。
"""

if __name__ == "__main__":
    web = parse(WEB_SAMPLE)
    print(f"WEB parsed={len(web)} gaps={[round(f.notional_gap, 6) for f in web]}")
    for f in web:
        print("  WEB", f.ts, f.action, f.side, f.symbol, f.price, f.qty,
              f.notional, f.realized_pnl)
    print("---")
    fills = parse(SAMPLE)
    print(f"parsed={len(fills)}  gaps={[round(f.notional_gap, 6) for f in fills]}")
    for pos in group_opens(fills):
        print("position:", pos)
        print("  @04:10:", entry_intent(pos, Rule(), datetime(2026, 11, 13, 4, 10)))
        print("  @12:30:", entry_intent(pos, Rule(), datetime(2026, 11, 13, 12, 30)))
    rule_tol = Rule(better_price_enabled=False, slippage_tol_pct=0.003)
    for pos in group_opens(fills):
        print("  @04:10 容差:", entry_intent(pos, rule_tol, datetime(2026, 11, 13, 4, 10)))
    for f in closes(fills):
        print("close:", dict(ts=f.ts, side=f.side, symbol=f.symbol,
                            qty=f.qty, price=f.price, pnl=f.realized_pnl))
