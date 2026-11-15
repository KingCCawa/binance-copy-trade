"""账户连接自检：验证 API Key 能不能读合约账户、权限配置是否正确。

不保存密钥明文，不打印密钥，只做只读检查（不下单、不撤单）。

用法：
  py -3.12 account.py --check
  py -3.12 account.py --check --json
  py -3.12 account.py --check --live          # 检查实盘域名（默认 testnet）
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from net import BinanceHttp

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def load_env(path: Path | None = None) -> list[str]:
    """把 .env 读进 os.environ（已存在的环境变量优先），返回读到的键名。"""
    p = path or ENV_FILE
    if not p.exists():
        return []
    keys = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
        if key:
            keys.append(key)
    return keys


class AccountProbe:
    def __init__(self, key: str | None = None, secret: str | None = None,
                 testnet: bool = True, timeout: int = 10):
        self.key = key or os.getenv("BINANCE_API_KEY", "")
        self.secret = secret or os.getenv("BINANCE_API_SECRET", "")
        self.testnet = testnet
        self.timeout = timeout
        self.fapi = ("https://testnet.binancefuture.com" if testnet
                     else "https://fapi.binance.com")
        self.sapi = ("https://testnet.binance.vision" if testnet
                     else "https://api.binance.com")
        self.offset = 0
        # 直连优先 / 系统代理兜底；不需要开 TUN，代理临时挂掉也能绕过去。
        self.http = BinanceHttp()
        self.http.set_headers({
            "User-Agent": UA,
            "X-MBX-APIKEY": self.key,
            "Accept": "application/json",
        })

    @property
    def masked_key(self) -> str:
        if not self.key:
            return "未设置"
        return f"{self.key[:4]}...{self.key[-4:]} (len={len(self.key)})"

    def _signed(self, extra: dict | None = None) -> dict:
        params = dict(extra or {})
        params["timestamp"] = int(time.time() * 1000) + self.offset
        params["recvWindow"] = 5000
        query = urlencode(params)
        params["signature"] = hmac.new(
            self.secret.encode("utf-8"), query.encode("utf-8"),
            hashlib.sha256).hexdigest()
        return params

    def _get(self, host: str, path: str, signed: bool = False) -> tuple[int, dict | list]:
        params = self._signed() if signed else {}
        # 只读请求，重试安全。超时拆成 (连接, 读)：连不上 3 秒就换通道，
        # 不会像原来那样卡满 10 秒才判失败。
        r = self.http.request("GET", host + path, params=params,
                              timeout=(3.05, float(self.timeout)), attempts=3)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"raw": r.text[:300]}

    def check(self) -> dict:
        out: dict = {
            "testnet": self.testnet,
            "fapi_host": self.fapi,
            "key": self.masked_key,
            "secret_present": bool(self.secret),
            "reachable": False,
            "account_ok": False,
            "warnings": [],
        }
        if not self.key or not self.secret:
            out["error"] = "缺少 BINANCE_API_KEY / BINANCE_API_SECRET"
            return out

        # 1. 公开时间端点 + 时钟校准（签名请求对时间偏差敏感，超 5s 会 -1021）
        try:
            # 首次请求含 TLS 握手，RTT 会虚高并把 offset 算成几十秒偏差；
            # 采样 3 次，取 RTT 最小的那次估算时钟差。
            best = None
            last_error = ""
            for _ in range(3):
                t0 = int(time.time() * 1000)
                status, body = self._get(self.fapi, "/fapi/v1/time")
                t1 = int(time.time() * 1000)
                if status == 200 and isinstance(body, dict) and "serverTime" in body:
                    rtt = t1 - t0
                    server = int(body["serverTime"])
                    offset = server - (t0 + t1) // 2
                    if best is None or rtt < best[0]:
                        best = (rtt, offset, server)
                else:
                    last_error = f"时间端点 HTTP {status}: {str(body)[:200]}"
            if best is None:
                out["error"] = last_error or "时间端点无响应"
                return out
            self.offset = best[1]
            out["server_time_ms"] = best[2]
            out["clock_offset_ms"] = self.offset
            out["time_rtt_ms"] = best[0]
            out["reachable"] = True
            if abs(self.offset) > 3000:
                out["warnings"].append(
                    f"本机与交易所时钟差 {self.offset} ms，签名请求可能被拒，请校准系统时间")
        except requests.RequestException as e:
            out["error"] = f"无法访问 {self.fapi}: {type(e).__name__}: {e}"
            return out

        # 2. 签名读账户：这一步通过 = Key/Secret 正确且有合约读取权限
        try:
            status, body = self._get(self.fapi, "/fapi/v2/account", signed=True)
            if status != 200 or not isinstance(body, dict):
                out["error"] = f"/fapi/v2/account HTTP {status}: {str(body)[:250]}"
                return out
            out["account_ok"] = True
            out["total_wallet_balance"] = float(body.get("totalWalletBalance", 0))
            out["available_balance"] = float(body.get("availableBalance", 0))
            out["total_unrealized_profit"] = float(body.get("totalUnrealizedProfit", 0))
            out["can_trade"] = bool(body.get("canTrade", True))
            if not out["can_trade"]:
                out["warnings"].append("账户 canTrade=false，下单会被拒绝")
        except requests.RequestException as e:
            out["error"] = f"账户查询失败: {type(e).__name__}: {e}"
            return out

        # 3. 当前持仓
        try:
            status, body = self._get(self.fapi, "/fapi/v2/positionRisk", signed=True)
            if status == 200 and isinstance(body, list):
                out["open_positions"] = [
                    {"symbol": p.get("symbol"), "qty": float(p.get("positionAmt", 0)),
                     "entry": float(p.get("entryPrice", 0)),
                     "leverage": p.get("leverage")}
                    for p in body if float(p.get("positionAmt", 0)) != 0]
            else:
                out["open_positions"] = []
                out["warnings"].append(f"持仓查询 HTTP {status}，跳过")
        except requests.RequestException:
            out["open_positions"] = []

        # 4. Key 权限自检（sapi 域；testnet 或 Key 无 USER_DATA 权限时会拿不到）
        try:
            status, body = self._get(self.sapi, "/sapi/v1/account/apiRestrictions",
                                     signed=True)
            if status == 200 and isinstance(body, dict):
                out["restrictions"] = body
                if body.get("enableWithdrawals") is True:
                    out["warnings"].append("这个 Key 开了提现权限，立刻去关掉")
                if body.get("enableFutures") is False:
                    out["warnings"].append("这个 Key 没开合约权限（Enable Futures）")
                if body.get("ipRestrict") is not True:
                    out["warnings"].append("没绑 IP 白名单，建议绑定本机公网 IP")
            else:
                out["restrictions"] = None
                out["warnings"].append(f"权限端点 HTTP {status}（可降级，不影响下单）")
        except requests.RequestException:
            out["restrictions"] = None
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--live", action="store_true", help="检查实盘而不是 testnet")
    args = ap.parse_args()

    loaded = load_env()
    testnet = not args.live and os.getenv("BINANCE_TESTNET", "true").lower() == "true"
    result = AccountProbe(testnet=testnet).check()
    result["env_loaded"] = loaded

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for k in ("fapi_host", "key", "secret_present", "reachable", "account_ok",
                  "clock_offset_ms", "total_wallet_balance", "available_balance",
                  "total_unrealized_profit", "can_trade", "error"):
            if k in result:
                print(f"  {k:26s} {result[k]}")
        print(f"  {'open_positions':26s} {result.get('open_positions', [])}")
        r = result.get("restrictions")
        if isinstance(r, dict):
            print(f"  {'enableFutures':26s} {r.get('enableFutures')} | "
                  f"enableWithdrawals={r.get('enableWithdrawals')} | "
                  f"ipRestrict={r.get('ipRestrict')}")
        for w in result.get("warnings", []):
            print(f"  WARN  {w}")
        print("  OK: 账户连接可用" if result.get("account_ok") else "  FAILED: 账户连接不可用")
    return 0 if result.get("account_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
