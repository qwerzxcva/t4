#!/usr/bin/env python3
"""
周期池 — Cycle Pool
基于 K线数据 + 实时行情，输出三类信号：
1. 宏观周期定位（牛市/熊市/震荡/复苏）——指数级别
2. 个股周期相位（启动/加速/见顶/回落/筑底）——股票级别
3. 轮动窗口（什么阶段该买什么板块）

核心算法：
- 周期定位：利用均线排列 + MACD + 波动率 + 量能四因子判定当前市场处于牛/熊/震荡的哪个阶段
- 个股相位：利用 RPS（相对强度）+ 价格位置（距高点/低点百分比）+ 趋势斜率 综合判定
- 轮动窗口：统计历史上各周期阶段的领涨板块，结合当前热点映射到候选板块

输入：
- data/market/all_YYYY-MM-DD.txt（全市场快照）
- data/kline/history/*.txt（日K线历史）
- docs/cycle.md（周期规律，人工维护）

输出：
- data/pool/{date}.cycle.json
- analysis/cycle_report_{date}.md
"""
import os, sys, json, time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

ROOT = os.environ.get("STOCKS_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KL_DIR = os.environ.get("STOCKS_KLINE_DIR") or os.path.join(ROOT, "..", "..", "stocks-data-kline", "data")
KL_DIR = os.path.abspath(KL_DIR)
KL_BULK = os.path.join(KL_DIR, "bulk")
KL_HISTORY = os.path.join(KL_DIR, "index", "history")
SNAPSHOT_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "market")
POOL_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "pool")
ANA_DIR = os.path.join(ROOT, "..", "..", "stocks", "analysis")

INDEX_CODES = {
    "sh000001": ("上证综指", 0),
    "sz399001": ("深证成指", 1),
    "sh000300": ("沪深300", 2),
    "sz399006": ("创业板指", 3),
    "sh000688": ("科创50", 4),
}

# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_snapshot(date_str):
    """Load latest market snapshot. Returns {code: dict}."""
    if not os.path.exists(SNAPSHOT_DIR):
        return {}
    latest = None
    for f in os.listdir(SNAPSHOT_DIR):
        if f.startswith("all_") and f.endswith(".txt"):
            if latest is None or f > latest:
                latest = f
    if not latest:
        return {}
    rows = {}
    for line in open(os.path.join(SNAPSHOT_DIR, latest), encoding="utf-8"):
        if line.startswith("#") or "|" not in line:
            continue
        p = line.rstrip().split("|")
        if len(p) < 16:
            continue
        try:
            rows[p[0]] = {
                "code": p[0], "name": p[1],
                "price": float(p[2]),
                "chg_pct": float(p[3]),
                "amount": float(p[4]),
                "turnover": float(p[5]),
                "pe": float(p[6]) or 0,
                "vol_ratio": float(p[7]),
                "mktcap": float(p[8]),
                "pb": float(p[10]) or 0,
                "main_net": float(p[11]),
                "main_ratio": float(p[12]),
                "industry": p[15].strip(),
            }
        except (ValueError, IndexError):
            continue
    return rows


def load_kline(code):
    """Load daily kline from bulk shards (primary) and history dir (fallback)."""
    bars = []
    if os.path.isdir(KL_BULK):
        for fn in sorted(os.listdir(KL_BULK)):
            if not fn.endswith(".csv"):
                continue
            for line in open(os.path.join(KL_BULK, fn), encoding="utf-8"):
                if line.startswith("#") or "|" not in line:
                    continue
                p = line.rstrip().split("|")
                if len(p) < 7 or p[0] != code:
                    continue
                try:
                    bars.append({
                        "date": p[1], "o": float(p[2]), "c": float(p[3]),
                        "h": float(p[4]), "l": float(p[5]), "v": int(float(p[6])),
                    })
                except (ValueError, IndexError):
                    continue
    if not bars and os.path.isdir(KL_HISTORY):
        for fn in sorted(os.listdir(KL_HISTORY)):
            if not fn.startswith(code + ".") or not fn.endswith(".txt"):
                continue
            for line in open(os.path.join(KL_HISTORY, fn), encoding="utf-8"):
                if line.startswith("#") or "|" not in line:
                    continue
                p = line.rstrip().split("|")
                if len(p) < 7 or p[0] != code:
                    continue
                try:
                    bars.append({
                        "date": p[1], "o": float(p[2]), "c": float(p[3]),
                        "h": float(p[4]), "l": float(p[5]), "v": int(float(p[6])),
                    })
                except (ValueError, IndexError):
                    continue
    bars.sort(key=lambda x: x["date"])
    return bars


