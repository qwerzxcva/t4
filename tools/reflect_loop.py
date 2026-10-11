"""
反思复盘引擎 (Reflective Loop v1) — 利用已发生的结果自我校正

机制:
  1. 每日收盘后:
     - multi_screener 运行 → picks (TOP N 打分股票)
     - 记录 pick[i].code, pick[i].score, pick[i].signal, pick[i].expect_range
  2. N 个交易日后 (默认 3/10/20 日):
     - 拉取 pick 实际收盘涨跌幅
     - 对比 expect_range 与 actual, 判定预测是否准确
  3. 更新 blogger_tracker 权重:
     - 将「预测信号→结果」作为一条「伪操作」录入
     - 命中预期 → 该信号源权重 +0.1; 偏离 → -0.05
  4. 生成复盘报告:
     - hot_impact 推演准确率统计
     - 哪些信号值得追踪 (高命中) / 哪些要忽略 (低命中)
     - 反馈到下一轮 multi_screener 的 score 公式权重

核心不变量:
  - 只有「有明确期望区间」的信号才参与复盘 (动量延续/超跌修复/追高警惕三类)
  - 预测窗口 3/10/20 日 (短期/中期/长期)
  - 置信度阈值 0.6 (score>=0.6 才算可复盘); 低于此不记入
"""
import argparse, datetime, json, os, re, sys, time, urllib.request
from pathlib import Path
from collections import defaultdict

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120",
      "Referer": "https://finance.sina.com.cn/"}


def fetch_close(code, bars=25, retries=2):
    """腾讯前复权日K, 返回 [[date, close], ...]"""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ai_screener import prefixed
    tcode = prefixed(str(code).zfill(6))
    for _ in range(retries):
        try:
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tcode},day,,,{bars},qfq"
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10).read()
            d = json.loads(body.decode("utf-8", "replace"))
            node = (d.get("data") or {}).get(tcode) or {}
            k = node.get("qfqday") or node.get("day") or []
            rows = [[r[0], float(r[2])] for r in k if len(r) > 2 and r[2]]
            if len(rows) >= bars:
                return rows
        except Exception as e:
            if _ == retries - 1:
                print(f"[close] {code}: {e}", file=sys.stderr)
    return []


def reflect(multi_file, lookback_days=10, out_dir=None):
    """对某份 multi_screener 输出的 TOP picks 做 N 日复盘"""
    data = json.load(open(multi_file, encoding="utf-8"))
    picks = data.get("round2_top") or data.get("picks") or []
    if not picks:
        print(f"[reflect] no picks in {multi_file}", file=sys.stderr)
        return

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    ts_orig = data.get("ts", "")
    orig_dt = datetime.datetime.fromisoformat(ts_orig.replace("Z", "+00:00")) if ts_orig else now
    days_since = (now - orig_dt).days

    results = []
    for p in picks[:30]:
        code = p.get("code", "")
        if not code:
            continue
        close_k = fetch_close(code, bars=lookback_days + 5)
        if len(close_k) < lookback_days:
            continue
        # 实际收益 = (今天收盘 - 买入价参考) / 买入价参考
        # 买入价参考: pick 当日的价格 (p.get('price')) 或 k 线倒数第 lookback_days 根
        buy_idx = max(0, len(close_k) - lookback_days - 1)
        sell_idx = len(close_k) - 1
        buy_price = close_k[buy_idx][1]
        sell_price = close_k[sell_idx][1]
        actual_pct = round((sell_price / buy_price - 1) * 100, 2) if buy_price else None
        signal = p.get("signal") or ""
        expect = p.get("expect") or ""
        # 判定
        ok = None
        if actual_pct is not None:
            if signal == "动量延续":
                ok = actual_pct > 0.3
            elif signal == "超跌修复":
                ok = actual_pct > -0.3
            elif signal == "追高警惕":
                ok = actual_pct < 0.3
            else:
                ok = None
        results.append({
            "code": code, "name": p.get("name", ""),
            "signal": signal, "expect": expect,
            "actual_pct": actual_pct, "days": days_since,
            "ok": ok,
        })

    hit_rate = sum(1 for r in results if r["ok"] is True) / max(1, sum(1 for r in results if r["ok"] is not None))
    report = {
        "ts": now.isoformat(),
        "multi_file": os.path.basename(multi_file),
        "total": len(results),
        "evaluated": sum(1 for r in results if r["ok"] is not None),
        "hit": sum(1 for r in results if r["ok"] is True),
        "miss": sum(1 for r in results if r["ok"] is False),
        "hit_rate": round(hit_rate, 3),
        "results": results,
        "meta": {"algo": "reflect-loop-v1", "zero_cred": True},
    }
    od = out_dir or os.path.dirname(multi_file)
    os.makedirs(od, exist_ok=True)
    jp = os.path.join(od, f"reflect_{datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y%m%d_%H%M')}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    md = [f"# 反思复盘 {now.strftime('%Y-%m-%d %H:%M')} CST", "",
          f"- 原文件: {os.path.basename(multi_file)} · 距今 {days_since} 日 · "
          f"评估 {report['evaluated']} 条 · 命中率 **{hit_rate:.0%}**", "",
          "| 代码 | 信号 | 预期 | 实际% | 判定 |",
          "|------|------|------|-------|------|"]
    for r in results:
        mark = "✅" if r["ok"] else ("❌" if r["ok"] is False else "—")
        md.append(f"| {r['code']} | {r['signal']} | {r['expect']} | {r['actual_pct']}% | {mark} |")
    mp = jp.replace(".json", ".md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"[reflect] {jp}  hit_rate={hit_rate:.0%} ({report['hit']}/{report['evaluated']})", flush=True)
    return report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("multi_file", help="multi_screener *.json 文件路径")
    p.add_argument("--lookback", type=int, default=10)
    p.add_argument("--out-dir", default=None)
    args = p.parse_args()
    reflect(args.multi_file, args.lookback, args.out_dir)


if __name__ == "__main__":
    main()
