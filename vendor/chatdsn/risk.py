"""Risk management and assessment."""

import logging
from typing import List
from .types import Position, RiskAssessment

logger = logging.getLogger(__name__)


class RiskManager:
    """Risk assessment and management."""

    # 改动（外场跟单）：原版 1.0 会放行「比信号价差 1% 以内」的单，
    # 与「只在他更好的位置开」冲突。改成 0.0 = 只接受已更优的价格。
    def __init__(self, default_tolerance_pct: float = 0.0,
                 max_position_size_pct: float = 50.0):
        """
        Initialize risk manager.

        Args:
            default_tolerance_pct: Default price tolerance percentage
            max_position_size_pct: Maximum position size as % of balance
        """
        self.default_tolerance_pct = default_tolerance_pct
        self.max_position_size_pct = max_position_size_pct
        self._symbol_tolerances: dict[str, float] = {}

    def set_symbol_tolerance(self, symbol: str, tolerance_pct: float):
        """Set custom tolerance for a symbol."""
        self._symbol_tolerances[symbol] = tolerance_pct

    def get_tolerance(self, symbol: str) -> float:
        """Get tolerance for a symbol."""
        return self._symbol_tolerances.get(symbol, self.default_tolerance_pct)

    def assess_price(self, position: Position, current_price: float) -> RiskAssessment:
        """
        Assess if current price is favorable for entry.

        For LONG: favorable if current price < agent's entry price (buy low)
        For SHORT: favorable if current price > agent's entry price (sell high)
        """
        warnings = []
        tolerance_pct = self.get_tolerance(position.coin)

        # Calculate price difference
        price_diff = current_price - position.entry_price
        price_diff_pct = (price_diff / position.entry_price) * 100

        # Directional check
        is_favorable = False
        if position.side == "LONG":
            # For LONG, we want to buy when price is lower or within tolerance
            if current_price <= position.entry_price:
                is_favorable = True
            elif price_diff_pct <= tolerance_pct:
                is_favorable = True
                warnings.append(
                    f"Price {price_diff_pct:.2f}% higher but within {tolerance_pct}% tolerance"
                )
            else:
                warnings.append(
                    f"Price {price_diff_pct:.2f}% too high for LONG "
                    f"(tolerance: {tolerance_pct}%)"
                )

        else:  # SHORT
            # For SHORT, we want to sell when price is higher or within tolerance
            if current_price >= position.entry_price:
                is_favorable = True
            elif abs(price_diff_pct) <= tolerance_pct:
                is_favorable = True
                warnings.append(
                    f"Price {abs(price_diff_pct):.2f}% lower but within "
                    f"{tolerance_pct}% tolerance"
                )
            else:
                warnings.append(
                    f"Price {abs(price_diff_pct):.2f}% too low for SHORT "
                    f"(tolerance: {tolerance_pct}%)"
                )

        # Calculate risk score (0-100, lower is better)
        risk_score = min(abs(price_diff_pct) * 10, 100)

        return RiskAssessment(
            is_favorable=is_favorable,
            risk_score=risk_score,
            warnings=warnings,
            price_diff_pct=price_diff_pct,
        )

    def assess_position_size(self, margin: float, total_balance: float) -> RiskAssessment:
        """Assess if position size is within risk limits."""
        warnings = []
        size_pct = (margin / total_balance) * 100

        is_favorable = size_pct <= self.max_position_size_pct

        if not is_favorable:
            warnings.append(
                f"Position size {size_pct:.1f}% exceeds maximum "
                f"{self.max_position_size_pct}%"
            )
        elif size_pct > self.max_position_size_pct * 0.8:
            warnings.append(
                f"Position size {size_pct:.1f}% is close to maximum "
                f"{self.max_position_size_pct}%"
            )

        risk_score = min((size_pct / self.max_position_size_pct) * 100, 100)

        return RiskAssessment(
            is_favorable=is_favorable,
            risk_score=risk_score,
            warnings=warnings,
        )

    def assess_leverage(self, leverage: int, max_leverage: int = 50) -> RiskAssessment:
        """Assess leverage risk."""
        warnings = []
        is_favorable = leverage <= max_leverage

        if leverage > max_leverage:
            warnings.append(f"Leverage {leverage}x exceeds maximum {max_leverage}x")
        elif leverage > max_leverage * 0.8:
            warnings.append(f"High leverage {leverage}x - increased liquidation risk")

        risk_score = min((leverage / max_leverage) * 100, 100)

        return RiskAssessment(
            is_favorable=is_favorable,
            risk_score=risk_score,
            warnings=warnings,
        )

    def assess_all(self, position: Position, current_price: float,
                  total_balance: float, allocated_margin: float) -> RiskAssessment:
        """Comprehensive risk assessment."""
        assessments = [
            self.assess_price(position, current_price),
            self.assess_position_size(allocated_margin, total_balance),
            self.assess_leverage(position.leverage),
        ]

        # Combine results
        all_favorable = all(a.is_favorable for a in assessments)
        avg_risk_score = sum(a.risk_score for a in assessments) / len(assessments)
        all_warnings = []
        for a in assessments:
            all_warnings.extend(a.warnings)

        return RiskAssessment(
            is_favorable=all_favorable,
            risk_score=avg_risk_score,
            warnings=all_warnings,
        )
