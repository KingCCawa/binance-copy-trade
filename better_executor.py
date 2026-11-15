"""在 chatdsn 的 TradingExecutor 上覆盖入场逻辑：市价单 -> 入场限价单。

入场价有两种模式，由 better_price_enabled 切换，价格计算统一走
feed.entry_limit_price()：
  开：更优价（多单挂低 / 空单挂高），只在优于信号价的位置成交；
  关：容差价（多单可挂更高 / 空单可挂更低），仍是限价单，成交价被封顶，
      用来接住带单员体量大、进场滑点后价格已经走远的单子。
平仓/撤单不在这层，归 copy_engine.CopyEngine 管，两种模式完全一致。
"""
from __future__ import annotations

import logging
import sys
import time
from math import floor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from chatdsn.trading import TradingExecutor
from feed import entry_limit_price

logger = logging.getLogger(__name__)


class BetterPriceExecutor(TradingExecutor):
    def __init__(self, binance, risk_manager, capital_manager, dry_run: bool = False,
                 improve_pct: float = 0.003, slippage_tol_pct: float = 0.003,
                 better_price_enabled: bool = True, fill_timeout_s: int = 300,
                 poll_sec: int = 5, tp_roe_pct: float | None = None,
                 sl_roe_pct: float | None = None):
        super().__init__(binance, risk_manager, capital_manager, dry_run)
        self.improve_pct = improve_pct
        self.slippage_tol_pct = slippage_tol_pct
        self.better_price_enabled = better_price_enabled
        self.fill_timeout_s = fill_timeout_s
        self.poll_sec = poll_sec
        # 公开流水没有预设止盈止损价。None = 不挂，出场靠平仓记录跟随；
        # 填数值时按保证金收益率(ROE%)换算价格幅度：price_move = roe/100/leverage
        self.tp_roe_pct = tp_roe_pct
        self.sl_roe_pct = sl_roe_pct

    def price_step_of(self, symbol: str) -> float:
        # 必须用 PRICE_FILTER.tickSize，不能用 pricePrecision：NVDAUSDT /
        # SOXLUSDT 这类代币化股票 pricePrecision=5、tickSize=0.01，按 1e-5
        # 取整挂出去会被交易所 -4014 拒掉（实测）。查不到 tick 才回落。
        try:
            tick = float(self.binance.get_symbol_tick(symbol) or 0.0)
        except (AttributeError, TypeError, ValueError):
            tick = 0.0
        if tick > 0:
            return tick
        precision, _ = self.binance.get_symbol_precision(symbol)
        return 10.0 ** (-precision)

    def entry_limit(self, entry_price: float, side: str, tick: float) -> float:
        return entry_limit_price(entry_price, side, tick,
                                 self.improve_pct, self.slippage_tol_pct,
                                 self.better_price_enabled)

    def improved_limit(self, entry_price: float, side: str, tick: float) -> float:
        """兼容旧调用名；语义由 better_price_enabled 决定。"""
        return self.entry_limit(entry_price, side, tick)

    def price_mode_text(self) -> str:
        if self.better_price_enabled:
            return f"更优 {self.improve_pct * 100:.2f}%"
        return f"容差 {self.slippage_tol_pct * 100:.2f}%"

    def open_position(self, position, allocated_margin: float,
                      telegram_service=None) -> bool:
        symbol = position.coin
        if allocated_margin <= 0:
            logger.error("No margin allocated for %s", symbol)
            return False

        balance = self.binance.get_balance() or {}
        equity = balance.get("available_balance", 0.0)
        size_check = self.risk_manager.assess_position_size(allocated_margin, equity)
        lev_check = self.risk_manager.assess_leverage(position.leverage)
        # 刻意不调 assess_all / assess_price：限价单自己会等到目标价，
        # 「当前价必须已更优」会把绝大多数信号挡在门外。
        if not (size_check.is_favorable and lev_check.is_favorable):
            logger.error("Risk check failed for %s: %s %s", symbol,
                         size_check.warnings, lev_check.warnings)
            return False

        tick = self.price_step_of(symbol)
        limit = self.entry_limit(position.entry_price, position.side, tick)
        quantity = self.capital_manager.calculate_quantity(position, allocated_margin, limit)
        # 现成 format_quantity 用 round()，可能向上取整把实际名义抬高；
        # 敞口复刻要严格不超过目标，先按 stepSize 向下取整再格式化。
        _, qty_precision = self.binance.get_symbol_precision(symbol)
        qty_step = 10.0 ** (-qty_precision)
        quantity = self.binance.format_quantity(symbol, floor(quantity / qty_step) * qty_step)
        if quantity <= 0:
            logger.error("Invalid quantity for %s", symbol)
            return False

        side = "BUY" if position.side == "LONG" else "SELL"
        logger.info("ENTRY %s %s %s qty=%s limit=%s (signal=%s, mode=%s)",
                    symbol, position.side, side, quantity, limit,
                    position.entry_price, self.price_mode_text())

        if self.dry_run:
            return True

        self.binance.set_leverage(symbol, position.leverage)
        # 逐仓：爆仓损失以该仓位保证金为限，不波及账户其他资金。
        self.binance.set_margin_mode(symbol, "ISOLATED")
        order = self.binance.place_limit_order(symbol, side, quantity, limit)
        if not order:
            return False

        filled_qty = self._wait_for_fill(symbol, order["order_id"], quantity)
        if filled_qty <= 0:
            self.binance.cancel_order(symbol, order["order_id"])
            logger.warning("Entry limit expired, cancelled: %s", symbol)
            return False

        self._attach_tp_sl(symbol, position.side, filled_qty, limit, position.leverage)
        if telegram_service:
            telegram_service.send_message(
                f"Opened {position.side} {symbol} qty={filled_qty} @ {limit} "
                f"lev={position.leverage}x"
            )
        return True

    def _wait_for_fill(self, symbol: str, order_id: int, quantity: float) -> float:
        deadline = time.monotonic() + self.fill_timeout_s
        while time.monotonic() < deadline:
            open_orders = self.binance.get_open_orders(symbol)
            if not any(o["order_id"] == order_id for o in open_orders):
                for p in self.binance.get_positions():
                    if p["symbol"] == symbol:
                        return p["quantity"]
                return 0.0
            time.sleep(self.poll_sec)
        return 0.0

    def _attach_tp_sl(self, symbol: str, side: str, qty: float,
                      entry: float, leverage: int) -> int:
        if self.tp_roe_pct is None and self.sl_roe_pct is None:
            return 0
        close_side = "SELL" if side == "LONG" else "BUY"
        placed = 0

        if self.tp_roe_pct is not None:
            move = (self.tp_roe_pct / 100.0) / leverage
            tp = entry * (1 + move) if side == "LONG" else entry * (1 - move)
            tp = self.binance.format_price(symbol, tp)
            if self.binance.place_limit_order(symbol, close_side, qty, tp, reduce_only=True):
                placed += 1
                logger.info("TP %s @ %s (ROE %.1f%%)", symbol, tp, self.tp_roe_pct)

        if self.sl_roe_pct is not None:
            move = (self.sl_roe_pct / 100.0) / leverage
            sl = entry * (1 - move) if side == "LONG" else entry * (1 + move)
            sl = self.binance.format_price(symbol, sl)
            if self.binance.place_stop_market_order(symbol, close_side, qty, sl,
                                                    reduce_only=True):
                placed += 1
                logger.info("SL %s @ %s (ROE %.1f%%)", symbol, sl, self.sl_roe_pct)

        return placed
