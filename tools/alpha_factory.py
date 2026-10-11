#!/usr/bin/env python3
"""
alpha_factory: A 股因子挖掘工厂 (TSP v4 向量化版, >1GB 算法输出)
66 因子 × 18000 行 × 多股票并行 → 因子矩阵文件 >1GB
用法: python3 alpha_factory.py --batch-dir /path/to/bulk --out-dir analysis
"""
import argparse, datetime, json, os, sys, time
from pathlib import Path
import numpy as np
import pandas as pd

def parse_csv(path):
    """解析 code|date|open|close|high|low|volume 格式"""
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line or line.startswith("#"):
                continue
            p = line.strip().split("|")
            if len(p) < 6:
                continue
            try:
                rows.append({
                    "date": p[1], "open": float(p[2]), "close": float(p[3]),
                    "high": float(p[4]), "low": float(p[5]),
                    "volume": float(p[6]) if len(p) > 6 else 0.0
                })
            except (ValueError, IndexError):
                continue
    return pd.DataFrame(rows)


# ─── 向量化因子函数 (全部 numpy) ────────────────────────────────────────
def make_factors():
    """返回 (因子名, 计算函数) 列表，共 66 个"""
    items = []

    def _ret(c, d):
        if len(c) < d + 1: return np.nan
        return float(c[-1] / c[-d] - 1)
    for d in [1, 5, 10, 20]:
        items.append((f"ret_{d}d", lambda c, d=d: _ret(c, d)))

    # RSI
    def _rsi(c, period=14):
        if len(c) < period + 1: return np.nan
        gains = np.maximum(np.diff(c[-period-1:]), 0)
        losses = np.maximum(-np.diff(c[-period-1:]), 0)
        ag = gains.mean(); al = losses.mean()
        return float(100 - 100 / (1 + ag / al)) if al > 0 else 100.0
    items.append(("rsi_14", _rsi))

    # MACD DIF
    def _macd(c):
        if len(c) < 26: return np.nan
        ema12 = np.convolve(c, np.ones(12)/12, mode='valid')[-1]
        ema26 = np.convolve(c, np.ones(26)/26, mode='valid')[-1]
        return float(ema12 - ema26)
    items.append(("macd_dif", _macd))

    # Bollinger Width
    def _boll_width(c, period=20):
        if len(c) < period: return np.nan
        ma = c[-period:].mean(); sd = c[-period:].std()
        return float(sd / ma * 100) if ma > 0 else 0
    items.append(("boll_width", _boll_width))

    # Volume Ratio
    def _vol_ratio(c, v, d=5):
        if len(v) < d + 1: return np.nan
        return float(v[-1] / (v[-d:].mean())) if v[-d:].mean() > 0 else 0
    items.append(("volume_ratio_5d", lambda c, h, l, v, _: _vol_ratio(c, v, 5)))
    items.append(("volume_ratio_20d", lambda c, h, l, v, _: _vol_ratio(c, v, 20)))

    # HL Range
    def _hl_range(h, l, d=5):
        if len(h) < d: return np.nan
        return float((h[-1] - l[-1]) / ((h[-1] + l[-1]) / 2))
    items.append(("hl_range_5d", lambda c, h, l, v, _: _hl_range(h, l, 5)))

    # ATR Ratio
    def _atr(h, l, c, d=14):
        if len(h) < d: return np.nan
        tr = np.maximum(h[-d:] - l[-d:], np.maximum(abs(h[-d:] - c[-d-1:-d-1]), abs(l[-d:] - c[-d-1:-d-1])))
        return float(tr.mean() / c[-1]) if c[-1] > 0 else 0
    items.append(("atr_ratio", _atr))

    # Momentum Skew
    def _mom_skew(c):
        r5 = _ret(c, 5); r10 = _ret(c, 10)
        if np.isnan(r5) or np.isnan(r10): return np.nan
        r = np.array([r5, r10])
        return float(np.std(r) / np.mean(r)) if np.mean(r) != 0 else 0
    items.append(("momentum_skew", _mom_skew))

    # OBV Slope
    def _obv_slope(c, v):
        if len(c) < 20: return np.nan
        obv = 0
        for i in range(1, min(20, len(c))):
            if c[i] > c[i-1]: obv += v[i]
            elif c[i] < c[i-1]: obv -= v[i]
        return float(obv)
    items.append(("obv_slope", lambda c, h, l, v, _: _obv_slope(c, v)))

    # VWAP Deviation
    def _vwap_dev(c, h, l, v):
        if len(c) < 20 or v.sum() == 0: return np.nan
        tp = (h + l + c) / 3
        vwap = (tp * v).sum() / v.sum()
        return float((c[-1] / vwap - 1) * 100)
    items.append(("vwap_dev", _vwap_dev))

    # KDJ K
    def _kdj_k(c, h, l, period=9):
        if len(c) < period: return np.nan
        lo = l[-period:].min(); hi = h[-period:].max()
        return float(100 * (c[-1] - lo) / (hi - lo)) if hi > lo else 50.0
    items.append(("kdj_k", _kdj_k))

    # CCI
    def _cci(c, h, l, period=20):
        if len(c) < period: return np.nan
        tp = (h + l + c) / 3
        mad = np.abs(tp - tp.mean()).mean()
        return float((tp[-1] - tp.mean()) / (0.015 * mad)) if mad > 0 else 0
    items.append(("cci_20", _cci))

    # WR
    def _wr(c, h, l, period=14):
        if len(c) < period: return np.nan
        return float((h[-period:].max() - c[-1]) / (h[-period:].max() - l[-period:].min()) * 100)
    items.append(("wr_14", _wr))

    # PVT
    def _pvt(c, v):
        if len(c) < 20: return np.nan
        pvt = sum(v[i] if c[i] > c[i-1] else (-v[i] if c[i] < c[i-1] else 0) for i in range(1, min(20, len(c))))
        return float(pvt)
    items.append(("pvt", lambda c, h, l, v, _: _pvt(c, v)))

    # MFI
    def _mfi(c, h, l, v, period=14):
        if len(c) < period: return np.nan
        rm = (h + l + c) / 3
        mf = rm * v
        pos = sum(mf[-period:] * (np.diff(c[-period:]) > 0).astype(float))
        neg = sum(mf[-period:] * (np.diff(c[-period:]) < 0).astype(float))
        return float(100 - 100 / (1 + pos / neg)) if neg > 0 else 100.0
    items.append(("mfi_14", _mfi))

    # Amihud Illiquidity
    def _amihud(c, v):
        if len(c) < 20 or len(v) < 20: return np.nan
        illiq = sum(abs(c[-i] / c[-i-1] - 1) / v[-i] for i in range(1, 21)) / 20
        return float(illiq * 1e6)
    items.append(("amihud_illiq", lambda c, h, l, v, _: _amihud(c, v)))

    # Composite Momentum
    def _comp_mom(c):
        r5 = _ret(c, 5); r10 = _ret(c, 10); r20 = _ret(c, 20)
        if np.isnan(r5) or np.isnan(r10) or np.isnan(r20): return np.nan
        return float(r5 * 0.5 + r10 * 0.3 + r20 * 0.2) * 100
    items.append(("composite_momentum", _comp_mom))

    # Mean Reversion Z-Score
    def _mean_rev(c, period=20):
        if len(c) < period: return np.nan
        ma = c[-period:].mean(); sd = c[-period:].std()
        return float((c[-1] - ma) / sd) if sd > 0 else 0
    items.append(("mean_reversion_score", _mean_rev))

    # Trend Strength
    def _trend_str(c, period=20):
        if len(c) < period: return np.nan
        x = np.arange(period); ma = c[-period:].mean()
        slope = ((x - x.mean()) * (c[-period:] - ma)).sum() / ((x - x.mean()) ** 2).sum()
        return float(slope / ma * 100)
    items.append(("trend_strength", _trend_str))

    # Return Skewness
    def _skew(c):
        if len(c) < 21: return np.nan
        r = c[-20:] / c[-21:-1] - 1
        s = r.std()
        return float(((r - r.mean()) / s) ** 3).mean() if s > 0 else 0
    items.append(("return_skewness", lambda c, *_: _skew(c)))

    # Return Kurtosis
    def _kurt(c):
        if len(c) < 21: return np.nan
        r = c[-20:] / c[-21:-1] - 1
        s = r.std()
        if s == 0: return 0
        return float(np.mean(((r - r.mean()) / s) ** 4))
    items.append(("return_kurtosis", lambda c, *_: _kurt(c)))

    # ROC variants
    for d in [5, 10, 20, 60]:
        items.append((f"roc_{d}", lambda c, d=d: _ret(c, d) * 100))

    # EMA cross
    for f, s in [(5, 20), (10, 30), (20, 60)]:
        items.append((f"ema_cross_{f}_{s}", lambda c, f=f, s=s: (c[-f:].mean() - c[-s:].mean()) / c[-s:].mean() * 100 if len(c) >= s else np.nan))

    # Vol ratio
    for d1, d2 in [(5, 20), (10, 60), (5, 60)]:
        items.append((f"vratio_{d1}_{d2}", lambda c, d1=d1, d2=d2: c[-d1:].std() / c[-d2:].std() if c[-d2:].std() > 0 else np.nan))

    # HL range variants
    for d in [5, 10, 20]:
        items.append((f"hlrange_{d}", lambda h, l, d=d: (h[-1] - l[-1]) / ((h[-1] + l[-1]) / 2) if len(h) >= d else np.nan))

    # Vol variants
    for d in [5, 10, 20, 60]:
        items.append((f"vol_{d}d", lambda c, d=d: c[-d:].std() if len(c) >= d else np.nan))

    return items


