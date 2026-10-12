#!/usr/bin/env python3
"""
全量扫描入口 — 依次运行所有池子 + 热点推演 + 多轮审核，输出统一报告。

用法：
  python3 tools/run_all_pools.py [date_str]
  缺省 date_str = 当天 (YYYY-MM-DD)

流程：
  1. pool_cycle.py    → 周期池（宏观定位 + 个股相位）
  2. pool_sector.py   → 板块池（动量 + 资金流 + 轮动）
  3. pool_lowpos.py   → 低位池（未涨停 + 低位启动 + 明日上涨概率）
  4. hotspot_infer.py → 热点推演（热点主题 → 同行业未涨停票 → 打分排序）
  5. multi_screener_v4.py → 综合五轮审核（以上四池 + 热点 → 综合分）
"""
import os, sys, subprocess, time
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")

STEPS = [
    ("周期池",     os.path.join(TOOLS, "pool_cycle.py")),
    ("板块池",     os.path.join(TOOLS, "pool_sector.py")),
    ("低位池",     os.path.join(TOOLS, "pool_lowpos.py")),
    ("热点推演",   os.path.join(TOOLS, "hotspot_infer.py")),
    ("综合扫描",   os.path.join(TOOLS, "multi_screener_v4.py")),
]


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now(CST).strftime("%Y-%m-%d")
    print(f"=== run_all_pools {date_str} ===", flush=True)
    
    for name, script in STEPS:
        t0 = time.time()
        print(f"\n[{name}] Running {os.path.basename(script)} ...", flush=True)
        r = subprocess.run([sys.executable, script, date_str],
                          capture_output=True, text=True, timeout=300)
        elapsed = time.time() - t0
        if r.stdout:
            for line in r.stdout.strip().split("\n")[-5:]:
                print(f"  {line}")
        if r.returncode != 0:
            print(f"  [ERROR] rc={r.returncode} stderr={r.stderr.strip()[:200]}", flush=True)
        else:
            print(f"  [OK] {elapsed:.1f}s", flush=True)
    
    # Print combined summary
    POOL_DIR = os.path.abspath(os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "pool"))
    print(f"\n=== 全部完成 {date_str} ===")
    import json
    for prefix, label in [("cycle", "周期池"), ("sector_pool", "板块池"),
                          ("lowpool", "低位池"), ("hotspot_infer", "热点推演"),
                          ("_multi_v4", "综合扫描")]:
        # Try both date formats
        for fmt in [date_str, date_str.replace("-", "")]:
            path = os.path.join(POOL_DIR, f"{fmt}.{prefix}.json") if prefix != "_multi_v4" \
                   else os.path.join(POOL_DIR, f"{fmt}_multi_v4.json")
            if os.path.exists(path):
                try:
                    d = json.load(open(path))
                    keys = {k: v for k, v in d.items() if isinstance(v, (int, float, str))}
                    keys = {k: v for k, v in keys.items() if not k.startswith("_")}
                    print(f"  {label}: {keys}")
                except Exception:
                    print(f"  {label}: file exists ({os.path.basename(path)})")
                break
        else:
            print(f"  {label}: not found")


if __name__ == "__main__":
    main()
