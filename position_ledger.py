"""带单员仓位重放：从聚合后的成交流水还原他当前还持有什么。

为什么需要它：公开 bapi 只给逐笔成交，没有「当前持仓」快照
（lead-data/positions 实测返回 []）。而跟单要处理三种情形：
  - 多次开仓（加仓）：6.305 BTC + 8.532 BTC，不能只跟最后一笔；
  - 分次平仓：399.789 ZEC 分 199.894 / 199.895 两次平，比例要按平前持仓算；
  - 换交易员时他存量但**未平**的仓位：可以在更优价跟进，已平掉的不能跟。

做法是每轮对窗口内全部事件做一次 FIFO 核销，纯函数、无状态、可独立测试：
  - surviving  : 仍存活的开仓批次（带原始开仓价/剩余数量）
  - close_before: 每笔 CLOSE 发生前该方向的持仓数量，用来算平仓比例
  - totals     : 每个 (品种,方向) 存活批次的累计数量、名义、加权均价

窗口只有最近 500 条逐笔成交，极老的仓位可能已滑出窗口；此时 CLOSE 数量会
大于窗口内 OPEN 数量，close_before 偏小导致比例偏大，最终退化为整仓平 ——
这是刻意的安全兜底。

  py -3.12 position_ledger.py --selftest
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from feed import Fill

EPS = 1e-9


@dataclass
class Lot:
    """一笔开仓批次；left 是被 CLOSE 核销后剩下的数量。"""
    key: str
    ts: str
    symbol: str
    side: str
    price: float
    notional: float
    qty: float
    left: float


@dataclass
class ReplayResult:
    lots: dict = field(default_factory=dict)
    books: dict = field(default_factory=dict)
    close_before: dict = field(default_factory=dict)
    surviving: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    # 每笔 OPEN 发生「当时」的累计持仓，用于按他的加仓进度等比开仓。
    # key = Fill.key，value = {"qty", "notional", "avg"}
    open_context: dict = field(default_factory=dict)

    def held(self, symbol: str, side: str) -> float:
        return float(self.totals.get((symbol, side), {}).get("qty", 0.0))

    def total(self, symbol: str, side: str) -> dict:
        return self.totals.get((symbol, side),
                               {"qty": 0.0, "notional": 0.0,
                                "avg_price": 0.0})


def replay(fills: list[Fill]) -> ReplayResult:
    """按时间顺序重放，OPEN 建批次、CLOSE 按 FIFO 核销。"""
    ordered = sorted(fills, key=lambda f: (f.ts, f.action != "OPEN"))
    lots: dict[tuple[str, str], list[Lot]] = {}
    books: dict[tuple[str, str], float] = {}
    close_before: dict[str, float] = {}
    open_context: dict[str, dict] = {}
    # 按时间顺序维护「当时的累计持仓」，遇到 CLOSE 就扣；这样跨轮加仓也能
    # 拿到该笔 OPEN 发生时刻的真实累计量，而不是只算本轮新事件。
    running: dict[tuple[str, str], dict] = {}

    for f in ordered:
        key = (f.symbol, f.side)
        if f.action == "OPEN":
            lots.setdefault(key, []).append(Lot(
                key=f.key, ts=f.ts, symbol=f.symbol, side=f.side,
                price=float(f.price), notional=float(f.notional),
                qty=float(f.qty), left=float(f.qty)))
            books[key] = books.get(key, 0.0) + float(f.qty)
            t = running.setdefault(key, {"qty": 0.0, "notional": 0.0})
            t["qty"] += float(f.qty)
            t["notional"] += float(f.notional)
            open_context[f.key] = {
                "qty": t["qty"], "notional": t["notional"],
                "avg": t["notional"] / t["qty"] if t["qty"] > EPS else f.price}
            continue

        before = books.get(key, 0.0)
        close_before[f.key] = before
        need = float(f.qty)
        # 名义要按被核销批次的「开仓名义」扣，不能用 CLOSE 的平仓名义：
        # 开仓价和平仓价不同，用平仓名义扣会把累计基准价拉偏。
        closed_notional = 0.0
        for lot in lots.get(key, []):
            if need <= EPS:
                break
            take = min(need, lot.left)
            frac = take / lot.qty if lot.qty > EPS else 0.0
            closed_notional += lot.notional * frac
            lot.left -= take
            need -= take
        books[key] = max(0.0, before - float(f.qty))
        t = running.get(key)
        if t:
            t["qty"] = max(0.0, t["qty"] - float(f.qty))
            t["notional"] = max(0.0, t["notional"] - closed_notional)

    surviving = [lot for arr in lots.values() for lot in arr
                 if lot.left > EPS]
    totals: dict[tuple[str, str], dict] = {}
    for lot in surviving:
        t = totals.setdefault((lot.symbol, lot.side),
                              {"qty": 0.0, "notional": 0.0})
        frac = lot.left / lot.qty if lot.qty > EPS else 0.0
        t["qty"] += lot.left
        t["notional"] += lot.notional * frac
    for t in totals.values():
        t["avg_price"] = t["notional"] / t["qty"] if t["qty"] > EPS else 0.0

    return ReplayResult(lots=lots, books=books,
                        close_before=close_before,
                        surviving=surviving, totals=totals,
                        open_context=open_context)


@dataclass
class WalletState:
    """带单员「钱包余额」的推算结果。

    为什么要它：`detail()` 只给 `marginBalance`，它含未实现盈亏。拿它做
    敞口比例的分母，他浮亏越深分母越小，同一笔开仓算出的比例越大 ——
    跟单人会在最不该加仓的位置被动加杠杆（浮亏 60% 时放大 2.5 倍）。

    把未实现盈亏剥出去，还原成只随「已实现盈亏 + 出入金」变化的钱包余额：
      钱包余额 = marginBalance - 未实现盈亏
    空仓时未实现为 0，此时 marginBalance 就是精确的钱包余额，是每次的校准点。
    """
    margin_balance: float
    unrealized: float
    wallet: float
    anchored: bool                  # True = 空仓，wallet 直接取自 marginBalance
    missing: list = field(default_factory=list)   # 拿不到标记价的品种
    lots: list = field(default_factory=list)      # 逐批明细，供诊断打印


def wallet_state(rep: ReplayResult, margin_balance: float,
                 marks: dict | None = None) -> WalletState:
    """marginBalance 分解。空仓精确，有仓按标记价逐批估算。

    拿不到标记价的品种按 0 计未实现盈亏。方向是保守的：
    unreal 少算 -> wallet 偏高 -> 分母偏大 -> 我方仓位偏小。
    """
    balance = float(margin_balance or 0.0)
    if not rep.surviving:
        return WalletState(margin_balance=balance, unrealized=0.0,
                           wallet=balance, anchored=True)

    marks = marks or {}
    unreal = 0.0
    missing: list[str] = []
    rows: list[dict] = []
    for lot in rep.surviving:
        mark = marks.get(lot.symbol)
        if not mark or mark <= 0:
            if lot.symbol not in missing:
                missing.append(lot.symbol)
            rows.append({"symbol": lot.symbol, "side": lot.side,
                         "qty": lot.left, "entry": lot.price,
                         "mark": None, "pnl": 0.0})
            continue
        sign = 1.0 if lot.side == "LONG" else -1.0
        pnl = (float(mark) - lot.price) * lot.left * sign
        unreal += pnl
        rows.append({"symbol": lot.symbol, "side": lot.side,
                     "qty": lot.left, "entry": lot.price,
                     "mark": float(mark), "pnl": pnl})

    return WalletState(margin_balance=balance, unrealized=unreal,
                       wallet=balance - unreal, anchored=False,
                       missing=missing, lots=rows)


def _btc_real() -> list[Fill]:
    """币安「最新操作记录」截图里的真实数值。"""
    return [
        Fill(ts="2026-11-13 14:01:11", action="OPEN", side="LONG",
             symbol="BTCUSDT", price=68025.48, qty=6.305,
             notional=428900.66),
        Fill(ts="2026-11-13 14:04:34", action="OPEN", side="LONG",
             symbol="BTCUSDT", price=68170.10, qty=8.532,
             notional=581627.30),
        Fill(ts="2026-11-13 21:38:50", action="CLOSE", side="LONG",
             symbol="BTCUSDT", price=69984.80, qty=14.837,
             notional=1038364.52),
    ]


def selftest() -> int:
    results: list[tuple[str, bool, str]] = []

    r = replay([f for f in _btc_real() if f.action == "OPEN"])
    t = r.total("BTCUSDT", "LONG")
    c1 = r.open_context["BTCUSDT|LONG|OPEN|2026-11-13 14:01:11"]
    c2 = r.open_context["BTCUSDT|LONG|OPEN|2026-11-13 14:04:34"]
    ok = (abs(t["qty"] - 14.837) < 1e-6 and len(r.surviving) == 2
          and 68025.48 < t["avg_price"] < 68170.10
          and abs(c1["qty"] - 6.305) < 1e-6
          and abs(c2["qty"] - 14.837) < 1e-6
          and 68025.48 < c2["avg"] < 68170.10)
    results.append(("两次开仓累计：数量、加权均价、每笔当时累计量", ok,
                    f"qty={t['qty']:.3f} avg={t['avg_price']:.2f} "
                    f"首笔累计={c1['qty']:.3f} 加仓后累计={c2['qty']:.3f} "
                    f"加仓基准={c2['avg']:.2f}"))

    r2 = replay(_btc_real())
    ok = (r2.held("BTCUSDT", "LONG") == 0.0 and not r2.surviving
          and abs(r2.close_before.get(
              "BTCUSDT|LONG|CLOSE|2026-11-13 21:38:50", 0) - 14.837) < 1e-6)
    results.append(("一次全平：批次全部核销，平前持仓=14.837", ok,
                    f"held={r2.held('BTCUSDT', 'LONG')} "
                    f"before={r2.close_before.get('BTCUSDT|LONG|CLOSE|2026-11-13 21:38:50')}"))

    zec = [
        Fill(ts="2026-11-13 14:00:00", action="OPEN", side="SHORT",
             symbol="ZECUSDT", price=900.0, qty=399.789,
             notional=359810.1),
        Fill(ts="2026-11-13 14:03:58", action="CLOSE", side="SHORT",
             symbol="ZECUSDT", price=914.94, qty=199.894,
             notional=182891.18),
        Fill(ts="2026-11-13 14:04:03", action="CLOSE", side="SHORT",
             symbol="ZECUSDT", price=920.68, qty=199.895,
             notional=184039.98),
    ]
    r3 = replay(zec)
    c1 = r3.close_before["ZECUSDT|SHORT|CLOSE|2026-11-13 14:03:58"]
    c2 = r3.close_before["ZECUSDT|SHORT|CLOSE|2026-11-13 14:04:03"]
    ok = (abs(c1 - 399.789) < 1e-6 and abs(c2 - 199.895) < 1e-6
          and r3.held("ZECUSDT", "SHORT") == 0.0)
    results.append(("分次平仓：每笔 CLOSE 的平前持仓正确", ok,
                    f"before1={c1:.3f} before2={c2:.3f} "
                    f"held={r3.held('ZECUSDT', 'SHORT')}"))

    partial = zec[:2]
    r4 = replay(partial)
    ok = (abs(r4.held("ZECUSDT", "SHORT") - 199.895) < 1e-6
          and len(r4.surviving) == 1)
    results.append(("只平一半：剩余批次仍存活", ok,
                    f"held={r4.held('ZECUSDT', 'SHORT'):.3f} "
                    f"lots={len(r4.surviving)}"))

    # 钱包余额分解：空仓精确、有仓剥离浮亏、缺标记价按 0 计
    ws_empty = wallet_state(replay([]), 332859.21576981, {})
    ok = (ws_empty.anchored and abs(ws_empty.wallet - 332859.21576981) < 1e-6
          and ws_empty.unrealized == 0.0)
    results.append(("空仓：钱包余额 = marginBalance，精确锚点", ok,
                    f"wallet={ws_empty.wallet:.8f} anchored={ws_empty.anchored}"))

    ws_win = wallet_state(replay(_btc_real()[:2]), 332859.21576981,
                          {"BTCUSDT": 70000.0})
    expect = ((70000.0 - 68025.48) * 6.305 + (70000.0 - 68170.10) * 8.532)
    ok = (not ws_win.anchored
          and abs(ws_win.unrealized - expect) < 1e-4
          and abs(ws_win.wallet - (332859.21576981 - expect)) < 1e-4
          and abs(ws_win.wallet - 332859.21576981) > 1.0)
    results.append(("多仓浮盈：未实现剥离，钱包余额低于 marginBalance", ok,
                    f"unreal={ws_win.unrealized:.4f} "
                    f"wallet={ws_win.wallet:.4f} "
                    f"margin={ws_win.margin_balance:.4f}"))

    ws_miss = wallet_state(replay(zec[:1]), 332859.21576981, {})
    ok = (abs(ws_miss.unrealized) < 1e-9
          and abs(ws_miss.wallet - 332859.21576981) < 1e-6
          and "ZECUSDT" in ws_miss.missing)
    results.append(("缺标记价：按 0 计浮亏并记入 missing（方向保守）", ok,
                    f"unreal={ws_miss.unrealized} missing={ws_miss.missing}"))

    ws_short = wallet_state(replay(zec[:1]), 332859.21576981,
                            {"ZECUSDT": 950.0})
    ok = (abs(ws_short.unrealized - (900.0 - 950.0) * 399.789) < 1e-3
          and ws_short.wallet > 332859.21576981)
    results.append(("空仓浮亏：未实现为负，钱包余额高于 marginBalance", ok,
                    f"unreal={ws_short.unrealized:.4f} "
                    f"wallet={ws_short.wallet:.4f}"))

    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"position_ledger selftest: {passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    raise SystemExit(selftest() if args.selftest else 0)
