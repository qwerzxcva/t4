#!/usr/bin/env python3
"""
self_improve: 复盘结果 → 权重自校准 (反思闭环第二环)

读入:
  - analysis/reflect_*.json (最近 14 天)      — 信号命中率
  - data/weights.json (可选)                  — 当前多轮审核权重
输出:
  - data/weights.json (更新)                  — multi_screener 下一轮加载
  - analysis/self_improve_latest.md           — 调整报告

校准规则 (保守, 避免过拟合):
  1. 信号命中: 对某信号近 14 天命中率 hit_rate
     hit_rate > 0.60 → 该信号对应维度权重 +0.02 (上限 +0.06)
     hit_rate < 0.40 → 该信号对应维度权重 -0.02 (下限 -0.06)
     评估样本 < 10 条 → 不调 (样本不足)
  2. 信号 → 维度映射:
     动量延续   → r5_momentum (0.15)
     追高警惕   → pos20       (0.10)
     超跌修复   → ma_dev      (0.20)
     横盘蓄势   → rsi         (0.25)
  3. 全部调整后重新归一化到总和 = 1.0
  4. 单轮调整幅度限制 |Δw| <= 0.06 (防震荡)

multi_screener.py 在轮次 2 会自动读取 weights.json (存在时覆盖默认权重)。
用法: python3 self_improve.py [--reflect-dir analysis] [--weights data/weights.json] [--days 14]
"""
import argparse, datetime, json, os, re, sys
from pathlib import Path

DEFAULT_WEIGHTS = {
    "rsi": 0.25, "ma_dev": 0.20, "r5_momentum": 0.15, "lianban": 0.15, "vol": 0.15, "pos20": 0.10,
}
SIG2DIM = {"动量延续": "r5_momentum", "追高警惕": "pos20", "超跌修复": "ma_dev", "横盘蓄势": "rsi"}
ADJ_STEP = 0.02
ADJ_CAP = 0.06
MIN_SAMPLES = 10


def load_reflects(reflect_dir, days):
    p = Path(reflect_dir)
    files = sorted(p.glob("reflect_*.json"))
    out = []
    if not files:
        return out
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y%m%d")
    for f in files:
        m = re.search(r"reflect_(\d{8}_\d{4})", f.name)
        if m and m.group(1)[:8] >= cutoff:
            try:
                out.append(json.load(open(f, encoding="utf-8")))
            except Exception:
                pass
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reflect-dir", default="analysis")
    parser.add_argument("--weights", default="data/weights.json")
    parser.add_argument("--md-out", default="analysis/self_improve_latest.md")
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()

    reflects = load_reflects(args.reflect_dir, args.days)
    # 汇总: 信号 → {total, hit, miss}
    per_sig = {}
    for r in reflects:
        for item in r.get("results", []):
            s = item.get("signal")
            ok = item.get("ok")
            if not s or ok is None:
                continue
            e = per_sig.setdefault(s, {"total": 0, "hit": 0})
            e["total"] += 1
            if ok:
                e["hit"] += 1
    weights = dict(DEFAULT_WEIGHTS)
    if os.path.exists(args.weights):
        try:
            saved = json.load(open(args.weights, encoding="utf-8"))
            weights.update({k: saved[k] for k in saved if k in DEFAULT_WEIGHTS})
        except Exception:
            pass

    deltas = {}
    for sig, e in per_sig.items():
        dim = SIG2DIM.get(sig)
        if not dim or e["total"] < MIN_SAMPLES:
            continue
        hit_rate = e["hit"] / e["total"]
        if hit_rate > 0.60:
            deltas[dim] = min(ADJ_CAP, deltas.get(dim, 0) + ADJ_STEP)
        elif hit_rate < 0.40:
            deltas[dim] = max(-ADJ_CAP, deltas.get(dim, 0) - ADJ_STEP)

    for dim, d in deltas.items():
        weights[dim] = round(weights.get(dim, DEFAULT_WEIGHTS.get(dim, 0.1)) + d, 4)
    # 归一化
    total = sum(weights.values())
    if total > 0:
        weights = {k: round(v / total, 4) for k, v in weights.items()}

    out = {"ts": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
           "weights": weights, "deltas": deltas,
           "signal_stats": {k: {**v, "hit_rate": round(v["hit"] / v["total"], 3)} for k, v in per_sig.items()},
           "meta": {"rule": "hit>60% +0.02 / hit<40% -0.02 / cap 0.06 / norm=1.0 / min_samples 10"}}
    os.makedirs(os.path.dirname(args.weights) or ".", exist_ok=True)
    with open(args.weights, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # MD 报告
    md = ["# 自校准报告", "",
          f"- 复盘文件 {len(reflects)} 份 · 信号 {len(per_sig)} 类 · 权重调整 {len(deltas)} 项", ""]
    md += ["| 信号 | 样本 | 命中 | 命中率 | 对应维度 | Δ权重 |", "|------|------|------|--------|----------|--------|"]
    for sig, e in sorted(per_sig.items(), key=lambda kv: -kv[1]["hit"] / max(1, kv[1]["total"])):
        dim = SIG2DIM.get(sig, "—")
        hr = e["hit"] / e["total"]
        mark = ""
        if e["total"] < MIN_SAMPLES:
            mark = " (样本不足, 不调)"
        md.append(f"| {sig} | {e['total']} | {e['hit']} | {hr:.0%}{mark} | {dim} | {deltas.get(dim, 0.0):+.2f} |")
    md += ["", "## 当前权重", ""]
    for k, v in weights.items():
        md.append(f"- {k}: {v:.4f}")
    md += ["", f"*生成 {out['ts'][:19]} · 下一轮 multi_screener 将自动加载 {args.weights}*"]
    os.makedirs(os.path.dirname(args.md_out) or ".", exist_ok=True)
    with open(args.md_out, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"[self-improve] weights={ {k: round(v,3) for k,v in weights.items()} } deltas={deltas} → {args.weights}", flush=True)
    print(f"[output] {args.md_out}", flush=True)


if __name__ == "__main__":
    main()
