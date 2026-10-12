#!/usr/bin/env python3
"""
复盘引擎 — postmortem.py
记录每日推荐，等 N 天后回填实际涨跌计算命中率

流程：
  1. 读 multi_screener_{date}.json（当日推荐结果）
  2. 对每个 BUY 股票，读取次日 K 线数据，计算涨跌
  3. 记录推荐 → 结果映射到 analysis/postmortem_{date}.json
  4. 定期生成命中率报告

用法：
  python3 postmortem.py [--days 3] [date_str]
  缺省 date_str = 3天前（自动回溯）
"""
import os, sys, json, glob
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POOL_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "pool")
ANA_DIR = os.path.join(ROOT, "..", "..", "stocks", "analysis")
KL_BULK = os.path.join(ROOT, "..", "..", "stocks-data-kline", "data", "bulk")


def load_kline(code):
    """Fast kline lookup from bulk cache."""
    for fn in sorted(os.listdir(KL_BULK)):
        if not fn.startswith(("day_qfq_", "day_raw_")) or not fn.endswith(".csv"):
            continue
        for line in open(os.path.join(KL_BULK, fn), encoding="utf-8"):
            if line.startswith("#") or "|" not in line:
                continue
            p = line.rstrip().split("|")
            if len(p) < 7 or p[0] != code:
                continue
            try:
                return {"date": p[1], "c": float(p[3]), "h": float(p[4]), "l": float(p[5])}
            except (ValueError, IndexError):
                continue
    return None


def get_price_on(date_str, code):
    """Get the close price of a stock on a specific date from K-line data."""
    bars = {}
    for fn in sorted(os.listdir(KL_BULK)):
        if not fn.startswith(("day_qfq_", "day_raw_")) or not fn.endswith(".csv"):
            continue
        for line in open(os.path.join(KL_BULK, fn), encoding="utf-8"):
            if line.startswith("#") or "|" not in line:
                continue
            p = line.rstrip().split("|")
            if len(p) < 7 or p[0] != code:
                continue
            try:
                bars[p[1]] = {"date": p[1], "c": float(p[3]), "h": float(p[4]), "l": float(p[5])}
            except (ValueError, IndexError):
                continue
    if not bars:
        return None
    # Find closest bar on or before the target date
    sorted_bars = sorted(bars.items())
    result = None
    for d, b in sorted_bars:
        if d <= date_str:
            result = b
        else:
            break
    return result


def run(postmortem_days=3, date_str=None):
    """Run postmortem for recommendations made `postmortem_days` days ago."""
    if date_str is None:
        date_str = (datetime.now(CST) - timedelta(days=postmortem_days)).strftime("%Y-%m-%d")
    ts = datetime.now(CST).strftime("%H%M")
    print(f"[postmortem] Running for date={date_str} (post-{postmortem_days}d)", flush=True)

    # Find the multi_screener JSON for this date
    pattern = os.path.join(POOL_DIR, f"{date_str}_multi_v5.json")
    if not os.path.exists(pattern):
        # Try v4
        pattern = os.path.join(POOL_DIR, f"{date_str}_multi_v4.json")
    if not os.path.exists(pattern):
        # Find any matching file
        files = sorted(glob.glob(os.path.join(POOL_DIR, f"*{date_str}*multi*.json")))
        if files:
            pattern = files[-1]
        else:
            print(f"[postmortem] No screener file found for {date_str}", file=sys.stderr)
            return None

    try:
        rec = json.load(open(pattern, encoding="utf-8"))
    except Exception as e:
        print(f"[postmortem] Failed to read {pattern}: {e}", file=sys.stderr)
        return None

    buy_list = rec.get("buy", [])
    if not buy_list:
        print(f"[postmortem] No BUY picks for {date_str}", flush=True)
        return None

    # Compute next-day returns from K-line data
    next_date = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    results = []
    hit_count = 0
    miss_count = 0
    unknown_count = 0

    for pick in buy_list[:30]:  # cap at 30
        code = pick.get("code", "")
        pre_bar = get_price_on(date_str, code)
        post_bar = get_price_on(next_date, code)

        if pre_bar and post_bar:
            ret = (post_bar["c"] / pre_bar["c"] - 1) * 100
            results.append({
                "code": code,
                "name": pick.get("name", ""),
                "score": pick.get("score", 0),
                "theme": pick.get("theme", ""),
                "signals": pick.get("signals", [])[:3],
                "pre_price": pre_bar["c"],
                "post_price": post_bar["c"],
                "return_1d": round(ret, 2),
                "hit": ret > 0,
            })
            if ret > 0:
                hit_count += 1
            else:
                miss_count += 1
        else:
            results.append({
                "code": code,
                "name": pick.get("name", ""),
                "score": pick.get("score", 0),
                "theme": pick.get("theme", ""),
                "signals": pick.get("signals", [])[:3],
                "pre_price": None,
                "post_price": None,
                "return_1d": None,
                "hit": None,
                "note": "no kline data",
            })
            unknown_count += 1

    # Sort by return descending
    results.sort(key=lambda x: -(x["return_1d"] or 0))

    output = {
        "date": date_str,
        "post_date": next_date,
        "ts": ts,
        "total_picks": len(buy_list),
        "evaluated": len([r for r in results if r["return_1d"] is not None]),
        "unknown": unknown_count,
        "hit_count": hit_count,
        "miss_count": miss_count,
        "hit_rate": round(hit_count / (hit_count + miss_count) * 100, 1) if (hit_count + miss_count) > 0 else None,
        "avg_return": round(sum(r["return_1d"] for r in results if r["return_1d"] is not None) / max(1, hit_count + miss_count), 2),
        "results": results,
    }

    os.makedirs(ANA_DIR, exist_ok=True)
    out_path = os.path.join(ANA_DIR, f"postmortem_{date_str}_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Also generate markdown report
    md_lines = [f"# 复盘报告 {date_str} → {next_date}", "",
                 f"- 推荐数: {len(buy_list)} | 已评估: {output['evaluated']} | 命中率: {output['hit_rate']}%",
                 f"- 平均收益: {output['avg_return']:+.2f}% | 上涨: {hit_count} | 下跌: {miss_count}", ""]

    md_lines.append("## 推荐明细（按次日收益排序）")
    md_lines.append("")
    md_lines.append("| 代码 | 名称 | 推荐分 | 主题 | 信号 | 前收 | 后收 | 次日涨跌 | 命中 |")
    md_lines.append("|------|------|--------|------|------|------|------|---------|------|")
    for r in results:
        ret_str = f"{r['return_1d']:+.2f}%" if r['return_1d'] is not None else "—"
        hit_str = "✅" if r['hit'] else ("❌" if r['return_1d'] is not None else "⏳")
        sig = "; ".join(r['signals'][:2])
        md_lines.append(f"| {r['code']} | {r['name'][:6]} | {r['score']:.0f} | {r['theme']} | {sig} | {r['pre_price'] or '—'} | {r['post_price'] or '—'} | {ret_str} | {hit_str} |")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("*仅供研究，不构成投资建议*")

    md_path = out_path.replace(".json", ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    print(f"[postmortem] Hit rate: {output['hit_rate']}% ({hit_count}/{hit_count+miss_count}) avg return: {output['avg_return']:+.2f}%", flush=True)
    print(f"[postmortem] -> {out_path}", flush=True)
    print(f"[postmortem] -> {md_path}", flush=True)

    return output


if __name__ == "__main__":
    days = 3
    date = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            days = int(args[i+1])
            i += 2
        elif args[i].startswith("--"):
            i += 1
        else:
            date = args[i]
            i += 1
    run(days, date)
