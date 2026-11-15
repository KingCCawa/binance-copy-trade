"""诊断：把带单员的 marginBalance 拆成「钱包余额 + 未实现盈亏」。

为什么需要它：`detail()` 只给 marginBalance，含未实现盈亏。拿它做敞口比例的
分母，他浮亏越深分母越小，同一笔开仓算出的比例越大。引擎改成用剔除浮亏后的
「钱包余额」做分母，但这个分解依赖一个前提 —— marginBalance 确实含浮亏。

本脚本只读，不动任何仓位，用来确认这个前提：
  - 他空仓时：unrealized 应为 0，wallet == marginBalance（精确锚点）
  - 他持仓浮亏时：marginBalance 随价格下跌而下降，wallet 保持不动
  - 若 wallet 跟着 marginBalance 一起动，说明 marginBalance 本身不含浮亏，
    分解无效，应把分母直接退回 marginBalance

用法：
  py -3.12 wallet_probe.py                    # 打印一次
  py -3.12 wallet_probe.py --watch 30         # 每 30 秒一次，Ctrl+C 停
  py -3.12 wallet_probe.py --json             # 机器可读
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from api_feed import LeadApi, mark_prices          # noqa: E402
from position_ledger import replay, wallet_state   # noqa: E402

DEFAULT_PID = "5075281354358777856"


def probe(pid: str) -> dict:
    api = LeadApi(pid)
    detail = api.detail()
    fills = api.fills(rows=500)
    marks = mark_prices()
    rep = replay(fills)
    margin = float(detail.get("marginBalance") or 0.0)
    ws = wallet_state(rep, margin, marks)
    return {
        "pid": pid,
        "nickname": detail.get("nickname"),
        "status": detail.get("status"),
        "margin_balance": ws.margin_balance,
        "unrealized": ws.unrealized,
        "wallet": ws.wallet,
        "anchored": ws.anchored,
        "missing": ws.missing,
        "lots": ws.lots,
        "surviving": len(rep.surviving),
        "marks_total": len(marks),
        "wall": time.time(),
    }


def render(data: dict, prev: dict | None) -> None:
    print(f"带单员 {data['nickname']} ({data['pid']}) · "
          f"status={data['status']} · 标记价 {data['marks_total']} 个")
    print(f"  marginBalance    {data['margin_balance']:.8f}")
    print(f"  未实现盈亏       {data['unrealized']:+.8f}")
    print(f"  钱包余额(推算)   {data['wallet']:.8f}"
          f"   {'空仓精确' if data['anchored'] else '持仓推算'}")
    if data["missing"]:
        print(f"  缺标记价         {', '.join(data['missing'])}（按 0 计，偏保守）")
    if data["lots"]:
        print(f"  存活批次 {data['surviving']} 笔：")
        for lot in data["lots"]:
            mark = (f"{lot['mark']:.8f}" if lot["mark"] is not None else "-")
            print(f"    {lot['symbol']:<14} {lot['side']:<5} "
                  f"qty={lot['qty']:.8f} entry={lot['entry']:.8f} "
                  f"mark={mark:<14} pnl={lot['pnl']:+.8f}")
    if prev:
        d_margin = data["margin_balance"] - prev["margin_balance"]
        d_wallet = data["wallet"] - prev["wallet"]
        print(f"  相对上轮：marginBalance {d_margin:+.8f} · "
              f"钱包余额 {d_wallet:+.8f}")
        if abs(d_margin) > 1.0 and abs(d_wallet) < 1.0:
            print("  → marginBalance 在动、钱包余额不动：分解有效，"
                  "分母用钱包余额是对的")
        elif abs(d_wallet) > 1.0 and abs(d_margin - d_wallet) < 1.0:
            print("  → 两者同向同幅：要么他刚出入金，要么 marginBalance "
                  "本身不含浮亏（需再观察）")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--portfolio-id", default=DEFAULT_PID)
    ap.add_argument("--watch", type=int, default=0,
                    help="每 N 秒重复采样，0 = 只跑一次")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    prev = None
    while True:
        try:
            data = probe(args.portfolio_id)
        except Exception as e:
            print(f"{type(e).__name__}: {e}", file=sys.stderr)
            if not args.watch:
                return 1
            time.sleep(max(5, args.watch))
            continue
        if args.json:
            print(json.dumps(data, ensure_ascii=False))
        else:
            render(data, prev)
        prev = data
        if not args.watch:
            return 0
        try:
            time.sleep(max(5, args.watch))
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
