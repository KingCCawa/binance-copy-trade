"""币安请求网络层：直连优先 / 系统代理兜底 + 退避重试。

两个币安域名的可达性不同，不能用同一套通道顺序：

  www.binance.com 的 bapi（取数）
    curl --noproxy '*' POST .../trade-history  -> 8s 超时，直连不通
    curl -x http://127.0.0.1:20122 同一地址     -> 200 / 0.18s
    所以 api_feed.LeadApi 固定用 mode="proxy_first"。

  fapi.binance.com（合约 API / 下单）
    直连可达与否取决于本机代理或 TUN 状态，两种都出现过。
    所以 account.py / vendor/chatdsn/binance.py 用默认 auto 两条都试。

模式：
  auto         无代理时直连 → 代理 → 直连；探测到代理后代理 → 直连 → 代理
  proxy_first  代理 → 直连 → 代理（www.binance.com bapi 默认）
  direct       只强制直连
  proxy        只走代理

首次成功后记住通道（_preferred），后续请求直接走它，不必每轮先撞一次
不通的路。只有网络异常和 5xx/429 才重试；4xx 直接交给调用方。

代理由 detect_proxy() 在首次使用时自动探测本机常见端口（HTTP 混合口 /
SOCKS5），不同机器端口不同也能适配，不依赖写死的数字或不入库的 .env。
优先级：proxy_url 参数 > 环境变量 BINANCE_NET_PROXY > .env 同名键 > 自动探测。
探测结果进程内缓存；proxy_url="" 可在代码里显式禁用。

.env 可覆盖：
  BINANCE_NET_MODE=auto|direct|proxy|proxy_first
  BINANCE_NET_PROXY=http://127.0.0.1:7890    可选，留空则自动探测
  BINANCE_NET_TIMEOUT=8                       读超时秒数
"""
from __future__ import annotations

import concurrent.futures
import os
import random
import threading
import time
from pathlib import Path

import requests

CONNECT_TIMEOUT = 3.05
MODES = ("auto", "direct", "proxy", "proxy_first")

# 常见本地代理端口：Clash 7890/7897、v2rayN 10809/10808、本机实测 20122 等。
# 每个端口同时试 http 与 socks5 两种 scheme；谁能让请求出去就用谁。
COMMON_PROXY_PORTS = (
    7890, 7897, 7891, 10809, 10808, 20122, 20123, 1080, 1087, 1086,
    8888, 33210, 2080, 2081, 10810, 10811, 8118, 8123, 3128, 8080,
)
_PROXY_PROBE_URL = "https://fapi.binance.com/fapi/v1/time"
_PROBE_CONNECT_TIMEOUT = 0.8
DETECT_TTL = 30.0          # 缓存秒数；代理软件重启换端口后会重新探测
_detect_state: dict = {"url": "", "at": 0.0}
_detect_lock = threading.Lock()


def _try_proxy(url: str, read_timeout: float) -> str:
    """单个候选代理实测一次；能拿到 200 就返回该 URL，否则返回空。"""
    try:
        r = requests.get(
            _PROXY_PROBE_URL,
            proxies={"http": url, "https": url},
            timeout=(_PROBE_CONNECT_TIMEOUT, read_timeout),
        )
        if r.status_code == 200:
            return url
    except Exception:
        pass
    return ""


def detect_proxy(read_timeout: float = 3.0, refresh: bool = False) -> str:
    """探测本机可用代理，返回可直用的 URL；没有则空串。

    并发探测全部候选，取第一个成功的；进程内按 DETECT_TTL 缓存，避免每
    个 Session 都重扫一遍。显式配置（参数 / 环境变量 / .env）优先于本函数。
    """
    now = time.monotonic()
    with _detect_lock:
        if (not refresh and _detect_state["url"]
                and now - _detect_state["at"] < DETECT_TTL):
            return _detect_state["url"]
        candidates = [
            f"{scheme}://127.0.0.1:{port}"
            for port in COMMON_PROXY_PORTS
            for scheme in ("http", "socks5")
        ]
        found = ""
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            futures = [ex.submit(_try_proxy, u, read_timeout) for u in candidates]
            for fut in concurrent.futures.as_completed(futures):
                url = fut.result()
                if url:
                    found = url
                    break
            for fut in futures:
                fut.cancel()
        _detect_state["url"] = found
        _detect_state["at"] = now
        return found


