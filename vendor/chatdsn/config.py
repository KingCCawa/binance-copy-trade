"""Configuration management using environment variables."""

import os
import logging
from typing import Optional, Dict
from pathlib import Path


class Config:
    """Configuration manager with environment variable support."""

    def __init__(self):
        """Initialize configuration from environment."""
        # API Source
        self.nof1_api_base = os.getenv("NOF1_API_BASE_URL", "https://nof1.ai/api")

        # Binance
        self.binance_api_key = os.getenv("BINANCE_API_KEY", "")
        self.binance_api_secret = os.getenv("BINANCE_API_SECRET", "")
        self.binance_testnet = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

        # Trading Settings
        # 改动（外场跟单）：带单员标签是 10x-20x，按上沿复刻，不缩水
        self.default_leverage = int(os.getenv("DEFAULT_LEVERAGE", "20"))
        self.margin_mode = os.getenv("MARGIN_MODE", "ISOLATED").upper()
        self.capital_mode = os.getenv("CAPITAL_MODE", "proportional").lower()
        self.fixed_margin_per_coin = float(os.getenv("FIXED_MARGIN_PER_COIN", "100"))
        self.total_margin = float(os.getenv("TOTAL_MARGIN", "1000"))

        # Risk Management
        # 改动（外场跟单）：0 = 不接受任何劣于信号价的执行
        self.default_price_tolerance_pct = float(os.getenv("DEFAULT_PRICE_TOLERANCE_PCT", "0.0"))
        self.max_position_size_pct = float(os.getenv("MAX_POSITION_SIZE_PCT", "50.0"))

        # Polling
        self.poll_interval_sec = int(os.getenv("POLL_INTERVAL_SEC", "30"))

        # Profit Exit
        self.profit_exit_enabled = os.getenv("PROFIT_EXIT_ENABLED", "false").lower() == "true"
        self.profit_exit_pct = float(os.getenv("PROFIT_EXIT_PCT", "5.0"))

        # Auto-Refollow
        self.auto_refollow_after_profit = os.getenv("AUTO_REFOLLOW_AFTER_PROFIT", "false").lower() == "true"
        self.auto_refollow_after_manual = os.getenv("AUTO_REFOLLOW_AFTER_MANUAL", "false").lower() == "true"

        # Telegram
        self.telegram_enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
        self.telegram_token = os.getenv("TELEGRAM_API_TOKEN", "")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

        # Logging
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        self.log_level = getattr(logging, log_level, logging.INFO)

        # Symbol-specific tolerances
        self._symbol_tolerances: Dict[str, float] = {}
        self._parse_symbol_tolerances()

    def _parse_symbol_tolerances(self):
        """Parse symbol-specific price tolerances from env vars."""
        # Format: PRICE_TOLERANCE_BTCUSDT=0.5
        for key, value in os.environ.items():
            if key.startswith("PRICE_TOLERANCE_") and key != "PRICE_TOLERANCE_PCT":
                symbol = key.replace("PRICE_TOLERANCE_", "")
                try:
                    self._symbol_tolerances[symbol] = float(value)
                except ValueError:
                    pass

    def get_price_tolerance(self, symbol: str) -> float:
        """Get price tolerance for a specific symbol."""
        return self._symbol_tolerances.get(symbol, self.default_price_tolerance_pct)

    def validate(self) -> tuple[bool, Optional[str]]:
        """Validate configuration. Returns (is_valid, error_message)."""
        if not self.binance_api_key:
            return False, "BINANCE_API_KEY not set"
        if not self.binance_api_secret:
            return False, "BINANCE_API_SECRET not set"

        if self.capital_mode not in ["proportional", "fixed_amount"]:
            return False, f"Invalid CAPITAL_MODE: {self.capital_mode}"

        if self.margin_mode not in ["ISOLATED", "CROSSED"]:
            return False, f"Invalid MARGIN_MODE: {self.margin_mode}"

        if self.telegram_enabled and (not self.telegram_token or not self.telegram_chat_id):
            return False, "Telegram enabled but token/chat_id not set"

        return True, None

    def __repr__(self) -> str:
        """String representation (hides secrets)."""
        return (
            f"Config(api={self.nof1_api_base}, "
            f"testnet={self.binance_testnet}, "
            f"capital_mode={self.capital_mode}, "
            f"margin_mode={self.margin_mode})"
        )


def load_env_file(env_path: Optional[Path] = None):
    """Load .env file if it exists (manual implementation to avoid dependency)."""
    if env_path is None:
        env_path = Path.cwd() / ".env"

    if not env_path.exists():
        return

    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # Remove quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]

                # Only set if not already in environment
                if key not in os.environ:
                    os.environ[key] = value


# Global config instance
config = Config()
