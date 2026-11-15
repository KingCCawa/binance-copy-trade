"""跟单状态持久化：已处理事件 + 未成交挂单 + 盈亏账本。

单文件 SQLite，单连接 + 锁；CopyEngine 在后台线程里串行调用，不需要连接池。
pending 用挂钟时间存，因为 monotonic 跨进程没有意义，恢复时要反算年龄。

盈亏账本三张表（v2 新增，全部 `IF NOT EXISTS`，老库直接兼容）：
  pnl_snapshot  每轮一行：权益 / 浮动盈亏 / 敞口 / 保证金占用
  pnl_position  每轮每品种一行：数量 / 开仓价 / 标记价 / 浮盈
  pnl_trade     我方动作流水：开 / 平 / 撤，带这一笔的实现盈亏

`mode` 列是必须的：dry-run 的持仓来自 MemoryBroker（entry_price 恒 0），
和 testnet/live 的交易所数据混在一条曲线上就是假数据。查询一律按 mode 过滤。

  py -3.12 state_store.py --selftest
"""
from __future__ import annotations

import argparse
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seen (
    k TEXT PRIMARY KEY, seen_wall REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending (
    symbol TEXT PRIMARY KEY, order_id TEXT, side TEXT,
    qty REAL, lim REAL, leverage INTEGER,
    created_wall REAL NOT NULL, source_key TEXT);
CREATE TABLE IF NOT EXISTS pnl_snapshot (
    ts REAL NOT NULL, mode TEXT NOT NULL,
    equity REAL, avail REAL, unreal REAL,
    exposure REAL, margin REAL, n_pos INTEGER,
    lead_wallet REAL, lead_unreal REAL, lead_exposure REAL,
    PRIMARY KEY (ts, mode));
CREATE INDEX IF NOT EXISTS idx_pnl_snapshot ON pnl_snapshot(mode, ts);
CREATE TABLE IF NOT EXISTS pnl_position (
    ts REAL NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT, qty REAL, entry REAL, mark REAL,
    unreal REAL, notional REAL,
    PRIMARY KEY (ts, mode, symbol));
CREATE TABLE IF NOT EXISTS pnl_trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, mode TEXT NOT NULL, kind TEXT NOT NULL,
    symbol TEXT, side TEXT, qty REAL, price REAL, notional REAL,
    realized REAL, note TEXT);