def _dotenv_value(key: str, default: str = "") -> str:
    """环境变量优先，其次读项目根 .env；让 CLI 和 GUI 拿到同一份网络配置。"""
    if key in os.environ:
        return os.environ[key]
    p = Path(__file__).resolve().parent / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


class BinanceHttp:
    """两个 Session 交替用；对外只暴露 request() 与 set_headers()。"""

    def __init__(self, mode: str | None = None, read_timeout: float = 8.0,
                 proxy_url: str | None = None):
        self.mode = (mode or _dotenv_value("BINANCE_NET_MODE", "auto")).strip().lower()
        if self.mode not in MODES:
            self.mode = "auto"
        # 显式指定优先：proxy_url="" 表示禁用（自检用）。None 时读环境变量
        # / .env；两者都没有则自动探测本机代理端口，不写死任何数字。
        self.proxy_url = (_dotenv_value("BINANCE_NET_PROXY", "")
                          if proxy_url is None else proxy_url).strip()
        try:
            self.read_timeout = float(
                _dotenv_value("BINANCE_NET_TIMEOUT", str(read_timeout)))
        except ValueError:
            self.read_timeout = read_timeout
        self.timeout = (CONNECT_TIMEOUT, self.read_timeout)
        if proxy_url is None and not self.proxy_url:
            self.proxy_url = detect_proxy(min(self.read_timeout, 3.0))

        self.direct = requests.Session()
        self.direct.trust_env = False       # 完全忽略系统/环境代理
        self.proxied = requests.Session()   # 跟随系统代理（Windows 读注册表）
        if self.proxy_url:
            self.proxied.trust_env = False
            self.proxied.proxies = {"http": self.proxy_url,
                                    "https": self.proxy_url}
        self.headers: dict[str, str] = {}
        # 记住上次成功的通道，后续请求直接走它，不必每轮都先撞一次不通的路。
        self._preferred: str | None = None

    def set_headers(self, headers: dict) -> None:
        self.headers = dict(headers)
        self.direct.headers.update(self.headers)
        self.proxied.headers.update(self.headers)

    def _pick(self, attempt: int):
        if attempt == 0 and self._preferred:
            return ((self.direct, "direct") if self._preferred == "direct"
                    else (self.proxied, "proxy"))
        if self.mode == "direct":
            return self.direct, "direct"
        if self.mode == "proxy":
            return self.proxied, "proxy"
        if self.mode == "proxy_first":
            # www.binance.com 的 bapi 直连实测超时，先走代理省掉那 3 秒。
            return ((self.proxied, "proxy") if attempt % 2 == 0
                    else (self.direct, "direct"))
        # auto：默认直连优先（fapi 直连通常可达）；但只要 .env 里显式配了
        # BINANCE_NET_PROXY，就改成代理优先 —— 用户专门配了代理，说明直连
        # 在他网络下不稳，先撞直连会造成「不开 TUN 就连不上」的假象。
        if self.mode == "auto" and self.proxy_url:
            return ((self.proxied, "proxy") if attempt % 2 == 0
                    else (self.direct, "direct"))
        if attempt % 2 == 0:
            return self.direct, "direct"
        return self.proxied, "proxy"

    def request(self, method: str, url: str, *, attempts: int = 3, **kw):
        """返回 Response；全部轮次失败则抛 ConnectionError 或返回最后的 5xx。"""
        kw.setdefault("timeout", self.timeout)
        errors: list[str] = []
        last_response = None
        for attempt in range(max(1, attempts)):
            session, label = self._pick(attempt)
            try:
                r = session.request(method, url, **kw)
                last_response = r
                if r.status_code < 500 and r.status_code != 429:
                    self._preferred = label
                    return r
                errors.append(f"{label}:HTTP{r.status_code}")
            except requests.exceptions.RequestException as e:
                errors.append(f"{label}:{type(e).__name__}")
            if attempt < attempts - 1:
                time.sleep(min(0.4 * 2 ** attempt, 2.0)
                           + random.uniform(0, 0.2))
        if last_response is not None:
            return last_response
        raise requests.exceptions.ConnectionError(
            f"{method} {url} 重试 {attempts} 次仍失败 → " + ", ".join(errors))

    def describe(self) -> str:
        return (f"net={self.mode} timeout={self.timeout} "
                f"direct={not self.direct.trust_env} "
                f"proxied={self.proxied.trust_env} "
                f"explicit_proxy={self.proxy_url or '-'}")


