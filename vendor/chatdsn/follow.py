"""Follow service for copy trading AI agents."""

import logging
from datetime import datetime
from typing import Optional, List
from .types import Position, FollowState, OrderRecord
from .api import ApiClient
from .binance import BinanceService
from .trading import TradingExecutor
from .capital import CapitalManager
from .history import OrderHistoryManager

logger = logging.getLogger(__name__)


class FollowService:
    """Service for following and copying agent positions."""

    def __init__(self, api_client: ApiClient, binance: BinanceService,
                 executor: TradingExecutor, capital_manager: CapitalManager,
                 history_manager: OrderHistoryManager):
        """Initialize follow service."""
        self.api = api_client
        self.binance = binance
        self.executor = executor
        self.capital = capital_manager
        self.history = history_manager
        self.state = FollowState(agent_name="")
        self.telegram = None

    def set_telegram(self, telegram_service):
        """Set Telegram service for notifications."""
        self.telegram = telegram_service

    def detect_changes(self, current_positions: List[Position]) -> dict:
        """
        Detect position changes from last state.

        Returns dict with:
        - new_positions: List of new positions opened
        - closed_positions: List of positions closed
        - modified_positions: List of positions with changed entry_oid
        """
        changes = {
            "new_positions": [],
            "closed_positions": [],
            "modified_positions": [],
        }

        current_coins = {pos.coin for pos in current_positions}
        last_coins = {pos.coin for pos in self.state.last_positions}

        # Detect new positions
        for pos in current_positions:
            if pos.coin not in last_coins:
                changes["new_positions"].append(pos)
                logger.info(f"Detected NEW position: {pos.coin} {pos.side}")

        # Detect closed positions
        for pos in self.state.last_positions:
            if pos.coin not in current_coins:
                changes["closed_positions"].append(pos)
                logger.info(f"Detected CLOSED position: {pos.coin}")

        # Detect modified positions (entry OID changed)
        for curr_pos in current_positions:
            last_pos = self.state.get_position_by_coin(curr_pos.coin)
            if last_pos and curr_pos.entry_oid != last_pos.entry_oid:
                changes["modified_positions"].append((last_pos, curr_pos))
                logger.info(
                    f"Detected MODIFIED position: {curr_pos.coin} "
                    f"(OID: {last_pos.entry_oid} → {curr_pos.entry_oid})"
                )

        return changes

    def handle_new_position(self, position: Position) -> bool:
        """Handle new position opened by agent."""
        symbol = position.coin

        # Check if already in history (deduplication)
        if self.history.has_entry_oid(position.entry_oid):
            logger.info(f"Position {symbol} already processed, skipping")
            return False

        # Get allocation
        allocations = self.capital.get_allocation([position])
        allocated_margin = allocations.get(symbol, 0)

        if allocated_margin <= 0:
            logger.error(f"No margin allocated for {symbol}")
            return False

        # Execute trade
        success = self.executor.open_position(position, allocated_margin, self.telegram)

        if success:
            # Add to history
            order_record = OrderRecord(
                entry_oid=position.entry_oid,
                coin=position.coin,
                side=position.side,
                entry_price=position.entry_price,
                quantity=position.quantity,
                leverage=position.leverage,
                margin=allocated_margin,
                timestamp=datetime.utcnow().isoformat(),
                tp_price=position.tp_price,
                sl_price=position.sl_price,
            )
            self.history.add_order(order_record)

        return success

    def handle_closed_position(self, position: Position) -> bool:
        """Handle position closed by agent."""
        symbol = position.coin

        # 改动：不管我方有没有仓位，都必须撤掉该品种的全部挂单。
        # 更优价限价单经常还没成交对方就已止损平仓；若不撤单，价格回落时
        # 会自动成交成一笔对方早已平掉、我方仍在持有的幽灵仓位。
        cancelled = self.binance.cancel_all_orders(symbol)
        logger.info("Cancelled all open orders for %s: %s", symbol, cancelled)

        our_positions = self.binance.get_positions()
        has_position = any(p["symbol"] == symbol for p in our_positions)
        if not has_position:
            logger.info("No position for %s, only cancelled pending orders", symbol)
            return True

        return self.executor.close_position(
            symbol, reason="Agent closed position", telegram_service=self.telegram
        )

    def handle_modified_position(self, old_pos: Position, new_pos: Position) -> bool:
        """Handle position entry OID change (switch detected)."""
        symbol = new_pos.coin

        logger.info(f"Handling position switch for {symbol}")

        # Close old position
        close_success = self.executor.close_position(
            symbol, reason="Position switch detected", telegram_service=self.telegram
        )

        if not close_success:
            logger.error(f"Failed to close old position {symbol}")
            return False

        # Open new position
        return self.handle_new_position(new_pos)

    def process_changes(self, changes: dict) -> dict:
        """Process all detected changes."""
        results = {
            "new_opened": 0,
            "closed": 0,
            "switched": 0,
            "errors": 0,
        }

        # Handle new positions
        for pos in changes["new_positions"]:
            try:
                if self.handle_new_position(pos):
                    results["new_opened"] += 1
                else:
                    results["errors"] += 1
            except Exception as e:
                logger.error(f"Error handling new position {pos.coin}: {e}")
                results["errors"] += 1

        # Handle closed positions
        for pos in changes["closed_positions"]:
            try:
                if self.handle_closed_position(pos):
                    results["closed"] += 1
                else:
                    results["errors"] += 1
            except Exception as e:
                logger.error(f"Error handling closed position {pos.coin}: {e}")
                results["errors"] += 1

        # Handle modified positions
        for old_pos, new_pos in changes["modified_positions"]:
            try:
                if self.handle_modified_position(old_pos, new_pos):
                    results["switched"] += 1
                else:
                    results["errors"] += 1
            except Exception as e:
                logger.error(f"Error handling modified position {new_pos.coin}: {e}")
                results["errors"] += 1

        return results

    def check_manual_closes(self) -> List[str]:
        """
        Detect manually closed positions.

        Returns list of coins that were manually closed.
        """
        manually_closed = []

        # Get our current positions
        our_positions = {p["symbol"] for p in self.binance.get_positions()}

        # Check if any tracked positions are no longer open
        for entry_oid in self.state.active_entry_oids[:]:
            order = self.history.get_order_by_oid(entry_oid)
            if not order:
                continue

            if order.coin not in our_positions:
                # Position closed but not by us
                logger.info(f"Detected manual close: {order.coin}")
                self.history.mark_manual_close(entry_oid)
                self.state.active_entry_oids.remove(entry_oid)
                manually_closed.append(order.coin)

        return manually_closed

    def update_state(self, positions: List[Position]):
        """Update internal state with current positions."""
        self.state.last_positions = positions
        self.state.active_entry_oids = [pos.entry_oid for pos in positions]

    def follow_once(self, agent_name: str) -> dict:
        """
        Execute one follow iteration.

        Returns dict with results.
        """
        logger.info(f"Checking agent: {agent_name}")

        # Fetch agent positions
        agent_data = self.api.get_agent_positions(agent_name)
        if not agent_data:
            logger.error(f"Failed to fetch positions for {agent_name}")
            return {"error": "Failed to fetch positions"}

        current_positions = agent_data.positions
        logger.info(f"Agent has {len(current_positions)} positions")

        # Update state if first run
        if not self.state.agent_name:
            self.state.agent_name = agent_name
            self.update_state(current_positions)
            logger.info("Initial state saved")
            return {"status": "initialized"}

        # Detect changes
        changes = self.detect_changes(current_positions)

        # Check for manual closes
        manually_closed = self.check_manual_closes()
        if manually_closed and self.telegram:
            self.telegram.send_message(
                f"⚠️ Detected manual close: {', '.join(manually_closed)}"
            )

        # Process changes
        results = self.process_changes(changes)

        # Update state
        self.update_state(current_positions)

        # Add summary to results
        results["positions_count"] = len(current_positions)
        results["manually_closed"] = manually_closed

        return results