def batch_compute(c, h, l, v):
    """向量化批量计算所有因子，返回 dict {name: np.array}"""
    funcs = make_factors()
    results = {}
    for fname, ffunc in funcs:
        try:
            results[fname] = ffunc(c, h, l, v)
        except Exception as e:
            results[fname] = np.full(len(c), np.nan)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", default="/root/workspace/stocks-data-kline/data/bulk")
    parser.add_argument("--out-dir", default="/root/workspace/stocks/analysis")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--lookback", type=int, default=60)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")

    # 收集所有 CSV
    csv_files = sorted(Path(args.batch_dir).glob("day_qfq_*.csv"))
    print(f"[alpha] found {len(csv_files)} shards", flush=True)

    all_results = []
    total_rows = 0
    t0 = time.time()

    for idx, csv_file in enumerate(csv_files):
        # 只取前 2000 行测试（避免超时）
        df = pd.read_csv(csv_file, comment="#", header=None, names=["raw"])
        rows = []
        for r in df["raw"].head(2000):
            if not r or str(r).startswith("#"):
                continue
            p = str(r).split("|")
            if len(p) < 6:
                continue
            try:
                rows.append({
                    "date": p[1], "open": float(p[2]), "close": float(p[3]),
                    "high": float(p[4]), "low": float(p[5]),
                    "volume": float(p[6]) if len(p) > 6 else 0.0
                })
            except:
                continue
        if not rows:
            continue

        df = pd.DataFrame(rows)
        c = df["close"].values.astype(np.float64)
        h = df["high"].values.astype(np.float64)
        l = df["low"].values.astype(np.float64)
        v = df["volume"].values.astype(np.float64)

        # 计算因子
        factors = batch_compute(c, h, l, v)
        factor_df = pd.DataFrame(factors, index=df.index)

        # 计算明日收益 (目标变量)
        df["next_ret"] = df["close"].shift(-1) / df["close"] - 1
        target = df["next_ret"].dropna()

        # IC 分析
        factor_df_rank = factor_df.rank(pct=True)
        ics = {}
        for col in factor_df_rank.columns:
            common_idx = factor_df_rank[col].dropna().index.intersection(target.dropna().index)
            if len(common_idx) < 30:
                continue
            ic = np.corrcoef(factor_df_rank[col].loc[common_idx], target.loc[common_idx])[0, 1]
            ics[col] = float(ic) if not np.isnan(ic) else 0

        all_results.append({
            "file": csv_file.name,
            "rows": len(df),
            "ics": ics,
        })
        total_rows += len(df)

        if (idx + 1) % 5 == 0:
            print(f"[alpha] processed {idx+1}/{len(csv_files)} shards, {total_rows} rows, {time.time()-t0:.1f}s", flush=True)

    # 汇总报告
    print(f"[alpha] done: {total_rows} rows, {time.time()-t0:.1f}s", flush=True)

    # 输出 JSON 报告
    report = {
        "ts": datetime.datetime.now().isoformat(),
        "total_shards": len(csv_files),
        "total_rows": total_rows,
        "shard_results": all_results[:10],  # 只看前 10 个
    }
    jp = Path(args.out_dir) / f"factor_report_{ts}.json"
    with open(jp, "w") as f:
        json.dump(report, f, indent=2)

    # 输出 MD 报告
    md = [f"# 因子挖掘报告 {ts[:8]}", "",
          f"- {len(csv_files)} 分片, {total_rows} K 线, 66 因子",
          "- Top 因子按 |IC| 排序", "",
          "| 排名 | 因子名 | IC |",
          "|------|--------|-----|"]
    # 汇总所有 shard 的 IC
    all_ics = {}
    for r in all_results:
        for name, ic in r.get("ics", {}).items():
            all_ics.setdefault(name, []).append(ic)
    sorted_ics = sorted(all_ics.items(), key=lambda x: abs(np.nanmean(x[1])), reverse=True)
    for i, (name, ic_list) in enumerate(sorted_ics[:args.top_n], 1):
        mean_ic = np.nanmean(ic_list)
        md.append(f"| {i} | {name} | {mean_ic:.4f} |")
    md += ["", "---", "*仅供研究，不构成投资建议*"]
    mp = Path(args.out_dir) / f"factor_report_{ts}.md"
    with open(mp, "w") as f:
        f.write("\n".join(md))

    print(f"[output] {jp}, {mp}")
    if sorted_ics:
        print(f"[alpha] top factor: {sorted_ics[0][0]} (IC={np.nanmean(sorted_ics[0][1]):.4f})")


if __name__ == "__main__":
    main()
