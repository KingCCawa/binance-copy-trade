"""Binance Futures API integration."""

import hashlib
import hmac
import logging
import time
from typing import Optional, List, Dict, Any
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

try:
    from net import BinanceHttp
except ImportError:                      # 直接从 vendor 目录单跑时补上项目根
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from net import BinanceHttp


class BinanceService:
    """Binance Futures API client."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        """Initialize Binance client."""
        self.api_key = api_key
        self.api_secret = api_secret

        # Set base URL based on testnet flag
        if testnet:
            self.base_url = "https://testnet.binancefuture.com"
        else:
            self.base_url = "https://fapi.binance.com"

        self.base_url = self.base_url.rstrip("/")

        # Import requests
        try:
            import requests
            self.requests = requests
        except ImportError:
            raise ImportError("requests library required. Install with: pip install requests")

        # 直连优先 / 系统代理兜底，网络抖动时自动换通道重试；不需要开 TUN。
        self.http = BinanceHttp()

        # Exchange info cache
        self._exchange_info: Optional[Dict] = None
        # 账户是否双向持仓（hedge）模式，None = 还没查过
        self._dual_side: Optional[bool] = None
        self._server_time_offset = 0
        # 改动：原版从不校准时钟，签名请求在本机时间偏差大时会直接 -1021
        self._sync_server_time()

    def _sync_server_time(self) -> None:
        """采样 3 次取 RTT 最小值估算时钟差，避开首次 TLS 握手延迟。"""
        best = None
        for _ in range(3):
            try:
                t0 = int(time.time() * 1000)
                server = self._get_server_time()
                t1 = int(time.time() * 1000)
                rtt = t1 - t0
                offset = server - (t0 + t1) // 2
                if best is None or rtt < best[0]:
                    best = (rtt, offset)
            except Exception:
                continue
        if best is not None:
            self._server_time_offset = best[1]
            logger.info("Server time offset: %d ms (rtt %d ms)", best[1], best[0])
        else:
            logger.warning("Failed to sync server time")

    def _get_server_time(self) -> int:
        """Get Binance server time."""
        try:
            response = self.http.request(
                "GET", f"{self.base_url}/fapi/v1/time",
                timeout=(3.05, 5), attempts=2)
            response.raise_for_status()
            return response.json()["serverTime"]
        except Exception as e:
            logger.warning(f"Failed to get server time: {e}")
            return int(time.time() * 1000)

    def _sign_request(self, params: Dict[str, Any]) -> str:
        """Generate HMAC SHA256 signature."""
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return signature

    @staticmethod
    def _retry_attempts(method: str, endpoint: str,
                        params: Optional[Dict]) -> int:
        """只读/幂等请求可重试；下单 POST 一律不重试（避免重复成交）。

        下单成功但响应丢失的情况不靠重试解决：CopyEngine 每轮都会拿
        clientOrderId 从 openOrders 把孤儿单认回来，见 rebuild_from_exchange()。
        """
        if method in ("GET", "DELETE"):
            return 3
        if endpoint in ("/fapi/v1/leverage", "/fapi/v1/marginType"):
            return 3
        return 1

    def _request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                 signed: bool = False) -> Optional[Dict]:
        """Make API request."""
        if params is None:
            params = {}

        url = f"{self.base_url}{endpoint}"
        headers = {"X-MBX-APIKEY": self.api_key}

        # Add timestamp for signed requests
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self._server_time_offset
            params["recvWindow"] = 5000
            params["signature"] = self._sign_request(params)

        attempts = self._retry_attempts(method, endpoint, params)
        try:
            # 超时拆成 (连接, 读)：连不上 3 秒就换通道，不卡满 10 秒。
            response = self.http.request(
                method, url, params=params, headers=headers,
                timeout=(3.05, 10), attempts=attempts)
            response.raise_for_status()
            return response.json()

        except self.requests.exceptions.HTTPError as e:
            resp = e.response
            if (resp is not None and resp.status_code == 401
                    and "testnet" in self.base_url):
                logger.error("测试网返回 401：实盘 API Key 不能用于 testnet。"
                             "请在测试网另建 Key，或把执行模式切回 dry-run / live")
            else:
                logger.error(f"Binance API error: {e}, Response: "
                             f"{resp.text if resp is not None else 'N/A'}")
            return None
        except Exception as e:
            logger.error(f"Request failed: {e}")
            return None

    def get_api_restrictions(self) -> Optional[Dict]:
        """查询这个 API Key 的权限（现货 sapi 域，不是 fapi）。

        返回字段含 enableReading / enableFutures / enableWithdrawals / ipRestrict。
        Key 没开 USER_DATA 权限或 testnet 不支持时返回 None，属于可降级的自检项。
        """
        host = ("https://testnet.binance.vision" if "testnet" in self.base_url
                else "https://api.binance.com")
        params = {
            "timestamp": int(time.time() * 1000) + self._server_time_offset,
            "recvWindow": 5000,
        }
        params["signature"] = self._sign_request(params)
        try:
            response = self.http.request(
                "GET", host + "/sapi/v1/account/apiRestrictions",
                params=params, headers={"X-MBX-APIKEY": self.api_key},
                timeout=(3.05, 10), attempts=3)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.warning("apiRestrictions unavailable: %s", e)
            return None

    def get_account_info(self) -> Optional[Dict]:
        """Get futures account information."""
        return self._request("GET", "/fapi/v2/account", signed=True)

    def get_balance(self) -> Optional[Dict]:
        """Get account balance."""
        account = self.get_account_info()
        if not account:
            return None

        return {
            "total_balance": float(account.get("totalWalletBalance", 0)),
            "available_balance": float(account.get("availableBalance", 0)),
        }

    def get_positions(self) -> List[Dict]:
        """Get all open positions."""
        data = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        if not data:
            return []

        # Filter only positions with non-zero quantity
        positions = []
        for pos in data:
            qty = float(pos.get("positionAmt", 0))
            if qty != 0:
                positions.append({
                    "symbol": pos["symbol"],
                    "side": "LONG" if qty > 0 else "SHORT",
                    "quantity": abs(qty),
                    "entry_price": float(pos.get("entryPrice", 0)),
                    "unrealized_pnl": float(pos.get("unRealizedProfit", 0)),
                    "leverage": int(pos.get("leverage", 1)),
                })
        return positions

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get open orders."""
        params = {}
        if symbol:
            params["symbol"] = symbol

        data = self._request("GET", "/fapi/v1/openOrders", params, signed=True)
        if not data:
            return []

        return [{
            "order_id": order["orderId"],
            "symbol": order["symbol"],
            "side": order["side"],
            "type": order["type"],
            "price": float(order.get("price", 0)),
            "quantity": float(order["origQty"]),
            "stop_price": float(order.get("stopPrice", 0)),
            "client_order_id": order.get("clientOrderId", ""),
        } for order in data]

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol."""
        params = {"symbol": symbol, "leverage": leverage}
        result = self._request("POST", "/fapi/v1/leverage", params, signed=True)
        return result is not None

    def set_margin_mode(self, symbol: str, margin_type: str) -> bool:
        """Set margin mode (ISOLATED or CROSSED)."""
        params = {"symbol": symbol, "marginType": margin_type}
        result = self._request("POST", "/fapi/v1/marginType", params, signed=True)
        # Note: API returns error if already in that mode, which is okay
        return True

    def get_symbol_precision(self, symbol: str) -> tuple[int, int]:
        """Get price and quantity precision for a symbol."""
        if not self._exchange_info:
            data = self._request("GET", "/fapi/v1/exchangeInfo")
            if data:
                self._exchange_info = data

        if not self._exchange_info:
            return 2, 3  # Defaults

        for s in self._exchange_info.get("symbols", []):
            if s["symbol"] == symbol:
                price_precision = s.get("pricePrecision", 2)
                qty_precision = s.get("quantityPrecision", 3)
                return price_precision, qty_precision

        return 2, 3

    def get_symbol_tick(self, symbol: str) -> float:
        """真实最小价格变动单位（PRICE_FILTER.tickSize）。

        不能拿 pricePrecision 代替：代币化股票合约（NVDAUSDT / SOXLUSDT）
        pricePrecision=5 但 tickSize=0.01，按 1e-5 算出来的挂单价会被
        交易所以 -4014「Price not increased by tick size」直接拒掉。
        查不到时返回 0，由调用方回落到 pricePrecision。
        """
        if not self._exchange_info:
            data = self._request("GET", "/fapi/v1/exchangeInfo")
            if data:
                self._exchange_info = data
        for s in (self._exchange_info or {}).get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            for f in s.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    try:
                        return float(f.get("tickSize") or 0)
                    except (TypeError, ValueError):
                        return 0.0
            return 0.0
        return 0.0

    def is_dual_side(self) -> bool:
        """账户是否「双向持仓」（hedge）模式。

        双向模式下每笔订单必须带 positionSide，不带就 -4061
        「Order's position side does not match user's setting」；
        单向模式下反过来，带了也是同样的报错。所以只能按账户实际模式决定，
        不能写死。查询结果缓存一次；查询失败时保持未知（下次再查）。
        """
        if self._dual_side is None:
            data = self._request("GET", "/fapi/v1/positionSide/dual",
                                 signed=True)
            if isinstance(data, dict) and "dualSidePosition" in data:
                self._dual_side = bool(data["dualSidePosition"])
        return bool(self._dual_side)

    def _apply_position_side(self, params: Dict[str, Any], side: str,
                             reduce_only: bool) -> None:
        """按持仓模式补 positionSide / reduceOnly。

        双向持仓：
          开仓 positionSide = 订单方向（BUY→LONG / SELL→SHORT）
          平仓 positionSide = 持仓方向（SELL 平多→LONG / BUY 平空→SHORT）
          两者都由 (side, reduceOnly) 唯一确定，调用方不用另传参数。
          另外双向模式下**不能**发 reduceOnly，会被 -1106 拒；平仓方向由
          positionSide 表达，数量由调用方夹到持仓量以内，不会反手开新仓。
        单向持仓：不发 positionSide，平仓靠 reduceOnly 兜住数量算错的情况。
        """
        side = side.upper()
        if self.is_dual_side():
            if reduce_only:
                params["positionSide"] = "LONG" if side == "SELL" else "SHORT"
            else:
                params["positionSide"] = "LONG" if side == "BUY" else "SHORT"
        elif reduce_only:
            params["reduceOnly"] = "true"

    def format_quantity(self, symbol: str, quantity: float) -> float:
        """Format quantity to symbol precision."""
        _, qty_precision = self.get_symbol_precision(symbol)
        return round(quantity, qty_precision)

    def format_price(self, symbol: str, price: float) -> float:
        """Format price to symbol precision."""
        price_precision, _ = self.get_symbol_precision(symbol)
        return round(price, price_precision)

    def place_market_order(self, symbol: str, side: str, quantity: float,
                           reduce_only: bool = False) -> Optional[Dict]:
        """Place a market order."""
        quantity = self.format_quantity(symbol, quantity)

        params = {
            "symbol": symbol,
            "side": side.upper(),  # BUY or SELL
            "type": "MARKET",
            "quantity": quantity,
        }
        self._apply_position_side(params, side, reduce_only)

        logger.info(f"Placing market order: {side} {quantity} {symbol}")
        result = self._request("POST", "/fapi/v1/order", params, signed=True)

        if result:
            return {
                "order_id": result["orderId"],
                "symbol": result["symbol"],
                "side": result["side"],
                "quantity": float(result["origQty"]),
                "status": result["status"],
            }
        return None

    def place_limit_order(self, symbol: str, side: str, quantity: float,
                         price: float, reduce_only: bool = False,
                         client_order_id: Optional[str] = None) -> Optional[Dict]:
        """Place a limit order (for TP/SL).

        client_order_id 用于重启后把这笔单认回来；只给跟单入场单传，
        账户里手动挂的单不带这个标记，对账时不会被接管。
        """
        quantity = self.format_quantity(symbol, quantity)
        price = self.format_price(symbol, price)

        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "LIMIT",
            "quantity": quantity,
            "price": price,
            "timeInForce": "GTC",
        }

        self._apply_position_side(params, side, reduce_only)
        if client_order_id:
            params["newClientOrderId"] = client_order_id

        logger.info(f"Placing limit order: {side} {quantity} {symbol} @ {price}")
        result = self._request("POST", "/fapi/v1/order", params, signed=True)

        if result:
            return {
                "order_id": result["orderId"],
                "symbol": result["symbol"],
                "side": result["side"],
                "quantity": float(result["origQty"]),
                "price": float(result["price"]),
                "status": result["status"],
            }
        return None

    def place_stop_market_order(self, symbol: str, side: str, quantity: float,
                                stop_price: float, reduce_only: bool = True) -> Optional[Dict]:
        """Place a stop-market order (for SL)."""
        quantity = self.format_quantity(symbol, quantity)
        stop_price = self.format_price(symbol, stop_price)

        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "STOP_MARKET",
            "quantity": quantity,
            "stopPrice": stop_price,
        }

        self._apply_position_side(params, side, reduce_only)

        logger.info(f"Placing stop-market order: {side} {quantity} {symbol} @ stop {stop_price}")
        result = self._request("POST", "/fapi/v1/order", params, signed=True)

        if result:
            return {
                "order_id": result["orderId"],
                "symbol": result["symbol"],
                "side": result["side"],
                "quantity": float(result["origQty"]),
                "stop_price": float(result["stopPrice"]),
                "status": result["status"],
            }
        return None

    def cancel_order(self, symbol: str, order_id: int) -> bool:
        """Cancel an order."""
        params = {"symbol": symbol, "orderId": order_id}
        result = self._request("DELETE", "/fapi/v1/order", params, signed=True)
        return result is not None

    def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for a symbol."""
        params = {"symbol": symbol}
        result = self._request("DELETE", "/fapi/v1/allOpenOrders", params, signed=True)
        return result is not None

    def close_position(self, symbol: str,
                       quantity: Optional[float] = None) -> bool:
        """平仓；quantity=None 整仓平，否则按指定数量减仓。

        部分平仓必须带 reduceOnly，否则数量算错时会反向开出一个新仓位。
        """
        positions = self.get_positions()
        for pos in positions:
            if pos["symbol"] == symbol:
                side = "SELL" if pos["side"] == "LONG" else "BUY"
                qty = abs(float(pos["quantity"]))
                if quantity is not None:
                    qty = min(qty, abs(float(quantity)))
                if qty <= 0:
                    return True
                result = self.place_market_order(symbol, side, qty,
                                                 reduce_only=True)
                return result is not None

        logger.warning(f"No position found for {symbol}")
        return False
