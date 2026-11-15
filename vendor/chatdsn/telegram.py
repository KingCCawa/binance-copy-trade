"""Telegram notification service."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TelegramService:
    """Simple Telegram notification service."""

    def __init__(self, token: str, chat_id: str):
        """Initialize Telegram service."""
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

        if not self.enabled:
            logger.warning("Telegram service disabled (token or chat_id missing)")
            return

        # Try to import requests
        try:
            import requests
            self.requests = requests
            self.base_url = f"https://api.telegram.org/bot{token}"
        except ImportError:
            logger.error("requests library required for Telegram")
            self.enabled = False

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message via Telegram."""
        if not self.enabled:
            return False

        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }

            response = self.requests.post(url, json=data, timeout=10)
            response.raise_for_status()

            logger.debug("Telegram message sent")
            return True

        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def send_trade_notification(self, action: str, symbol: str, side: str,
                               price: float, quantity: float, **kwargs) -> bool:
        """Send formatted trade notification."""
        emoji = "✅" if action == "OPEN" else "🔴"

        text = (
            f"{emoji} <b>{action} {side}</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Price: {price:.4f}\n"
            f"Quantity: {quantity:.4f}"
        )

        # Add optional fields
        if "leverage" in kwargs:
            text += f"\nLeverage: {kwargs['leverage']}x"
        if "margin" in kwargs:
            text += f"\nMargin: {kwargs['margin']:.2f} USDT"
        if "tp_price" in kwargs and kwargs["tp_price"]:
            text += f"\nTP: {kwargs['tp_price']:.4f}"
        if "sl_price" in kwargs and kwargs["sl_price"]:
            text += f"\nSL: {kwargs['sl_price']:.4f}"
        if "reason" in kwargs:
            text += f"\n\nReason: {kwargs['reason']}"

        return self.send_message(text)

    def send_error(self, error_msg: str) -> bool:
        """Send error notification."""
        text = f"❌ <b>Error</b>\n{error_msg}"
        return self.send_message(text)

    def test(self) -> bool:
        """Test Telegram connection."""
        return self.send_message("🤖 ChatDSN Bot Test - Connection OK!")
