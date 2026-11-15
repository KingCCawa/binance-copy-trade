"""一键运行全部模块的自检与回归，输出真实结果，不编造任何结论。

  py -3.12 run_all.py            # 常规全套（约 20-40 秒）
  py -3.12 run_all.py --full     # 额外跑前端 JS 接口探测（较慢）
  py -3.12 run_all.py --json     # 机器可读汇总
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY312 = Path(r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe")

CHECKS = [
    ("解析层 App/网页双格式", ["feed.py"], 0),
    ("仓位重放账本", ["position_ledger.py", "--selftest"], 0),
    ("盈亏口径与曲线统计", ["pnl_tracker.py", "--selftest"], 0),
    # rc=2 = 币安 11012005 限流（外部服务状态），标 SKIP，不伪报 PASS 也不算 FAIL
    ("带单本接口取数", ["api_feed.py", "--rows", "10"], (0, 2)),
    ("事件引擎幽灵单 + 状态恢复 + 断线补开", ["copy_engine.py", "--selftest"], 0),
    ("网络层直连/代理兜底", ["net.py", "--selftest"], 0),
    ("账户连接自检", ["account.py", "--check", "--json"], None),
    ("状态持久化恢复", ["state_store.py", "--selftest"], 0),
    # rc=3 = 未安装 PySide6（可选依赖），标 SKIP 而不是 FAIL
    ("GUI 控件与全链路", ["gui_qt.py", "--selftest"], (0, 3)),
    ("主链路 dry-run", ["main.py"], 0),
]
FULL_ONLY = [
    # rc=2 = 缺可选依赖 playwright，探测未执行，标 SKIP 而不是假 PASS
    ("前端 JS 接口探测", ["probe_apis.py"], (0, 2)),
    # rc=2 = 带单本接口限流/不可用，回放未执行，标 SKIP 而不是假 PASS
    ("真实流水回放", ["_refs/_replay_check.py"], (0, 2)),
]


def pick_python() -> str:
    try:
        import requests          # noqa: F401
        import PySide6           # noqa: F401
        return sys.executable
    except ImportError:
        return str(PY312) if PY312.exists() else sys.executable


def run(py: str, cmd: list[str], timeout: int = 150) -> dict:
    started = time.monotonic()
    try:
        done = subprocess.run([py, str(ROOT / cmd[0]), *cmd[1:]], cwd=str(ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        return {
            "returncode": done.returncode,
            "elapsed_s": round(time.monotonic() - started, 2),
            "stdout": (done.stdout or "").strip().splitlines(),
            "stderr": (done.stderr or "").strip().splitlines(),
        }
    except subprocess.TimeoutExpired:
        return {"returncode": "TIMEOUT", "elapsed_s": timeout,
                "stdout": [], "stderr": ["超过 %ds 未结束" % timeout]}
    except Exception as e:
        return {"returncode": "ERROR", "elapsed_s": round(time.monotonic() - started, 2),
                "stdout": [], "stderr": [f"{type(e).__name__}: {e}"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    py = pick_python()
    checks = CHECKS + (FULL_ONLY if args.full else [])
    results = []
    for name, cmd, expect in checks:
        r = run(py, cmd)
        r["name"] = name
        r["cmd"] = " ".join(cmd)
        if expect is None:
            r["status"] = "PASS" if r["returncode"] in (0, 1) else "FAIL"
            r["note"] = "未配置凭证时返回 1 属预期" if r["returncode"] == 1 else ""
        elif isinstance(expect, tuple):
            if r["returncode"] not in expect:
                r["status"] = "FAIL"
                r["note"] = ""
            elif r["returncode"] == 2:
                r["status"] = "SKIP"
                r["note"] = ("缺少可选依赖 playwright，本项未执行"
                             if "probe_apis" in r["cmd"] else
                             "外部接口不可用或限流，本项未执行")
            else:
                r["status"] = "PASS"
                r["note"] = ""
        else:
            r["status"] = "PASS" if r["returncode"] == expect else "FAIL"
            r["note"] = ""
        results.append(r)

    if args.json:
        print(json.dumps({"python": py, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        print(f"python = {py}")
        print(f"checks = {len(results)}  pass = "
              f"{sum(1 for r in results if r['status'] == 'PASS')}  "
              f"skip = {sum(1 for r in results if r['status'] == 'SKIP')}\n")
        for r in results:
            print(f"[{r['status']}] {r['name']:24s} rc={r['returncode']} "
                  f"{r['elapsed_s']}s  {r['cmd']}")
            for line in r["stdout"][:3]:
                print(f"       out | {line[:150]}")
            for line in r["stderr"][:2]:
                print(f"       err | {line[:150]}")
            if r["note"]:
                print(f"       note| {r['note']}")
    return 0 if all(r["status"] in ("PASS", "SKIP") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