CREATE INDEX IF NOT EXISTS idx_pnl_trade ON pnl_trade(mode, ts);
"""

SNAP_COLS = ("ts", "equity", "avail", "unreal", "exposure", "margin",
             "n_pos", "lead_wallet", "lead_unreal", "lead_exposure")
POS_COLS = ("symbol", "side", "qty", "entry", "mark", "unreal", "notional")
TRADE_COLS = ("ts", "kind", "symbol", "side", "qty", "price",
              "notional", "realized", "note")


class StateStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # 老库升级：补 historical 列（CREATE TABLE IF NOT EXISTS 不会改已有表）
        try:
            self.db.execute(
                "ALTER TABLE pending ADD COLUMN historical INTEGER "
                "NOT NULL DEFAULT 0")
            self.db.commit()
        except sqlite3.OperationalError:
            pass                       # 列已存在

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ---- meta ----
    def get(self, key: str, default=None):
        with self._lock:
            row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                            (key, str(value)))
            self.db.commit()

    # ---- seen ----
    def load_seen(self) -> dict[str, float]:
        with self._lock:
            rows = self.db.execute("SELECT k, seen_wall FROM seen").fetchall()
        return {k: float(v) for k, v in rows}

    def mark_seen(self, keys) -> None:
        keys = list(keys)
        if not keys:
            return
        now = time.time()
        with self._lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO seen(k,seen_wall) VALUES(?,?)",
                [(k, now) for k in keys])
            self.db.commit()

    def drop_seen(self, keys) -> None:
        keys = list(keys)
        if not keys:
            return
        with self._lock:
            self.db.executemany("DELETE FROM seen WHERE k=?", [(k,) for k in keys])
            self.db.commit()

    def prune_seen(self, older_than_wall: float) -> int:
        with self._lock:
            cur = self.db.execute("DELETE FROM seen WHERE seen_wall < ?",
                                  (older_than_wall,))
            self.db.commit()
        return cur.rowcount

    def clear_seen(self) -> None:
        """换带单员时清空去重水位；新人的事件 key 不能和旧人的混在一起。"""
        with self._lock:
            self.db.execute("DELETE FROM seen")
            self.db.commit()

    # ---- pending ----
    def load_pending(self) -> list[dict]:
        cols = ("symbol", "order_id", "side", "qty", "limit", "leverage",
                "created_wall", "source_key", "historical")
        with self._lock:
            rows = self.db.execute(
                "SELECT symbol, order_id, side, qty, lim, leverage, "
                "created_wall, source_key, historical FROM pending").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def upsert_pending(self, p) -> None:
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO pending"
                "(symbol,order_id,side,qty,lim,leverage,created_wall,"
                "source_key,historical) VALUES(?,?,?,?,?,?,?,?,?)",
                (p.symbol, str(p.order_id), p.side, float(p.qty),
                 float(p.limit), int(p.leverage), time.time(), p.source_key,
                 int(bool(getattr(p, "historical", False)))))
            self.db.commit()

    def remove_pending(self, symbol: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM pending WHERE symbol=?", (symbol,))
            self.db.commit()

    # ---- 盈亏账本 ----
    def save_pnl(self, mode: str, snap: dict, positions: list[dict]) -> None:
        """一轮一行快照 + 每个品种一行持仓。`snap["ts"]` 由调用方给。"""
        ts = float(snap["ts"])
        row = tuple([ts]
                    + [None if snap.get(c) is None else float(snap.get(c) or 0.0)
                       for c in SNAP_COLS[1:6]]
                    + [int(snap.get("n_pos") or 0)]
                    + [float(snap.get(c) or 0.0)
                       for c in SNAP_COLS[7:]])
        with self._lock:
            self.db.execute(
                f"INSERT OR REPLACE INTO pnl_snapshot"
                f"({','.join(SNAP_COLS)}, mode) VALUES"
                f"({','.join('?' * (len(SNAP_COLS) + 1))})", row + (mode,))
            if positions:
                self.db.executemany(
                    f"INSERT OR REPLACE INTO pnl_position"
                    f"(ts, mode, {','.join(POS_COLS)}) VALUES"
                    f"({','.join('?' * (len(POS_COLS) + 2))})",
                    [(ts, mode) + tuple(p.get(c) for c in POS_COLS)
                     for p in positions])
            self.db.commit()

    def save_trades(self, mode: str, trades: list[dict]) -> int:
        """动作流水。同一份 actions 会被重放，按 (ts, kind, symbol, qty) 去重。

        没有主键去重会导致每次重启都把全量 actions 再写一遍 —— state.db 里
        self.actions 是 maxlen=500 的 deque，重启后不会重放，但同一批
        snapshot 重复投递仍可能发生，这里用唯一索引式的 INSERT OR IGNORE +
        先查后插的简单办法挡一次。
        """
        if not trades:
            return 0
        written = 0
        with self._lock:
            for t in trades:
                row = (float(t["ts"]), mode) + tuple(
                    t.get(c) for c in TRADE_COLS)
                dup = self.db.execute(
                    "SELECT 1 FROM pnl_trade WHERE ts=? AND mode=? AND kind=?"
                    " AND IFNULL(symbol,'')=IFNULL(?,'') AND IFNULL(qty,0)=IFNULL(?,0)",
                    (row[0], mode, t.get("kind"), t.get("symbol"),
                     t.get("qty"))).fetchone()
                if dup:
                    continue
                self.db.execute(
                    f"INSERT INTO pnl_trade(ts, mode, {','.join(TRADE_COLS)})"
                    f" VALUES({','.join('?' * (len(TRADE_COLS) + 2))})", row)
                written += 1
            self.db.commit()
        return written

    def load_pnl_curve(self, mode: str, since_ts: float = 0.0,
                       limit: int = 2000) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT ts, equity, avail, unreal, exposure, margin, n_pos,"
                " lead_wallet, lead_unreal, lead_exposure FROM pnl_snapshot"
                " WHERE mode=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                (mode, float(since_ts), int(limit))).fetchall()
        return [dict(zip(SNAP_COLS, r)) for r in reversed(rows)]

    def load_pnl_positions(self, mode: str, ts: float) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                f"SELECT {','.join(POS_COLS)} FROM pnl_position"
                f" WHERE mode=? AND ts=?", (mode, float(ts))).fetchall()
        return [dict(zip(POS_COLS, r)) for r in rows]

    def load_pnl_trades(self, mode: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                f"SELECT {','.join(TRADE_COLS)} FROM pnl_trade"
                f" WHERE mode=? ORDER BY ts DESC, id DESC LIMIT ?",
                (mode, int(limit))).fetchall()
        return [dict(zip(TRADE_COLS, r)) for r in rows]

    def pnl_realized(self, mode: str) -> float:
        """已平仓的累计实现盈亏（按明细加总，不是钱包差分）。

        钱包差分会把出入金一起算进去；这里只认「本程序自己平的仓」。
        平仓价拿不到交易所成交均价，记的是平仓那一刻的标记价，属近似值 ——
        UI 上必须标明。
        """
        with self._lock:
            row = self.db.execute(
                "SELECT COALESCE(SUM(realized), 0) FROM pnl_trade"
                " WHERE mode=? AND kind='CLOSE'", (mode,)).fetchone()
        return float(row[0] or 0.0)

    def prune_pnl(self, keep_days: float = 90.0) -> int:
        """滚动清理，避免一年攒出几百万行。动作流水不清（那是账本）。"""
        cut = time.time() - keep_days * 86400.0
        with self._lock:
            a = self.db.execute("DELETE FROM pnl_snapshot WHERE ts<?", (cut,))
            b = self.db.execute("DELETE FROM pnl_position WHERE ts<?", (cut,))
            self.db.commit()
        return a.rowcount + b.rowcount


def selftest() -> int:
    import tempfile

    class P:
        symbol = "BTCUSDT"
        order_id = "123"
        side = "BUY"
        qty = 0.05
        limit = 67965.0
        leverage = 20
        source_key = "BTCUSDT|LONG|OPEN|ts1"

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "state.db"
        s = StateStore(db)
        s.mark_seen(["A|LONG|OPEN|ts1", "B|SHORT|CLOSE|ts2"])
        s.set("last_run_wall", time.time())
        s.upsert_pending(P())
        s.save_pnl("live", {"ts": 1000.0, "equity": 100.0, "avail": 40.0,
                            "unreal": 1.5, "exposure": 300.0, "margin": 15.0,
                            "n_pos": 1, "lead_wallet": 5000.0,
                            "lead_unreal": -2.0, "lead_exposure": 9000.0},
                    [{"symbol": "BTCUSDT", "side": "LONG", "qty": 0.01,
                      "entry": 68000.0, "mark": 68150.0, "unreal": 1.5,
                      "notional": 681.5}])
        # 同 mode 不同 ts 的第二行，用来验证曲线按 ts 升序返回
        s.save_pnl("live", {"ts": 2000.0, "equity": 101.0, "avail": 41.0,
                            "unreal": 2.0, "exposure": 300.0, "margin": 15.0,
                            "n_pos": 1, "lead_wallet": 5000.0,
                            "lead_unreal": 0.0, "lead_exposure": 9000.0}, [])
        # dry-run 的数据不能串进 live
        s.save_pnl("dry-run", {"ts": 1500.0, "equity": 999.0}, [])
        n1 = s.save_trades("live", [
            {"ts": 1.0, "kind": "CLOSE", "symbol": "BTCUSDT", "side": "LONG",
             "qty": 0.01, "price": 68150.0, "notional": 681.5,
             "realized": 1.5, "note": "带单员平仓"}])
        n2 = s.save_trades("live", [
            {"ts": 1.0, "kind": "CLOSE", "symbol": "BTCUSDT", "side": "LONG",
             "qty": 0.01, "price": 68150.0, "notional": 681.5,
             "realized": 1.5, "note": "带单员平仓"}])   # 重复投递必须被挡掉
        s.close()

        s2 = StateStore(db)
        seen = s2.load_seen()
        pend = s2.load_pending()
        last = float(s2.get("last_run_wall", 0) or 0)
        curve = s2.load_pnl_curve("live")
        pos = s2.load_pnl_positions("live", 1000.0)
        trades = s2.load_pnl_trades("live")
        realized = s2.pnl_realized("live")
        s2.close()

    ok = (len(seen) == 2 and len(pend) == 1 and last > 0
          and pend[0]["limit"] == 67965.0
          and pend[0]["created_wall"] > 0
          and [c["ts"] for c in curve] == [1000.0, 2000.0]
          and abs(curve[1]["equity"] - 101.0) < 1e-9
          and len(pos) == 1 and abs(pos[0]["unreal"] - 1.5) < 1e-9
          and n1 == 1 and n2 == 0 and len(trades) == 1
          and abs(realized - 1.5) < 1e-9)
    print(f"seen={len(seen)} pending={len(pend)} last_run_wall={last:.0f}")
    print(f"curve={[c['ts'] for c in curve]} pos={len(pos)} "
          f"trades={len(trades)} realized={realized:.2f} "
          f"dedup={n1}/{n2}")
    print("state_store selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    print("StateStore: 供 CopyEngine 使用；运行 --selftest 做回归")
