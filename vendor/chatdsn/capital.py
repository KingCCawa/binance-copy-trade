"""Capital allocation manager for position sizing."""

import logging
from typing import List
from .types import Position

logger = logging.getLogger(__name__)


class CapitalManager:
    """Manages capital allocation across positions."""

    def __init__(self, mode: str = "proportional", total_margin: float = 1000,
                 fixed_margin: float = 100):
        """
        Initialize capital manager.

        Args:
            mode: "proportional" or "fixed_amount"
            total_margin: Total margin to allocate (proportional mode)
            fixed_margin: Fixed margin per position (fixed_amount mode)
        """
        self.mode = mode.lower()
        self.total_margin = total_margin
        self.fixed_margin = fixed_margin

        if self.mode not in ["proportional", "fixed_amount"]:
            raise ValueError(f"Invalid capital mode: {self.mode}")

    def calculate_quantity(self, position: Position, allocated_margin: float,
                          current_price: float) -> float:
        """
        Calculate quantity based on allocated margin.

        Formula: quantity = (allocated_margin * leverage) / current_price
        """
        if allocated_margin <= 0:
            return 0.0

        quantity = (allocated_margin * position.leverage) / current_price
        return quantity

    def allocate_proportional(self, positions: List[Position]) -> dict[str, float]:
        """
        Allocate margin proportionally based on agent's position weights.

        Returns dict: {coin: allocated_margin}
        """
        if not positions:
            return {}

        # Calculate total agent margin
        total_agent_margin = sum(pos.margin for pos in positions)

        if total_agent_margin == 0:
            logger.warning("Total agent margin is 0, cannot allocate proportionally")
            return {}

        # Allocate proportionally
        allocations = {}
        for pos in positions:
            weight = pos.margin / total_agent_margin
            allocated = self.total_margin * weight
            allocations[pos.coin] = allocated

            logger.debug(
                f"{pos.coin}: weight={weight:.2%}, allocated={allocated:.2f} USDT"
            )

        return allocations

    def allocate_fixed(self, positions: List[Position]) -> dict[str, float]:
        """
        Allocate fixed margin per position.

        Returns dict: {coin: allocated_margin}
        """
        allocations = {}
        for pos in positions:
            allocations[pos.coin] = self.fixed_margin
            logger.debug(f"{pos.coin}: allocated={self.fixed_margin:.2f} USDT (fixed)")

        return allocations

    def get_allocation(self, positions: List[Position]) -> dict[str, float]:
        """Get margin allocation based on configured mode."""
        if self.mode == "proportional":
            return self.allocate_proportional(positions)
        elif self.mode == "fixed_amount":
            return self.allocate_fixed(positions)
        else:
            raise ValueError(f"Unknown capital mode: {self.mode}")

    def calculate_position_quantities(self, positions: List[Position],
                                     current_prices: dict[str, float]) -> dict[str, float]:
        """
        Calculate quantities for all positions.

        Returns dict: {coin: quantity}
        """
        allocations = self.get_allocation(positions)
        quantities = {}

        for pos in positions:
            if pos.coin not in current_prices:
                logger.warning(f"No current price for {pos.coin}, skipping")
                continue

            allocated_margin = allocations.get(pos.coin, 0)
            current_price = current_prices[pos.coin]

            quantity = self.calculate_quantity(pos, allocated_margin, current_price)
            quantities[pos.coin] = quantity

            logger.info(
                f"{pos.coin}: margin={allocated_margin:.2f}, "
                f"price={current_price:.2f}, quantity={quantity:.4f}"
            )

        return quantities

    def validate_total_margin(self, positions: List[Position],
                             available_balance: float) -> tuple[bool, str]:
        """
        Validate if we have enough balance for positions.

        Returns (is_valid, message)
        """
        allocations = self.get_allocation(positions)
        total_required = sum(allocations.values())

        if total_required > available_balance:
            return False, (
                f"Insufficient balance: need {total_required:.2f} USDT, "
                f"have {available_balance:.2f} USDT"
            )

        if total_required > available_balance * 0.95:
            return True, (
                f"Warning: Using {total_required/available_balance:.1%} of balance"
            )

        return True, f"Balance OK: using {total_required:.2f} / {available_balance:.2f} USDT"
