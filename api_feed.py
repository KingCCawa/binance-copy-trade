"""直连币安带单本公开接口抓逐笔成交，不登录、不渲染浏览器。

2026-11-13 实测：用正确的 portfolioId 调 /bapi/futures/v1/friendly/... 端点，
未登录即可拿到 code=000000 的真实数据。

网络：www.binance.com 的 bapi 端点强制直连 8s 超时、走本地代理 0.18s 通，
所以固定用 net.BinanceHttp 的 proxy_first（代理优先 / 直连兜底）。
.env 里 BINANCE_NET_PROXY 可显式指定代理端口，不需要开 TUN。

端点（从币安跟单页前端 JS 里提取，见 probe_apis.py）：
  GET  friendly/future/copy-trade/lead-portfolio/detail?portfolioId=X
  POST friendly/future/copy-trade/lead-portfolio/trade-history      {"portfolioId","page","rows"}
  POST friendly/future/copy-trade/lead-portfolio/position-history   {"portfolioId","page","rows"}
  GET  public/future/copy-trade/lead-portfolio/performance?...
  GET  friendly/future/copy-trade/lead-data/positions?portfolioId=X

用法：
  py -3.12 api_feed.py --portfolio-id 1234567890123456789 --rows 20
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from feed import EXAMPLE_PORTFOLIO_ID, Fill
from net import BinanceHttp

BASE = "https://www.binance.com/bapi/futures/v1/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 短间隔重复请求会返回 11012005「当前系统正忙」，属于可重试的瞬时错误。
RETRY_BUSY_CODES = {"11012005"}
NET_ERRORS = (requests.exceptions.RequestException, ValueError)


def _ts(ms: Any) -> str:
    return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")


# 标记价：fapi 域，一次返回全部合约（实测 949 个），用于把带单员的
# marginBalance 拆成「钱包余额 + 未实现盈亏」。走 auto 通道，和取数链路
# 的 proxy_first 不同 —— bapi 与 fapi 两个域的可达性不一样。
_MARK_CACHE: list = [0.0, {}]
_MARK_HTTP: BinanceHttp | None = None


def mark_prices(ttl: float = 5.0) -> dict[str, float]:
    """全部合约标记价 {symbol: price}；ttl 内复用，失败返回上次结果。"""
    global _MARK_HTTP
    now = time.monotonic()
    if _MARK_CACHE[1] and now - _MARK_CACHE[0] < ttl:
        return _MARK_CACHE[1]
    if _MARK_HTTP is None:
        _MARK_HTTP = BinanceHttp(mode="auto")
    r = _MARK_HTTP.request(
        "GET", "https://fapi.binance.com/fapi/v1/premiumIndex",
        timeout=(3.05, 10), attempts=2)
    r.raise_for_status()
    out: dict[str, float] = {}
    for row in r.json():
        price = row.get("markPrice")
        if not price:
            continue
        try:
            out[row["symbol"]] = float(price)
        except (TypeError, ValueError):
            continue
    if out:
        _MARK_CACHE[0] = now
        _MARK_CACHE[1] = out
    return out


class LeadApi:
    def __init__(self, portfolio_id: str,
                 timeout: tuple[float, float] = (3.05, 10)):
        if not portfolio_id:
            raise ValueError("portfolioId 不能为空")
        self.portfolio_id = str(portfolio_id)
        self.timeout = timeout
        # www.binance.com 的 bapi 端点实测强制直连 8s 超时、走代理 0.18s 通。
        # 所以取数侧用 proxy_first：先本地代理端口，失败再直连兜底。
        # BINANCE_NET_PROXY 可显式指定端口，不依赖 TUN 或系统 PAC 规则。
        self.http = BinanceHttp(mode="proxy_first", read_timeout=10.0)
        self.http.set_headers({
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "clienttype": "web",
            "lang": "zh-CN",
            "Referer": ("https://www.binance.com/zh-CN/copy-trading/"
                        f"lead-details/{self.portfolio_id}"),
        })

    @staticmethod
    def _backoff(attempt: int) -> float:
        # 指数退避 + jitter。11012005 是服务端短时限流，0.5s 起步常常连撞
        # 4 次都过不去；拉到 0.8s 起步、6s 封顶，给限流窗口留出时间。
        return min(0.8 * 2 ** attempt, 6.0) + random.uniform(0, 0.5)

    def _request(self, method: str, path: str, params: dict | None = None,
                 payload: dict | None = None) -> dict:
        """GET/POST 统一入口：网络异常与业务忙码重试，确定性错误立即返回。

        这些 POST 都是只读查询，重试安全；确定性错误（参数错、项目关闭）不重试，
        交给调用方抛 RuntimeError，避免失败时反复刷屏。
        """
        last: dict = {}
        # 忙码最多 6 轮；网络异常仍是最多 4 轮就抛，不跟着放大。
        for attempt in range(6):
            try:
                r = self.http.request(method, BASE + path, params=params,
                                      json=payload, timeout=self.timeout,
                                      attempts=2)
                r.raise_for_status()
                out = r.json()
            except NET_ERRORS as e:
                last = {"code": "NETWORK", "message": f"{type(e).__name__}: {e}"}
                # 底层每次已试过「代理 → 直连」两条通道，外层再兜 2 轮即可。
                if attempt < 2:
                    time.sleep(self._backoff(attempt))
                    continue
                raise RuntimeError(f"{method} {path} 网络失败: {e}") from e
            if out.get("code") == "000000":
                return out
            last = out
            if str(out.get("code")) in RETRY_BUSY_CODES and attempt < 5:
                time.sleep(self._backoff(attempt))
                continue
            return out
        return last

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, payload=payload)

    def detail(self) -> dict:
        out = self._get("friendly/future/copy-trade/lead-portfolio/detail",
                        {"portfolioId": self.portfolio_id})
        if out.get("code") != "000000":
            raise RuntimeError(f"detail 返回 {out.get('code')}: {out.get('message')}")
        return out["data"]

    def performance(self, time_range: str = "30D") -> dict:
        """带单员区间表现（收益率/盈亏/回撤/胜率）。

        数据源是币安公开表现接口，直接返回平台算好的数值，程序不自己算。
        time_range: 7D / 30D / 90D / 365D。失败返回空 dict，绝不抛异常
        —— 这是概览上的补充信息，取不到不该影响跟单主链路。
        """
        try:
            out = self._get(
                "public/future/copy-trade/lead-portfolio/performance",
                {"portfolioId": self.portfolio_id, "timeRange": time_range})
        except Exception:
            return {}
        if out.get("code") != "000000" or not isinstance(out.get("data"), dict):
            return {}
        return out["data"]

    def positions(self) -> list[dict]:
        out = self._get("friendly/future/copy-trade/lead-data/positions",
                        {"portfolioId": self.portfolio_id})
        return out.get("data") or []

    def snapshot_fills(self) -> list[Fill]:
        """当前持仓快照 -> OPEN 记录，用于「轮询持仓 diff」这种更准的跟单方式。

        字段名按常见别名容错，真实响应确认后再收紧映射。

        2026-11-13 14:19 实测：trade-history 已出现 14:04:34 开多 BTCUSDT，
        本接口仍返回 []，说明它有缓存/延迟或只对登录态开放。
        因此主信号源用 trade-history 的秒级聚合结果，这里留作备用通道。
        """
        out: list[Fill] = []
        for r in self.positions():
            symbol = r.get("symbol")
            if not symbol:
                continue
            amt = float(r.get("amount") or r.get("positionAmt") or r.get("qty") or 0)
            price = float(r.get("entryPrice") or r.get("avgCost") or 0)
            if amt == 0 or price == 0:
                continue
            ps = str(r.get("positionSide") or ("SHORT" if amt < 0 else "LONG")).upper()
            side = "SHORT" if ps in ("SHORT", "SELL") or amt < 0 else "LONG"
            qty = abs(amt)
            ts = _ts(r.get("updateTimeStamp") or r.get("updateTime")
                     or r.get("openTime") or time.time() * 1000)
            out.append(Fill(ts=ts, action="OPEN", side=side, symbol=symbol,
                            price=price, qty=qty, notional=price * qty))
        return out

    def trade_history(self, page: int = 1, rows: int = 100) -> dict:
        out = self._post(
            "friendly/future/copy-trade/lead-portfolio/trade-history",
            {"portfolioId": self.portfolio_id, "page": page, "rows": rows})
        if out.get("code") != "000000":
            raise RuntimeError(f"trade-history 返回 {out.get('code')}: {out.get('message')}")
        return out["data"] or {}

    def orders(self, page: int = 1, rows: int = 10) -> dict:
        """「最新操作记录」—— 按**订单**聚合，一条就是一个完整订单。

        实测 trade-history 一次只回 10 条**逐笔撮合**，他一秒里成交几十笔时
        只能拿到尾巴：2026-11-14 02:51 他开 NVDAUSDT 两单共 3206.99 张
        （名义 52.2 万），逐笔接口只回了 539.65 张（8.8 万），仓位算小 6 倍。
        这个端点给的是 executedQty（整单成交量），不会截断。
        分页参数（page/rows）服务端仍然忽略，固定回最近 10 单。
        """
        out = self._post(
            "friendly/future/copy-trade/lead-portfolio/order-history",
            {"portfolioId": self.portfolio_id, "page": page, "rows": rows})
        if out.get("code") != "000000":
            raise RuntimeError(
                f"order-history 返回 {out.get('code')}: {out.get('message')}")
        return out["data"] or {}

    def position_history(self, page: int = 1, rows: int = 50) -> dict:
        out = self._post(
            "friendly/future/copy-trade/lead-portfolio/position-history",
            {"portfolioId": self.portfolio_id, "page": page, "rows": rows})
        if out.get("code") != "000000":
            raise RuntimeError(
                f"position-history 返回 {out.get('code')}: {out.get('message')}")
        return out["data"] or {}

    def fills(self, page: int = 1, rows: int = 10,
              aggregate: bool = True) -> list[Fill]:
        """拉取并转换一次。默认把同一秒同方向的订单聚合成一条操作记录。

        数据源是 order-history（按订单聚合）而不是 trade-history（逐笔撮合）：
        后者一次只回 10 条逐笔，大单会被截断，仓位直接算小。
        """
        raw = self.order_fills(self.orders(page, rows))
        return self.aggregate(raw) if aggregate else raw

    @staticmethod
    def order_fills(data: dict) -> list[Fill]:
        """「最新操作记录」-> Fill。

        side/positionSide 决定开平：LONG 的 BUY 是开、SELL 是平；
        SHORT 的 SELL 是开、BUY 是平。executedQty 是整单成交量，
        avgPrice 是整单成交均价，两者相乘就是这笔操作的名义。
        """
        out: list[Fill] = []
        for row in data.get("list", []):
            qty = float(row.get("executedQty") or 0)
            price = float(row.get("avgPrice") or 0)
            if qty <= 0 or price <= 0:
                continue                      # 撤单/未成交，不构成操作
            side = ("SHORT" if str(row.get("positionSide", "")).upper() == "SHORT"
                    else "LONG")
            deal = str(row.get("side", "")).upper()
            is_open = ((side == "SHORT" and deal == "SELL")
                       or (side == "LONG" and deal == "BUY"))
            pnl = row.get("totalPnl")
            out.append(Fill(
                ts=_ts(row.get("orderTime") or row.get("orderUpdateTime")),
                action="OPEN" if is_open else "CLOSE",
                side=side,
                symbol=row["symbol"],
                price=price,
                qty=qty,
                notional=price * qty,
                realized_pnl=(float(pnl) if pnl not in (None, "") else None)
                             if not is_open else None))
        out.sort(key=lambda x: (x.ts, x.action != "OPEN"))
        return out

    @staticmethod
    def aggregate(fills: list[Fill]) -> list[Fill]:
        """网页「最新操作记录」是聚合展示：一秒内的几十笔撮合成一条。

        不聚合会把一次平空拆成几十个 CLOSE 事件，虚拟仓位账本会被反复核销。
        聚合后的 Fill.key 就是这个桶的标识，跨轮拉到同样的秒不会重复开平。
        """
        buckets: dict[tuple, list[Fill]] = {}
        for f in fills:
            key = (f.ts[:19], f.symbol, f.side, f.action)
            buckets.setdefault(key, []).append(f)
        out: list[Fill] = []
        for (ts, symbol, side, action), group in buckets.items():
            qty = sum(x.qty for x in group)
            notional = sum(x.notional for x in group)
            pnls = [x.realized_pnl for x in group if x.realized_pnl is not None]
            out.append(Fill(
                ts=ts, action=action, side=side, symbol=symbol,
                price=notional / qty if qty else 0.0,
                qty=qty, notional=notional,
                realized_pnl=sum(pnls) if pnls else None))
        out.sort(key=lambda x: (x.ts, x.action != "OPEN"))
        return out

    @staticmethod
    def fills_from(data: dict) -> list[Fill]:
        """把已有的逐笔成交 JSON 转成 Fill 列表，不再发请求。"""
        out: list[Fill] = []
        for row in data.get("list", []):
            symbol = row["symbol"]
            pos_side = str(row.get("positionSide", "")).upper()
            deal_side = str(row.get("side", "")).upper()
            side = "SHORT" if pos_side == "SHORT" else "LONG"
            # SHORT 仓位：SELL 开空 / BUY 平空；LONG 仓位：BUY 开多 / SELL 平多
            is_open = ((side == "SHORT" and deal_side == "SELL") or
                       (side == "LONG" and deal_side == "BUY"))
            price = float(row["price"])
            qty = float(row["qty"])
            notional = float(row.get("quantity") or price * qty)
            pnl = row.get("realizedProfit")
            f = Fill(
                ts=_ts(row["time"]),
                action="OPEN" if is_open else "CLOSE",
                side=side,
                symbol=symbol,
                price=price,
                qty=qty,
                notional=notional,
                realized_pnl=float(pnl) if pnl not in (None, "") else None,
            )
            if f.notional_gap > 0.002:
                raise ValueError(f"price*qty 与 quantity 偏差 {f.notional_gap:.4%}: {f.key}")
            out.append(f)
        # 接口按时间倒序返回，账本需要按时间正序重放
        out.sort(key=lambda x: (x.ts, x.action != "OPEN"))
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--portfolio-id", default=EXAMPLE_PORTFOLIO_ID)
    ap.add_argument("--rows", type=int, default=20)
    ap.add_argument("--json", action="store_true", help="输出原始 JSON 而非 Fill 摘要")
    ap.add_argument("--snapshot", action="store_true", help="打印当前持仓快照原始 JSON")
    args = ap.parse_args()

    pid = str(args.portfolio_id)
    # 命令行没给就跟本地配置走，这样本机回归仍然测真实接口；
    # 别人 clone 下来没有 gui-config.json，自然落到下面的 SKIP。
    if pid in ("", EXAMPLE_PORTFOLIO_ID):
        cfg_path = Path(__file__).resolve().parent / "gui-config.json"
        try:
            pid = str(json.loads(cfg_path.read_text(encoding="utf-8"))
                      .get("portfolio_id") or pid)
        except (OSError, ValueError):
            pass
    args.portfolio_id = pid

    # 没配真实带单员就别去打接口：占位 ID 必然 404，报 FAIL 只会误导。
    # 返回 2 = 未配置/不可用，run_all.py 会标 SKIP 而不是失败。
    if str(args.portfolio_id) in ("", EXAMPLE_PORTFOLIO_ID):
        print("未配置真实带单员 portfolioId（gui-config.json 或 "
              "--portfolio-id），本项跳过", file=sys.stderr)
        return 2

    api = LeadApi(args.portfolio_id)
    try:
        if args.snapshot:
            print(json.dumps(api.positions(), ensure_ascii=False, indent=2))
            return 0
        d = api.detail()
        print(f" nickname={d.get('nickname')} status={d.get('status')} "
              f"copiers={d.get('currentCopyCount')}/{d.get('maxCopyCount')} "
              f"marginBalance={d.get('marginBalance')} aum={d.get('aumAmount')}",
              file=sys.stderr)
        data = api.orders(rows=args.rows)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"total={data.get('total')} returned={len(data.get('list', []))}",
                  file=sys.stderr)
            for f in api.aggregate(api.order_fills(data)):
                print(f"  {f.ts} {f.action:5s} {f.side:5s} {f.symbol:12s} "
                      f"{f.price} x {f.qty} = {f.notional} pnl={f.realized_pnl}")
        return 0
    except Exception as e:
        msg = str(e)
        if "11012005" in msg or "当前系统正忙" in msg:
            # 币安对这个公开端点的限流窗口可达 1 分钟以上，重试再久也可能撞上。
            # 这是外部服务状态，不是程序故障：rc=2 让回归标 SKIP，GUI 轮询
            # 会退避重连，恢复后拉全量历史补回事件，不会漏单。
            print("未执行：带单本接口限流(11012005)，稍后单独复测；"
                  "GUI 轮询会自动退避重连，事件靠全量历史补回。",
                  file=sys.stderr)
            return 2
        print(f"{type(e).__name__}: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
