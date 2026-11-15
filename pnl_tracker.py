"""跟单盈亏：把「交易所持仓 + 动作流水」翻译成可入库的盈亏快照。

只有纯函数，不碰 Qt、不碰网络 —— GUI 在后台线程里调用它，自检直接调它。

三个口径，必须分清楚，否则曲线看着漂亮但是假的：

1. **钱包余额** `equity`       = `totalWalletBalance`，已实现盈亏已经进去了，
                                 浮动盈亏还没进。这是本程序自己的钱。
2. **浮动盈亏** `unreal`      = 持仓未实现盈亏之和。优先取交易所的
                                 `unRealizedProfit`（权威，含资金费），
                                 拿不到才用 `(标记价 - 开仓价) × 数量` 估。
3. **净值** = `equity + unreal` —— 曲线画的是这条，因为它才是「此刻平掉
   全部仓位能拿回多少钱」。

已实现盈亏按「本程序自己平的仓」逐笔累加（`pnl_trade.realized`），不走
钱包差分：差分会把用户手动划转的钱算成盈利。

  py -3.12 pnl_tracker.py --selftest
"""
from __future__ import annotations

import argparse
from typing import Iterable

# 动作流水里认得的类型；其余（RECONCILE_ADOPT / FUND_CHANGE）不进盈亏账本
TRADE_KINDS = {"ENTRY": "OPEN", "ENTRY_CHASE": "OPEN",
               "CLOSE_FOLLOW": "CLOSE",
               "MANUAL_CLOSE": "CLOSE", "ENTRY_CANCELLED": "CANCEL",
               "ENTRY_TIMEOUT": "CANCEL"}


def _sign(side: str) -> float:
    return 1.0 if str(side).upper() in ("LONG", "BUY") else -1.0


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def position_rows(positions: Iterable[dict], marks: dict | None,
                  leverage: int = 20) -> list[dict]:
    """持仓 → 每个品种一行的浮盈明细。

    `qty` 一律取绝对值：币安 `positionRisk` 的空头数量是负数，混用会让
    敞口正负抵消。方向单独用 `side` 表达。
    """
    marks = marks or {}
    rows: list[dict] = []
    for p in positions or []:
        symbol = str(p.get("symbol") or "")
        if not symbol:
            continue
        qty = abs(_f(p.get("quantity")))
        if qty <= 0:
            continue
        side = str(p.get("side") or "LONG").upper()
        entry = abs(_f(p.get("entry_price")))
        mark = abs(_f(marks.get(symbol))) or entry
        # 交易所给的未实现盈亏优先：它含资金费，且和账户页数字对齐。
        raw = p.get("unrealized_pnl")
        unreal = (_f(raw) if raw is not None
                  else (mark - entry) * qty * _sign(side))
        lev = int(_f(p.get("leverage"), leverage) or leverage) or 20
        notional = qty * mark
        rows.append({"symbol": symbol, "side": side, "qty": qty,
                     "entry": entry, "mark": mark, "unreal": unreal,
                     "notional": notional,
                     "_margin": notional / max(1, lev)})
    rows.sort(key=lambda r: -abs(r["notional"]))
    return rows


def snapshot(positions: Iterable[dict], marks: dict | None, equity: float,
             avail: float = 0.0, leverage: int = 20,
             lead_wallet: float = 0.0, lead_unreal: float = 0.0,
             lead_exposure: float = 0.0, ts: float = 0.0) -> tuple[dict, list]:
    """一轮快照。返回 (snapshot 行, 持仓明细行)。"""
    rows = position_rows(positions, marks, leverage)
    snap = {
        "ts": float(ts),
        "equity": _f(equity),
        "avail": _f(avail),
        "unreal": sum(r["unreal"] for r in rows),
        "exposure": sum(r["notional"] for r in rows),
        "margin": sum(r["_margin"] for r in rows),
        "n_pos": len(rows),
        "lead_wallet": _f(lead_wallet),
        "lead_unreal": _f(lead_unreal),
        "lead_exposure": _f(lead_exposure),
    }
    # _margin 只用于汇总，不落库
    clean = [{k: v for k, v in r.items() if not k.startswith("_")}
             for r in rows]
    return snap, clean


