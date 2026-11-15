"""从币安跟单页前端 JS 里提取所有 copy-trade / lead-portfolio / lead-data 接口路径。

用于定位「最新操作记录」tab 背后的真实端点，方便后续改成直连而不是渲染 DOM。
运行： python probe_apis.py
"""
from __future__ import annotations

import re
import sys

import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HOME = "https://www.binance.com/zh-CN/copy-trading"


def main() -> int:
    if sync_playwright is None:
        print("未执行：缺少可选依赖 playwright；接口已固化在 api_feed.py，"
              "运行时不需要它")
        return 2
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        page = browser.new_context(locale="zh-CN", user_agent=UA).new_page()
        page.goto(HOME, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(4000)
        srcs = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
        browser.close()

    print(f"scripts={len(srcs)}", file=sys.stderr)
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    pattern = re.compile(
        r"[\"']([^\"']{0,180}(?:lead-portfolio|lead-data|copy-trade)[^\"']{0,180})[\"']")
    paths: set[str] = set()
    for url in srcs:
        if "bnbstatic" not in url and "binance.com" not in url:
            continue
        try:
            resp = sess.get(url, timeout=15)
            if resp.status_code != 200:
                continue
            for match in pattern.findall(resp.text):
                if "/" in match and len(match) < 220:
                    paths.add(match)
        except requests.RequestException:
            continue
    for path in sorted(paths):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
