"""模拟仓：拿一笔假资金，把带单员的成交重放一遍，看它「该开多少、会持多少」。

存在意义只有一个 —— **核对等比缩放**。实盘上想验证「应开 / 实开」是否正确，
要么真下单，要么把资金调大，两种都有代价。这里用固定资金（默认 1000 U）
离线跑一遍：

  他的累计名义 / 他的钱包余额 = 缩放系数
  我的资金 × 缩放系数          = 应开名义
  应开名义 / 限价 / 步进        = 应开数量

和 `CopyEngine.rebalance()` 调的是同一个 `target_position()`，所以这里算出来的
就是实盘会下的数。两边各写一套的话，排查时看到的数可能和实际下单不是一回事。

模拟成交按「更优价限价」记成本（不计手续费、不计滑点），平仓按他的减仓比例
等比缩。这是账面推演，不是回测，不预测盈亏。

  py -3.12 sim_portfolio.py --equity 1000     # 用实盘数据打印一张表
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from copy_engine import target_position          # noqa: E402  单一真源
from feed import Rule                            # noqa: E402


@dataclass
class SimLeg:
    """一个品种一个方向上的模拟持仓。"""
    symbol: str
    side: str
    qty: float = 0.0            # 模拟持仓数量
    cost: float = 0.0           # 模拟持仓成本（名义，按限价成交累计）
    avg: float = 0.0            # 模拟持仓均价
    lead_qty: float = 0.0       # 他的累计数量（用于算平仓比例）
    lead_notional: float = 0.0  # 他的累计名义
    target_qty: float = 0.0     # 按当前资金算出的应开数量
    target_notional: float = 0.0
    limit: float = 0.0          # 应开用的限价
    ratio: float = 0.0          # 他的名义 / 他的钱包
    opens: int = 0
    closes: int = 0

    def add(self, qty: float, price: float) -> None:
        self.cost += qty * price
        self.qty += qty
        self.avg = self.cost / self.qty if self.qty else 0.0

    def trim(self, ratio: float) -> None:
        ratio = max(0.0, min(1.0, ratio))
        self.qty *= (1.0 - ratio)
        self.cost *= (1.0 - ratio)
        self.avg = self.cost / self.qty if self.qty else 0.0


@dataclass
class SimResult:
    equity: float
    wallet: float
    legs: list[SimLeg] = field(default_factory=list)
    skipped: int = 0

    @property
    def total_notional(self) -> float:
        return sum(l.qty * l.avg for l in self.legs if l.qty > 0)

    def margin_used(self, leverage: int = 20) -> float:
        return self.total_notional / max(1, leverage)


class SimPortfolio:
    """无状态重放：每次 `replay()` 都从空仓开始，结果只取决于输入。

    无状态是刻意的 —— 有状态就会和实盘一样攒下历史误差，排查时反而分不清
    是数据错了还是账本攒错了。
    """

    def __init__(self, equity: float = 1000.0, leverage: int = 20):
        self.equity = float(equity)
        self.leverage = int(leverage)

    def replay(self, fills, wallet: float, tick_of, step_of,
               rule: Rule | None = None) -> SimResult:
        rule = rule or Rule()
        res = SimResult(equity=self.equity, wallet=float(wallet or 0.0))
        if not fills or res.wallet <= 0 or self.equity <= 0:
            return res
        legs: dict[tuple[str, str], SimLeg] = {}
        for f in sorted(fills, key=lambda x: x.ts):
            key = (f.symbol, f.side)
            leg = legs.setdefault(key, SimLeg(f.symbol, f.side))
            if f.action == "OPEN":
                leg.lead_qty += f.qty
                leg.lead_notional += f.notional
                t = target_position(leg.lead_notional, leg.lead_qty, f.side,
                                    res.wallet, self.equity,
                                    tick_of(f.symbol), step_of(f.symbol), rule)
                leg.target_qty = t["target_qty"]
                leg.target_notional = t["target_notional"]
                leg.limit = t["limit"]
                leg.ratio = t["ratio"]
                gap = t["target_qty"] - leg.qty
                # 和引擎同一个门槛：差额太小不动，避免抖来抖去
                if gap > 0 and gap * t["limit"] >= max(
                        5.0, t["target_notional"] * 0.05):
                    leg.add(gap, t["limit"])
                    leg.opens += 1
            else:
                # 平仓：按他这一笔占「平仓前持仓」的比例缩我们的仓
                before = leg.lead_qty
                ratio = (f.qty / before) if before > 0 else 1.0
                leg.lead_qty = max(0.0, before - f.qty)
                leg.lead_notional = max(0.0, leg.lead_notional - f.notional)
                leg.trim(ratio)
                leg.closes += 1
                if leg.lead_qty <= 0:
                    leg.lead_qty = 0.0
                    leg.lead_notional = 0.0
                    leg.qty = 0.0
                    leg.cost = 0.0
                    leg.avg = 0.0
        res.legs = sorted(legs.values(),
                          key=lambda l: (-l.lead_notional, l.symbol))
        return res


def main() -> None:
    ap = argparse.ArgumentParser(description="模拟仓：核对应开数量")
    ap.add_argument("--equity", type=float, default=1000.0,
                    help="模拟资金（USDT），默认 1000")
    ap.add_argument("--leverage", type=int, default=20)
    ap.add_argument("--portfolio", default=None, help="带单员 portfolioId")
    args = ap.parse_args()

    from account import load_env
    load_env()
    from api_feed import LeadApi
    from position_ledger import replay as replay_lead

    pid = args.portfolio or "5075281354358777856"
    api = LeadApi(pid)
    fills = api.fills()
    detail = api.detail()
    rep = replay_lead(fills)
    from position_ledger import wallet_state
    ws = wallet_state(rep, float(detail["marginBalance"]), {})
    wallet = ws.wallet or float(detail["marginBalance"])

    from feed import Rule as _Rule
    rule = _Rule()
    sim = SimPortfolio(args.equity, args.leverage)
    # 没有交易所连接时用他的成交价当 tick/step 的近似值
    res = sim.replay(fills, wallet, lambda s: 0.01, lambda s: 0.001, rule)

    print(f"模拟资金 {args.equity:.2f} U · 带单员钱包 {wallet:.2f} U · "
          f"缩放系数 {args.equity / wallet:.6f}")
    print(f"{'品种':<12}{'方向':<6}{'带单员名义':>14}{'占比':>9}"
          f"{'应开名义':>11}{'应开数量':>12}{'模拟数量':>12}{'模拟名义':>11}")
    for l in res.legs:
        if l.lead_notional <= 0 and l.qty <= 0:
            continue
        print(f"{l.symbol:<12}{'多' if l.side == 'LONG' else '空':<6}"
              f"{l.lead_notional:>14.2f}{l.ratio * 100:>8.3f}%"
              f"{l.target_notional:>11.2f}{l.target_qty:>12.6f}"
              f"{l.qty:>12.6f}{l.qty * l.avg:>11.2f}")
    print(f"合计模拟名义 {res.total_notional:.2f} U · "
          f"占用保证金 {res.margin_used(args.leverage):.2f} U")


if __name__ == "__main__":
    main()
