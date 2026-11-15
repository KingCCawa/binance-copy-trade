"""币安「最新交易记录」逐笔流水 -> chatdsn 的 DataProvider 接口。

公开数据是成交流水，不是持仓快照；这里把成交还原成虚拟持仓账本，
再交给现成的 FollowService 做差分、去重、平仓跟随。
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))

from chatdsn.api import DataProvider
from chatdsn.types import AgentPositions, Position

from feed import Fill, Rule, parse

logger = logging.getLogger(__name__)


@dataclass
class Lot:
    symbol: str
    side: str
    qty: float = 0.0
    cost: float = 0.0            # Σ price*qty，仅用于核对，不参与均价计算
    notional: float = 0.0
    entry_oid: str = ""
    opened_at: str = ""

    @property
    def avg(self) -> float:
        # 以页面给出的「总价值」为准（成交额是权威值），不用 Σ(显示均价×数量)，
        # 后者因页面四舍五入会有约 1e-5 的偏差，会算出两个不同的加权均价。
        return self.notional / self.qty if self.qty > 1e-12 else 0.0


class LeadFeedProvider(DataProvider):
    """把带单本流水包装成 chatdsn 能消费的 AgentPositions。"""

    def __init__(self, source, rule: Rule | None = None, agent_name: str = "lead"):
        self.source = source
        self.rule = rule or Rule()
        self.agent_name = agent_name
        self.lots: Dict[tuple, Lot] = {}
        self.orphans: List[Fill] = []
        self.seen: set = set()

    def list_available_agents(self) -> List[str]:
        return [self.agent_name]

    def _apply(self, f: Fill) -> None:
        if f.key in self.seen:
            return
        self.seen.add(f.key)

        key = (f.symbol, f.side)
        lot = self.lots.setdefault(key, Lot(f.symbol, f.side))

        if f.action == "OPEN":
            if lot.qty <= 1e-12:
                lot.entry_oid = f.key
                lot.opened_at = f.ts
            lot.qty += f.qty
            lot.cost += f.price * f.qty
            lot.notional += f.notional
        else:
            if lot.qty <= 1e-12:
                # 对应开仓不在抓取视窗内：只记录，绝不去平自己的仓
                self.orphans.append(f)
                logger.warning("孤儿平仓，跳过: %s", f.key)
                return
            ratio = max(0.0, 1.0 - f.qty / lot.qty)
            lot.qty = max(0.0, lot.qty - f.qty)
            lot.cost *= ratio
            lot.notional *= ratio
            if lot.qty <= 1e-12:
                lot.cost = 0.0
                lot.notional = 0.0
                lot.entry_oid = ""

    def get_agent_positions(self, agent_name: str | None = None) -> AgentPositions:
        raw = self.source() if callable(self.source) else self.source
        # source 可以返回中文流水文本（App/网页粘贴），也可以直接返回 List[Fill]
        # （api_feed.py 直连接口时，避免文本往返丢精度）
        fills = list(raw) if isinstance(raw, (list, tuple)) else parse(raw)
        # 同一分钟里先处理开仓再处理平仓，避免账本顺序错乱
        for f in sorted(fills, key=lambda x: (x.ts, x.action != "OPEN")):
            self._apply(f)

        positions = []
        for lot in self.lots.values():
            if lot.qty <= 1e-12:
                continue
            # 敞口比例复刻：我的名义 = 我的权益 × (他的名义 / 他的带单余额)
            my_notional = self.rule.my_equity * (lot.notional / self.rule.lead_balance)
            my_margin = my_notional / self.rule.leverage
            positions.append(Position(
                coin=lot.symbol,
                side=lot.side,
                entry_price=lot.avg,
                quantity=lot.qty,
                leverage=self.rule.leverage,
                margin=my_margin,
                entry_oid=lot.entry_oid,
                tp_price=None,       # 公开流水没有预设止盈止损价
                sl_price=None,
            ))

        return AgentPositions(
            agent_name=agent_name or self.agent_name,
            positions=positions,
            total_balance=self.rule.lead_balance,
            total_margin=sum(p.margin for p in positions),
        )