def selftest() -> int:
    # 连通性只做实测记录，不进 PASS 判定：本机代理 / TUN 随时可能切换，
    # 让回归套件依赖外网状态会变成假红。
    http = BinanceHttp(mode="direct", read_timeout=5)
    http.set_headers({"User-Agent": "epomc-net-selftest"})
    try:
        r = http.request("GET", "https://fapi.binance.com/fapi/v1/time",
                         attempts=2)
        detail = f"status={r.status_code} serverTime={r.json().get('serverTime')}"
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
    print(http.describe())
    print("强制直连 fapi:", detail)
    via = BinanceHttp(mode="proxy", read_timeout=5)
    try:
        vr = via.request("GET", "https://fapi.binance.com/fapi/v1/time",
                         attempts=1)
        via_detail = f"status={vr.status_code}"
    except Exception as e:
        via_detail = type(e).__name__
    print("走代理 fapi:", via_detail)
    # 自动探测结果只记录、不进 PASS：本机代理软件是否开着随时可能变。
    print("自动探测到的代理:", detect_proxy(refresh=True) or "未发现")

    # 不依赖真实网络：验证 auto 交替换通道，且断线时确实重试 3 次。
    # proxy_url="" 显式关掉自动探测与配置代理，保证测的是「无代理时 auto
    # 直连优先」这条分支；探测到代理时 auto 会变代理优先，那是另一条路径。
    auto = BinanceHttp(mode="auto", proxy_url="")
    picks = [auto._pick(i)[1] for i in range(3)]
    switched = picks == ["direct", "proxy", "direct"]

    class Broken:
        def request(self, *a, **k):
            raise requests.exceptions.ConnectionError("simulated drop")

    auto.direct = Broken()
    auto.proxied = Broken()
    try:
        auto.request("GET", "https://example.invalid/time", attempts=3)
        multi, retry_detail = False, "未触发重试"
    except requests.exceptions.ConnectionError as e:
        retry_detail = str(e)
        multi = ("重试 3 次" in retry_detail
                 and "direct:ConnectionError" in retry_detail
                 and "proxy:ConnectionError" in retry_detail)

    # www.binance.com 的 bapi 直连不通：取数侧必须代理优先，且能显式指定端口。
    pf = BinanceHttp(mode="proxy_first",
                     proxy_url="http://127.0.0.1:20122")
    pf_picks = [pf._pick(i)[1] for i in range(3)]
    pf_switched = pf_picks == ["proxy", "direct", "proxy"]
    explicit = (pf.proxied.proxies.get("https") == "http://127.0.0.1:20122"
                and not pf.proxied.trust_env)

    # 成功过的通道要被记住，后续请求不再每轮先撞一次不通的路。
    auto._preferred = "proxy"
    remembered = auto._pick(0)[1] == "proxy"
    auto._preferred = None

    ok = switched and multi and pf_switched and explicit and remembered
    print(f"auto通道={picks} proxy_first通道={pf_picks} "
          f"断线重试={'OK' if multi else 'FAIL'} "
          f"显式代理={'OK' if explicit else 'FAIL'} "
          f"通道记忆={'OK' if remembered else 'FAIL'}")
    print("net selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    print(BinanceHttp().describe())