def trades_from_actions(actions: Iterable[dict], ts: float,
                        prev_positions: Iterable[dict] | None = None,
                        marks: dict | None = None) -> list[dict]:
    """引擎动作 → 流水行。

    平仓的实现盈亏是**近似值**：交易所的市价平仓回执不带成交均价
    （`place_market_order` 只返回 order_id/symbol/side/quantity/status），
    所以用「平仓那一刻的标记价 - 平仓前该品种的开仓均价」估。误差来自
    滑点，量级远小于记账口径错误，UI 上标明即可。
    """
    marks = marks or {}
    # 按 (品种, 方向) 建索引：同品种反手后，旧方向的成本不能拿来算新方向。
    entry_of = {}
    for p in prev_positions or []:
        entry_of[(str(p.get("symbol") or ""),
                  str(p.get("side") or "").upper())] = abs(
                      _f(p.get("entry_price")))
    out: list[dict] = []
    for a in actions or []:
        kind = TRADE_KINDS.get(str(a.get("type") or ""))
        symbol = str(a.get("symbol") or "")
        if not kind or not symbol:
            continue
        side = str(a.get("side") or "").upper()
        if kind == "OPEN":
            qty = abs(_f(a.get("qty")))
            price = _f(a.get("limit"))
            notional = qty * price
            note = str(a.get("price_mode") or "")
            if a.get("historical"):
                note = (note + " · 历史仓位").strip(" ·")
            realized = None
        elif kind == "CLOSE":
            qty = abs(_f(a.get("close_qty", a.get("qty"))))
            # 方向缺失就不算盈亏：默认成空头会让「平多盈利」记成亏损。
            entry = entry_of.get((symbol, side))
            mark = abs(_f(marks.get(symbol))) or (entry or 0.0)
            price = mark
            notional = qty * price
            realized = ((mark - entry) * qty * _sign(side)
                        if entry and side and qty > 0 else None)
            note = ("带单员平仓" if a.get("our_position", True)
                    else "仅撤挂单")
            if a.get("cancel_only"):
                note = "仅撤挂单"
        else:                       # CANCEL：入场限价单超时撤单
            qty = abs(_f(a.get("qty")))
            price = _f(a.get("limit"))
            notional = qty * price
            realized = None
            note = str(a.get("reason") or "超时撤单")
        out.append({"ts": float(ts), "kind": kind, "symbol": symbol,
                    "side": side, "qty": qty, "price": price,
                    "notional": notional, "realized": realized,
                    "note": note})
    return out


def curve_stats(points: list[dict]) -> dict:
    """净值曲线的收尾统计。points 需按 ts 升序。

    净值 = 钱包余额 + 浮动盈亏。用净值画曲线，否则一次平仓会让曲线
    凭空下蹲（浮盈消失、钱进了钱包），看着像亏了。
    """
    if not points:
        return {"n": 0, "first": 0.0, "last": 0.0, "peak": 0.0,
                "mdd": 0.0, "mdd_pct": 0.0, "pnl": 0.0, "pnl_pct": 0.0}
    equity = [(_f(p.get("equity")) + _f(p.get("unreal"))) for p in points]
    first, last = equity[0], equity[-1]
    peak = equity[0]
    mdd = mdd_pct = 0.0
    for v in equity:
        peak = max(peak, v)
        drop = peak - v
        # 比例要按「回撤发生当时的峰值」算，不能用全程最高点：
        # 峰值出现在回撤之后时，分母会被后面的新高抬高，回撤被低估。
        pct = (drop / peak * 100.0) if peak > 1e-9 else 0.0
        if drop > mdd:
            mdd, mdd_pct = drop, pct
    base = first if abs(first) > 1e-9 else 0.0
    return {"n": len(points), "first": first, "last": last,
            "peak": peak, "mdd": mdd, "mdd_pct": mdd_pct,
            "pnl": last - first,
            "pnl_pct": ((last - first) / base * 100.0) if base else 0.0}


