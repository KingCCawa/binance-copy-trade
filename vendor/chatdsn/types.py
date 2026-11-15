"""Core data types using dataclasses."""

from dataclasses import dataclass, field
from typing import Optional, List, Literal
from enum import Enum


class PositionSide(Enum):
    """Position side enum."""
    LONG = "LONG"
    SHORT = "SHORT"


class MarginMode(Enum):
    """Margin mode enum."""
    ISOLATED = "ISOLATED"
    CROSSED = "CROSSED"


class CapitalMode(Enum):
    """Capital allocation mode."""
    PROPORTIONAL = "proportional"
    FIXED_AMOUNT = "fixed_amount"


@dataclass
class Position:
    """Represents a trading position."""
    coin: str
    side: str  # "LONG" or "SHORT"
    entry_price: float
    quantity: float
    leverage: int
    margin: float
    entry_oid: str
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    unrealized_pnl: float = 0.0

    def __post_init__(self):
        """Normalize types."""
        self.entry_price = float(self.entry_price)
        self.quantity = float(self.quantity)
        self.leverage = int(self.leverage)
        self.margin = float(self.margin)
        if self.tp_price:
            self.tp_price = float(self.tp_price)
        if self.sl_price:
            self.sl_price = float(self.sl_price)


@dataclass
class AgentPositions:
    """Agent positions response."""
    agent_name: str
    positions: List[Position] = field(default_factory=list)
    total_balance: float = 0.0
    total_margin: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class OrderRecord:
    """Order history record."""
    entry_oid: str
    coin: str
    side: str
    entry_price: float
    quantity: float
    leverage: int
    margin: float
    timestamp: str
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    profit_exit: bool = False
    manual_close: bool = False


@dataclass
class RiskAssessment:
    """Risk assessment result."""
    is_favorable: bool
    risk_score: float
    warnings: List[str] = field(default_factory=list)
    suggested_quantity: Optional[float] = None
    price_diff_pct: float = 0.0


@dataclass
class FollowState:
    """Follow service state."""
    agent_name: str
    last_positions: List[Position] = field(default_factory=list)
    active_entry_oids: List[str] = field(default_factory=list)

    def get_position_by_coin(self, coin: str) -> Optional[Position]:
        """Get position by coin symbol."""
        for pos in self.last_positions:
            if pos.coin == coin:
                return pos
        return None
