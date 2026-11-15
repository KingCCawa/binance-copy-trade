"""Trading execution engine."""

import logging
from typing import Optional
from .types import Position
from .binance import BinanceService
from .risk import RiskManager
from .capital import CapitalManager

logger = logging.getLogger(__name__)


class TradingExecutor:
    """Executes trades on Binance."""

    def __init__(self, binance: BinanceService, risk_manager: RiskManager,
                 capital_manager: CapitalManager, dry_run: bool = False):
        """
        Initialize trading executor.

        Args:
            binance: Binance service instance
            risk_manager: Risk manager instance
            capital_manager: Capital manager instance
            dry_run: If True, only simulate trades
        """
        self.binance = binance
        self.risk_manager = risk_manager
        self.capital_manager = capital_manager
        self.dry_run = dry_run

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current market price for a symbol."""
        try:
            import requests
            base_url = self.binance.base_url
            response = requests.get(
                f"{base_url}/fapi/v1/ticker/price",
                params={"symbol": symbol},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()
            return float(data["price"])
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return None

    def open_position(self, position: Position, allocated_margin: float,
                     telegram_service=None) -> bool:
        """
        Open a new position.

        Steps:
        1. Get current price
        2. Assess risk
        3. Set leverage and margin mode
        4. Calculate quantity
        5. Place market order
        6. Place TP and SL orders
        """
        symbol = position.coin
        logger.info(f"Opening position: {symbol} {position.side}")

        # Get current price
        current_price = self.get_current_price(symbol)
        if not current_price:
            logger.error(f"Cannot get price for {symbol}")
            return False

        # Assess risk
        balance_info = self.binance.get_balance()
        if not balance_info:
            logger.error("Cannot get balance")
            return False

        available_balance = balance_info["available_balance"]
        assessment = self.risk_manager.assess_all(
            position, current_price, available_balance, allocated_margin
        )

        # Log warnings
        for warning in assessment.warnings:
            logger.warning(warning)

        if not assessment.is_favorable:
            logger.error(f"Risk assessment failed for {symbol}")
            if telegram_service:
                telegram_service.send_message(
                    f"⚠️ Risk check FAILED for {symbol}\n" +
                    "\n".join(assessment.warnings)
                )
            return False

        logger.info(f"Risk score: {assessment.risk_score:.1f}/100")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would open {symbol} {position.side} @ {current_price}")
            return True

        # Set leverage
        if not self.binance.set_leverage(symbol, position.leverage):
            logger.warning(f"Failed to set leverage (may already be set)")

        # Set margin mode (ISOLATED or CROSSED)
        margin_mode = "ISOLATED"  # from config
        self.binance.set_margin_mode(symbol, margin_mode)

        # Calculate quantity
        quantity = self.capital_manager.calculate_quantity(
            position, allocated_margin, current_price
        )
        quantity = self.binance.format_quantity(symbol, quantity)

        if quantity <= 0:
            logger.error(f"Invalid quantity: {quantity}")
            return False

        logger.info(f"Calculated quantity: {quantity} {symbol}")

        # Place market order
        side = "BUY" if position.side == "LONG" else "SELL"
        order = self.binance.place_market_order(symbol, side, quantity)

        if not order:
            logger.error(f"Failed to place market order for {symbol}")
            if telegram_service:
                telegram_service.send_message(f"❌ Failed to open {symbol} position")
            return False

        logger.info(f"Market order placed: {order}")

        # Place TP order if specified
        if position.tp_price:
            tp_side = "SELL" if position.side == "LONG" else "BUY"
            tp_order = self.binance.place_limit_order(
                symbol, tp_side, quantity, position.tp_price, reduce_only=True
            )
            if tp_order:
                logger.info(f"TP order placed @ {position.tp_price}")
            else:
                logger.warning("Failed to place TP order")

        # Place SL order if specified
        if position.sl_price:
            sl_side = "SELL" if position.side == "LONG" else "BUY"
            sl_order = self.binance.place_stop_market_order(
                symbol, sl_side, quantity, position.sl_price, reduce_only=True
            )
            if sl_order:
                logger.info(f"SL order placed @ {position.sl_price}")
            else:
                logger.warning("Failed to place SL order")

        # Send Telegram notification
        if telegram_service:
            msg = (
                f"✅ Opened {position.side} position\n"
                f"Coin: {symbol}\n"
                f"Entry: {current_price:.4f}\n"
                f"Quantity: {quantity:.4f}\n"
                f"Leverage: {position.leverage}x\n"
                f"Margin: {allocated_margin:.2f} USDT"
            )
            if position.tp_price:
                msg += f"\nTP: {position.tp_price:.4f}"
            if position.sl_price:
                msg += f"\nSL: {position.sl_price:.4f}"

            telegram_service.send_message(msg)

        return True

    def close_position(self, symbol: str, reason: str = "",
                      telegram_service=None,
                      quantity: float | None = None) -> bool:
        """
        Close an existing position.

        Steps:
        1. Cancel all open orders for the symbol
        2. Close position with market order
        """
        logger.info(f"Closing position: {symbol} (reason: {reason})")

        if self.dry_run:
            logger.info(f"[DRY RUN] Would close {symbol}")
            return True

        # Cancel all orders first
        if not self.binance.cancel_all_orders(symbol):
            logger.warning(f"Failed to cancel orders for {symbol}")

        # Close position
        if not self.binance.close_position(symbol, quantity=quantity):
            logger.error(f"Failed to close position for {symbol}")
            return False

        logger.info(f"Position closed: {symbol}")

        # Send Telegram notification
        if telegram_service:
            msg = f"🔴 Closed position: {symbol}"
            if reason:
                msg += f"\nReason: {reason}"
            telegram_service.send_message(msg)

        return True

    def update_stop_loss(self, symbol: str, side: str, quantity: float,
                        new_sl_price: float) -> bool:
        """Update stop-loss order."""
        if self.dry_run:
            logger.info(f"[DRY RUN] Would update SL for {symbol} to {new_sl_price}")
            return True

        # Cancel existing SL orders
        orders = self.binance.get_open_orders(symbol)
        for order in orders:
            if order["type"] == "STOP_MARKET":
                self.binance.cancel_order(symbol, order["order_id"])

        # Place new SL
        sl_side = "SELL" if side == "LONG" else "BUY"
        order = self.binance.place_stop_market_order(
            symbol, sl_side, quantity, new_sl_price, reduce_only=True
        )

        return order is not None

    def update_take_profit(self, symbol: str, side: str, quantity: float,
                          new_tp_price: float) -> bool:
        """Update take-profit order."""
        if self.dry_run:
            logger.info(f"[DRY RUN] Would update TP for {symbol} to {new_tp_price}")
            return True

        # Cancel existing TP orders
        orders = self.binance.get_open_orders(symbol)
        for order in orders:
            if order["type"] == "LIMIT" and order.get("reduce_only"):
                self.binance.cancel_order(symbol, order["order_id"])

        # Place new TP
        tp_side = "SELL" if side == "LONG" else "BUY"
        order = self.binance.place_limit_order(
            symbol, tp_side, quantity, new_tp_price, reduce_only=True
        )

        return order is not None