def load_all_klines(codes, max_per_code=250):
    """Batch-load klines for multiple codes."""
    cache = {}
    for c in codes:
        cache[c] = load_kline(c)[:max_per_code]
    return cache


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

def ma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n

def ema(arr, n):
    if len(arr) < n:
        return None
    k = 2.0 / (n + 1)
    v = arr[0]
    for x in arr[1:]:
        v = v * (1 - k) + x * k
    return v

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = [max(closes[i] - closes[i-1], 0) for i in range(len(closes)-period, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(len(closes)-period, len(closes))]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))

def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    dif_list = []
    for i in range(len(closes)):
        sub = closes[max(0, i-slow+1):i+1]
        if len(sub) < fast:
            dif_list.append(0.0)
            continue
        e12 = ema(sub, fast)
        e26 = ema(sub, slow)
        dif_list.append((e12 - e26) if e12 and e26 else 0.0)
    dif = dif_list[-1]
    dea = ema(dif_list[-signal:], signal) if len(dif_list) >= signal else dif
    hist = (dif - dea) * 2
    return dif, dea, hist

def std_dev(vals):
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return (sum((x - m)**2 for x in vals) / len(vals)) ** 0.5

def vol_ratio_series(vols, period=20):
    if len(vols) < period:
        return 1.0
    recent = vols[-1]
    avg = sum(vols[-period:]) / period
    return recent / avg if avg > 0 else 1.0

# ---------------------------------------------------------------------------
# Cycle algorithms
# ---------------------------------------------------------------------------

def macro_cycle_phase(kline_map):
    """
    Determine the current market cycle phase using 4 indicators:
    1. MA alignment (MA5>MA10>MA20>MA30 = bull)
    2. MACD position (above/below zero)
    3. Volatility regime (high vol = trend intensifying, low vol = consolidation)
    4. Volume trend (increasing volume = bull confirmation)
    
    Returns dict: {code: {phase, score, details}}
    """
    result = {}
    for code, bars in kline_map.items():
        if len(bars) < 60:
            continue
        closes = [b["c"] for b in bars]
        vols = [b["v"] for b in bars]
        
        # MA alignment
        m5 = ma(closes, 5)
        m10 = ma(closes, 10)
        m20 = ma(closes, 20)
        m30 = ma(closes, 30)
        ma_bull = (m5 and m10 and m20 and m30) and m5 > m10 > m20 > m30
        ma_bear = (m5 and m10 and m20 and m30) and m5 < m10 < m20 < m30
        
        # MACD
        dif, dea, hist = macd(closes)
        macd_bull = hist is not None and hist > 0
        macd_bear = hist is not None and hist < -0.05
        
        # Volatility
        recent_vol = std_dev(closes[-20:]) / (sum(closes[-20:])/20) if len(closes) >= 20 else 0
        prev_vol = std_dev(closes[-40:-20]) / (sum(closes[-40:-20])/20) if len(closes) >= 40 else 0
        vol_regime = "high" if recent_vol > prev_vol * 1.3 else ("low" if recent_vol < prev_vol * 0.7 else "normal")
        
        # Volume trend
        vr = vol_ratio_series(vols)
        
        # Composite score (-10 to +10)
        score = 0
        if ma_bull: score += 3
        elif ma_bear: score -= 3
        if macd_bull: score += 2
        elif macd_bear: score -= 2
        if vr > 1.5: score += 1
        elif vr < 0.6: score -= 1
        if vol_regime == "low": score += 1  # consolidation = potential setup
        elif vol_regime == "high" and not ma_bull: score -= 1
        
        # Phase determination
        if score >= 4:
            phase = "acceleration"  # 加速期
        elif score >= 1:
            phase = "recovery"  # 复苏期
        elif score <= -4:
            phase = "decline"  # 衰退期
        elif score <= -1:
            phase = "recession"  # 萧条期
        else:
            phase = "consolidation"  # 震荡期
        
        result[code] = {
            "phase": phase,
            "score": round(score, 1),
            "ma_bull": ma_bull,
            "macd_bull": macd_bull,
            "vr": round(vr, 2),
            "vol_regime": vol_regime,
            "rsi": round(rsi(closes), 1),
        }
    return result


