"""CLI：用网页截图样本跑一遍事件流跟单引擎（不联网、不下单）。

  py -3.12 main.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from better_executor import BetterPriceExecutor
from copy_engine import CopyEngine, MemoryBroker
from feed import WEB_SAMPLE, Rule, entry_limit_price, parse


def main() -> int:
    fills = parse(WEB_SAMPLE)
    broker = MemoryBroker(unfilled=True)      # 入场限价单不成交，模拟行情直接走掉
    executor = BetterPriceExecutor(
        broker, None, None, dry_run=False, improve_pct=0.003,
        slippage_tol_pct=0.003, better_price_enabled=True,
        fill_timeout_s=300, poll_sec=1, tp_roe_pct=None, sl_roe_pct=None)
    rule = Rule(lead_balance=305647.87, my_equity=10000.0,
                leverage=20, improve_pct=0.003,
                slippage_tol_pct=0.003, better_price_enabled=True)
    engine = CopyEngine(executor, rule)

    print("=== 入场价模式（信号 68000 / tick 0.00001 / 幅度 0.3%）===")
    print("  更优价 ON :", entry_limit_price(68000.0, "LONG", 0.00001,
                                            0.003, 0.003, True))
    print("  容差价 OFF:", entry_limit_price(68000.0, "LONG", 0.00001,
                                            0.003, 0.003, False))

    result = engine.process(fills)
    print("=== 事件流跟进 ===")
    print(" ", result)
    print("=== 动作 ===")
    for action in engine.actions:
        print(" ", action)
    print("=== 下单 / 撤单调用 ===")
    for call in broker.calls:
        if call[0] in ("LIMIT", "CANCEL_ALL", "CLOSE", "set_leverage"):
            print(" ", call)
    print("=== 未成交挂单 ===")
    print(" ", engine.state()["pending"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
