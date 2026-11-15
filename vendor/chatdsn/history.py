"""Order history manager for persistent state."""

import json
import logging
from pathlib import Path
from typing import List, Optional
from datetime import datetime
from .types import OrderRecord

logger = logging.getLogger(__name__)


class OrderHistoryManager:
    """Manages order history with JSON persistence."""

    def __init__(self, history_file: Optional[Path] = None):
        """Initialize history manager."""
        if history_file is None:
            history_file = Path("data/order-history.json")

        self.history_file = history_file
        self.orders: List[OrderRecord] = []

        # Ensure data directory exists
        self.history_file.parent.mkdir(parents=True, exist_ok=True)

        # Load existing history
        self.load()

    def load(self):
        """Load order history from file."""
        if not self.history_file.exists():
            logger.info("No existing order history found")
            return

        try:
            with open(self.history_file) as f:
                data = json.load(f)

            self.orders = []
            for item in data:
                try:
                    order = OrderRecord(
                        entry_oid=item["entry_oid"],
                        coin=item["coin"],
                        side=item["side"],
                        entry_price=float(item["entry_price"]),
                        quantity=float(item["quantity"]),
                        leverage=int(item["leverage"]),
                        margin=float(item["margin"]),
                        timestamp=item["timestamp"],
                        tp_price=float(item["tp_price"]) if item.get("tp_price") else None,
                        sl_price=float(item["sl_price"]) if item.get("sl_price") else None,
                        profit_exit=item.get("profit_exit", False),
                        manual_close=item.get("manual_close", False),
                    )
                    self.orders.append(order)
                except (KeyError, ValueError, TypeError) as e:
                    logger.warning(f"Failed to parse order record: {e}")
                    continue

            logger.info(f"Loaded {len(self.orders)} order records")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse history file: {e}")
        except Exception as e:
            logger.error(f"Failed to load history: {e}")

    def save(self):
        """Save order history to file."""
        try:
            data = []
            for order in self.orders:
                data.append({
                    "entry_oid": order.entry_oid,
                    "coin": order.coin,
                    "side": order.side,
                    "entry_price": order.entry_price,
                    "quantity": order.quantity,
                    "leverage": order.leverage,
                    "margin": order.margin,
                    "timestamp": order.timestamp,
                    "tp_price": order.tp_price,
                    "sl_price": order.sl_price,
                    "profit_exit": order.profit_exit,
                    "manual_close": order.manual_close,
                })

            with open(self.history_file, "w") as f:
                json.dump(data, f, indent=2)

            logger.debug(f"Saved {len(self.orders)} order records")

        except Exception as e:
            logger.error(f"Failed to save history: {e}")

    def add_order(self, order: OrderRecord) -> bool:
        """Add order to history (with deduplication)."""
        # Check if already exists
        if self.has_entry_oid(order.entry_oid):
            logger.debug(f"Order {order.entry_oid} already in history")
            return False

        self.orders.append(order)
        self.save()
        logger.info(f"Added order to history: {order.coin} {order.side} @ {order.entry_price}")
        return True

    def has_entry_oid(self, entry_oid: str) -> bool:
        """Check if entry OID exists in history."""
        return any(order.entry_oid == entry_oid for order in self.orders)

    def get_order_by_oid(self, entry_oid: str) -> Optional[OrderRecord]:
        """Get order by entry OID."""
        for order in self.orders:
            if order.entry_oid == entry_oid:
                return order
        return None

    def get_orders_by_coin(self, coin: str) -> List[OrderRecord]:
        """Get all orders for a specific coin."""
        return [order for order in self.orders if order.coin == coin]

    def mark_profit_exit(self, entry_oid: str):
        """Mark an order as profit exit."""
        order = self.get_order_by_oid(entry_oid)
        if order:
            order.profit_exit = True
            self.save()
            logger.info(f"Marked {entry_oid} as profit exit")

    def mark_manual_close(self, entry_oid: str):
        """Mark an order as manually closed."""
        order = self.get_order_by_oid(entry_oid)
        if order:
            order.manual_close = True
            self.save()
            logger.info(f"Marked {entry_oid} as manual close")

    def get_recent_profit_exits(self, coin: str, limit: int = 1) -> List[OrderRecord]:
        """Get recent profit exits for a coin."""
        orders = [o for o in self.orders if o.coin == coin and o.profit_exit]
        return sorted(orders, key=lambda x: x.timestamp, reverse=True)[:limit]

    def get_recent_manual_closes(self, coin: str, limit: int = 1) -> List[OrderRecord]:
        """Get recent manual closes for a coin."""
        orders = [o for o in self.orders if o.coin == coin and o.manual_close]
        return sorted(orders, key=lambda x: x.timestamp, reverse=True)[:limit]

    def clear_history(self):
        """Clear all history (use with caution)."""
        self.orders = []
        self.save()
        logger.warning("Cleared all order history")

    def get_statistics(self) -> dict:
        """Get basic statistics."""
        if not self.orders:
            return {
                "total_orders": 0,
                "unique_coins": 0,
                "profit_exits": 0,
                "manual_closes": 0,
            }

        return {
            "total_orders": len(self.orders),
            "unique_coins": len(set(o.coin for o in self.orders)),
            "profit_exits": sum(1 for o in self.orders if o.profit_exit),
            "manual_closes": sum(1 for o in self.orders if o.manual_close),
        }