def selftest() -> int:
    results: list[tuple[str, bool, str]] = []

    pos = [
        {"symbol": "BTCUSDT", "side": "LONG", "quantity": 0.02,
         "entry_price": 68000.0, "unrealized_pnl": 3.0, "leverage": 20},
        {"symbol": "ZECUSDT", "side": "SHORT", "quantity": -5.0,
         "entry_price": 900.0, "unrealized_pnl": None, "leverage": 10},
    ]
    snap, rows = snapshot(pos, {"ZECUSDT": 880.0}, equity=100.0, avail=60.0)
    ok = (snap["n_pos"] == 2
          and abs(snap["unreal"] - (3.0 + 100.0)) < 1e-9
          and abs(rows[0]["qty"] - 5.0) < 1e-9          # 空头取绝对值
          and abs(snap["exposure"] - (0.02 * 68000.0 + 5.0 * 880.0)) < 1e-6
          and abs(snap["margin"] - (1360.0 / 20 + 4400.0 / 10)) < 1e-6)
    results.append(("交易所浮盈优先，缺失才用标记价估算；空头数量取绝对值",
                    ok, f"unreal={snap['unreal']:.2f} "
                        f"exposure={snap['exposure']:.2f} "
                        f"margin={snap['margin']:.2f}"))

    prev = [{"symbol": "BTCUSDT", "side": "LONG",
             "entry_price": 68000.0, "quantity": 0.02}]
    tr = trades_from_actions(
        [{"type": "ENTRY", "symbol": "BTCUSDT", "side": "LONG",
          "qty": 0.02, "limit": 67796.0, "price_mode": "更优 0.30%"},
         {"type": "CLOSE_FOLLOW", "symbol": "BTCUSDT", "side": "LONG",
          "close_qty": 0.02, "close_ratio": 1.0, "our_position": True},
         {"type": "FUND_CHANGE", "symbol": "", "delta": 100.0}],
        ts=1000.0, prev_positions=prev, marks={"BTCUSDT": 69000.0})
    ok = (len(tr) == 2                       # FUND_CHANGE 不进账本
          and tr[0]["kind"] == "OPEN"
          and abs(tr[0]["notional"] - 0.02 * 67796.0) < 1e-6
          and tr[1]["kind"] == "CLOSE"
          and abs(tr[1]["realized"] - (69000.0 - 68000.0) * 0.02) < 1e-6)
    results.append(("动作流水：开仓记限价、平仓按标记价估实现盈亏",
                    ok, f"kinds={[t['kind'] for t in tr]} "
                        f"realized={tr[-1]['realized'] if len(tr) > 1 else None}"))

    tr2 = trades_from_actions(
        [{"type": "CLOSE_FOLLOW", "symbol": "ZECUSDT", "side": "SHORT",
          "close_qty": 5.0, "cancel_only": True, "our_position": False}],
        ts=1.0)
    ok = len(tr2) == 1 and tr2[0]["realized"] is None and "撤" in tr2[0]["note"]
    results.append(("只撤挂单的 CLOSE 不产生实现盈亏", ok,
                    f"realized={tr2[0]['realized']} note={tr2[0]['note']}"))

    pts = [{"ts": 1, "equity": 100.0, "unreal": 0.0},
           {"ts": 2, "equity": 100.0, "unreal": 10.0},
           {"ts": 3, "equity": 100.0, "unreal": -5.0},
           {"ts": 4, "equity": 100.0, "unreal": 20.0}]
    st = curve_stats(pts)
    ok = (abs(st["last"] - 120.0) < 1e-9 and abs(st["peak"] - 120.0) < 1e-9
          and abs(st["mdd"] - 15.0) < 1e-9 and abs(st["pnl"] - 20.0) < 1e-9
          and abs(st["mdd_pct"] - 15.0 / 110.0 * 100) < 1e-6)
    results.append(("净值 = 钱包 + 浮盈；最大回撤按净值峰值算", ok,
                    f"last={st['last']:.2f} mdd={st['mdd']:.2f} "
                    f"mdd%={st['mdd_pct']:.2f} pnl%={st['pnl_pct']:.2f}"))

    ok = curve_stats([])["n"] == 0
    results.append(("空曲线不抛异常", ok, "n=0"))

    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"pnl_tracker selftest: {passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="跟单盈亏快照（纯函数）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    raise SystemExit(selftest() if args.selftest else 0)