def stock_cycle_phase(code, bars, snap=None):
    """
    Determine individual stock cycle phase:
    - early_accumulation (底部吸筹)
    - breakout (启动突破)
    - acceleration (加速上涨)
    - top_distribution (顶部派发)
    - correction (回调)
    - bottoming (筑底)
    
    Uses: price position (percentile), trend slope, RPS, volume pattern
    """
    if len(bars) < 60:
        return None
    
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    
    current = closes[-1]
    high_52 = max(highs[-250:]) if len(highs) >= 250 else max(highs)
    low_52 = min(lows[-250:]) if len(lows) >= 250 else min(lows)
    price_pct = (current - low_52) / (high_52 - low_52) * 100 if high_52 != low_52 else 50
    
    # Trend slope (linear regression on last 20 days)
    n = min(20, len(closes))
    xs = list(range(n))
    ys = closes[-n:]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = sum((x - x_mean)**2 for x in xs)
    slope = numerator / denominator if denominator else 0
    slope_norm = slope / y_mean if y_mean else 0  # normalized slope
    
    # RPS (relative price strength over various periods)
    def rps(period):
        if len(closes) < period + 1:
            return 50.0
        ret = (closes[-1] / closes[-period-1] - 1) * 100
        return max(0, min(100, 50 + ret * 5))  # rough percentile
    
    rps5 = rps(5)
    rps20 = rps(20)
    rps60 = rps(60)
    
    # Volume pattern
    vr = vol_ratio_series(vols)
    vol_trend = "expanding" if vr > 1.5 else ("contracting" if vr < 0.7 else "normal")
    
    # RSI
    rsi_val = rsi(closes)
    
    # MACD
    _, _, hist = macd(closes)
    
    # Phase scoring
    score = 0
    phase = "unknown"
    
    # Early accumulation: low price position, contracting volume, flat/slight uptrend
    if price_pct < 25 and vol_trend == "contracting" and slope_norm > -0.001:
        score = 5
        phase = "early_accumulation"
    
    # Breakout: price crossing above key MAs, expanding volume
    elif price_pct > 30 and price_pct < 55 and vr > 1.3 and slope_norm > 0.002:
        score = 8
        phase = "breakout"
    
    # Acceleration: strong uptrend, high RPS, expanding volume
    elif price_pct > 55 and rps20 > 70 and vr > 1.2 and slope_norm > 0.003:
        score = 10
        phase = "acceleration"
    
    # Top distribution: very high price, high volatility, weakening volume
    elif price_pct > 85 and vr < 1.0 and rsi_val > 75:
        score = -8
        phase = "top_distribution"
    
    # Correction: from high, now dropping
    elif price_pct > 60 and slope_norm < -0.002 and rsi_val < 50:
        score = -3
        phase = "correction"
    
    # Bottoming: at low, volume picking up, RSI oversold recovering
    elif price_pct < 30 and vr > 1.0 and rsi_val > 35 and rsi_val < 55:
        score = 6
        phase = "bottoming"
    
    else:
        # Generic scoring
        if slope_norm > 0.003: score += 3
        if rps20 > 70: score += 2
        if vr > 1.2: score += 2
        if price_pct < 30: score += 1
        elif price_pct > 80: score -= 3
        if rsi_val > 75: score -= 2
        elif rsi_val < 25: score += 1
        if hist and hist > 0: score += 1
    
    if score >= 8:
        phase = "acceleration"
    elif score >= 5:
        phase = "breakout"
    elif score >= 2:
        phase = "early_accumulation"
    elif score <= -5:
        phase = "top_distribution"
    elif score <= -2:
        phase = "correction"
    elif score >= -1:
        phase = "bottoming"
    
    return {
        "phase": phase,
        "score": round(score, 1),
        "price_pct": round(price_pct, 1),
        "slope_norm": round(slope_norm * 1000, 3),
        "rps5": round(rps5, 1),
        "rps20": round(rps20, 1),
        "rps60": round(rps60, 1),
        "vr": round(vr, 2),
        "vol_trend": vol_trend,
        "rsi": round(rsi_val, 1),
        "macd_hist": round(hist, 3) if hist else None,
        "ma5": round(ma(closes, 5), 2),
        "ma20": round(ma(closes, 20), 2),
        "high_52": round(high_52, 2),
        "low_52": round(low_52, 2),
    }


# Sector rotation scoring
def sector_cycle_score(sector_data, market_phase):
    """
    Given a sector's recent performance and the overall market phase,
    determine if this sector is in a favorable rotation window.
    
    Returns dict with sector, score, and rotation_window.
    """
    scores = {}
    for sector, stats in sector_data.items():
        score = 0
        window = []
        
        # Sector relative strength vs market
        if stats.get("avg_chg_5d", 0) > stats.get("market_avg_chg_5d", 0):
            score += 2
            window.append("outperforming")
        
        # Sector in correct phase for market cycle
        if market_phase in ("recovery", "acceleration"):
            if stats.get("avg_chg_20d", 0) > 5:
                score += 3
                window.append("leading")
            elif stats.get("avg_chg_20d", 0) > 0:
                score += 1
                window.append("following")
        elif market_phase in ("decline", "recession"):
            if stats.get("defensive", False):
                score += 2
                window.append("defensive")
            else:
                score -= 2
        
        # Volume confirmation
        if stats.get("vol_ratio", 1) > 1.2:
            score += 1
            window.append("volume_confirmed")
        
        # Recent pullback = buying opportunity
        if stats.get("pullback_5d", False):
            score += 2
            window.append("pullback_buy")
        
        scores[sector] = {
            "score": score,
            "window": window,
            "avg_chg_5d": stats.get("avg_chg_5d", 0),
            "avg_chg_20d": stats.get("avg_chg_20d", 0),
            "vol_ratio": stats.get("vol_ratio", 1),
        }
    return scores


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(date_str=None):
    date_str = date_str or datetime.now(CST).strftime("%Y-%m-%d")
    print(f"[cycle] Running for {date_str}", flush=True)
    
    # 1. Load snapshot
    snap = load_snapshot(date_str)
    print(f"[cycle] Snapshot: {len(snap)} stocks loaded", flush=True)
    
    # 2. Compute macro cycle
    macro_bars = {}
    for code in INDEX_CODES:
        macro_bars[code] = load_kline(code)
    macro_phase = macro_cycle_phase(macro_bars)
    print(f"[cycle] Macro phases:", flush=True)
    for code, info in macro_phase.items():
        name = INDEX_CODES.get(code, (code,))[0]
        print(f"  {name}: {info['phase']} (score={info['score']})", flush=True)
    
    # 3. Determine dominant market phase
    phases = [v["phase"] for v in macro_phase.values()]
    from collections import Counter
    dominant = Counter(phases).most_common(1)[0][0] if phases else "consolidation"
    
    # 4. Compute sector-level data
    sector_map = defaultdict(lambda: {"chgs_5d": [], "chgs_20d": [], "vrs": [], "codes": []})
    for code, s in snap.items():
        ind = s.get("industry", "")
        if ind:
            sector_map[ind]["chgs_5d"].append(s.get("chg_pct", 0))
            sector_map[ind]["vrs"].append(s.get("vol_ratio", 1))
            sector_map[ind]["codes"].append(code)
    
    market_avg_5d = sum(s.get("chg_pct", 0) for s in snap.values()) / len(snap) if snap else 0
    sector_stats = {}
    for sector, data in sector_map.items():
        if len(data["chgs_5d"]) < 3:
            continue
        sector_stats[sector] = {
            "avg_chg_5d": sum(data["chgs_5d"]) / len(data["chgs_5d"]),
            "avg_chg_20d": 0,  # would need historical snapshots
            "vol_ratio": sum(data["vrs"]) / len(data["vrs"]),
            "stock_count": len(data["codes"]),
            "defensive": sector in ("银行", "电力", "石油石化", "煤炭", "公用事业", "电信"),
            "pullback_5d": False,
        }
    
    # 5. Sector rotation scoring
    sector_scores = sector_cycle_score(sector_stats, dominant)
    
    # 6. Stock-level cycle phases (top by volume, skip indices)
    top_codes = sorted(
        [c for c in snap if not c.startswith(("000001","399001","399006","000300","000688"))],
        key=lambda c: snap[c].get("amount", 0) if isinstance(snap[c], dict) else 0,
        reverse=True
    )[:500]  # top 500 by activity
    
    stock_phases = {}
    for code in top_codes:
        bars = load_kline(code)
        if not bars:
            continue
        phase_info = stock_cycle_phase(code, bars, snap.get(code))
        if phase_info:
            stock_phases[code] = phase_info
    
    # 7. Classify stocks by phase
    phase_groups = defaultdict(list)
    for code, info in stock_phases.items():
        phase_groups[info["phase"]].append({
            "code": code,
            "price_pct": info["price_pct"],
            "rps20": info["rps20"],
            "vr": info["vr"],
            "score": info["score"],
            "rsi": info["rsi"],
        })
    
    # 8. Merge with snapshot data
    pool_output = {
        "date": date_str,
        "market_phase": dominant,
        "macro_signals": {code: {"name": INDEX_CODES.get(code, (code,))[0], **info} 
                         for code, info in macro_phase.items()},
        "sector_rotation": sector_scores,
        "top_sectors": sorted(
            [(s, v) for s, v in sector_scores.items()],
            key=lambda x: -x[1]["score"]
        )[:10],
        "bottom_sectors": sorted(
            [(s, v) for s, v in sector_scores.items()],
            key=lambda x: x[1]["score"]
        )[:5],
        "phase_pools": {},
    }
    
    for phase_name, stocks in phase_groups.items():
        stocks_sorted = sorted(stocks, key=lambda x: -x["score"])
        pool_output["phase_pools"][phase_name] = {
            "count": len(stocks_sorted),
            "top": stocks_sorted[:20],
        }
    
    # Write output
    os.makedirs(POOL_DIR, exist_ok=True)
    out_path = os.path.join(POOL_DIR, f"{date_str}.cycle.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(pool_output, f, ensure_ascii=False, indent=2)
    print(f"[cycle] Saved -> {out_path}", flush=True)
    
    # Generate analysis report
    lines = [f"# 周期池报告 {date_str}", ""]
    lines.append(f"- **当前市场周期**: {dominant}")
    lines.append(f"- **数据源**: 腾讯日线K线 + 东财全市场快照")
    lines.append("")
    
    lines.append("## 一、宏观周期定位")
    lines.append("")
    lines.append("| 指数 | 名称 | 阶段 | 评分 | RSI | 量比 | 波动率 |")
    lines.append("|------|------|------|------|-----|------|--------|")
    for code, info in macro_phase.items():
        name = INDEX_CODES.get(code, (code,))[0]
        lines.append(f"| {code} | {name} | {info['phase']} | {info['score']} | {info['rsi']:.0f} | {info['vr']:.2f} | {info['vol_regime']} |")
    lines.append("")
    
    lines.append("## 二、板块轮动窗口")
    lines.append("")
    lines.append("### 推荐关注（得分前5）")
    lines.append("")
    lines.append("| 板块 | 得分 | 信号窗口 | 5日均涨跌% | 成交量比 |")
    lines.append("|------|------|---------|-----------|---------|")
    for sector, info in pool_output["top_sectors"]:
        lines.append(f"| {sector} | {info['score']} | {', '.join(info['window'])} | {info['avg_chg_5d']:+.2f}% | {info['vol_ratio']:.2f}x |")
    lines.append("")
    
    lines.append("### 回避板块")
    lines.append("")
    for sector, info in pool_output["bottom_sectors"]:
        lines.append(f"- ❌ {sector}: 得分 {info['score']}")
    lines.append("")
    
    lines.append("## 三、个股周期分布")
    lines.append("")
    for phase in ["acceleration", "breakout", "early_accumulation", "bottoming", "correction", "top_distribution"]:
        group = phase_groups.get(phase, [])
        if not group:
            continue
        lines.append(f"### {phase} ({len(group)} 只)")
        lines.append("")
        lines.append("| 代码 | 52w位置% | RPS20 | 量比 | RSI | 得分 |")
        lines.append("|------|---------|-------|------|-----|------|")
        for s in sorted(group, key=lambda x: -x["score"])[:15]:
            lines.append(f"| {s['code']} | {s['price_pct']:.0f}% | {s['rps20']:.0f} | {s['vr']:.2f}x | {s['rsi']:.0f} | {s['score']} |")
        lines.append("")
    
    lines.append("---")
    lines.append("*仅供研究，不构成投资建议*")
    
    os.makedirs(ANA_DIR, exist_ok=True)
    report_path = os.path.join(ANA_DIR, f"cycle_report_{date_str}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[cycle] Report -> {report_path}", flush=True)
    
    return pool_output


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    run(date)
