#!/usr/bin/env python3
"""
低位股票池 — Low Position Pool
筛选：尚未涨停、处于低位、有启动迹象、明日上涨概率高的股票

核心逻辑：
1. 排除涨停板（连板票不在这）
2. 价格处于52周底部区域（低于近250日最高价60%以上）
3. 技术面出现启动信号（MA5上穿MA10、放量、主力资金流入）
4. 用多因子加权打分，预测次日上涨概率

输入：
- data/market/all_YYYY-MM-DD.txt
- data/kline/history/*.txt

输出：
- data/pool/{date}.lowpool.json
- analysis/lowpool_{date}.md
"""
import os, sys, json, time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

ROOT = os.environ.get("STOCKS_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KL_DIR = os.environ.get("STOCKS_KLINE_DIR") or os.path.join(ROOT, "..", "..", "stocks-data-kline", "data")
KL_DIR = os.path.abspath(KL_DIR)
# bulk/ has shard CSVs (day_qfq_00.csv etc); index/history/ has per-stock .txt files
KL_BULK = os.path.join(KL_DIR, "bulk")
KL_HISTORY = os.path.join(KL_DIR, "index", "history")
SNAPSHOT_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "market")
POOL_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "pool")
ANA_DIR = os.path.join(ROOT, "..", "..", "stocks", "analysis")

NONTRADABLE = {"sh000001", "sz399001", "sz399006", "sh000300", "sh000688", "bj899050"}
_KLINE_CACHE = None


def load_snapshot(date_str):
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
    """Load daily kline for a code. Uses global pre-loaded cache if available."""
    global _KLINE_CACHE
    if _KLINE_CACHE is None:
        _KLINE_CACHE = _build_kline_cache()
    return _KLINE_CACHE.get(code, [])


def _build_kline_cache():
    """One-pass load of daily kline bulk shard CSVs into a {code: [bars]} dict.
    Only loads day_qfq (primary) and day_raw (fallback) shards.
    """
    cache = {}
    if os.path.isdir(KL_BULK):
        import time
        t0 = time.time()
        for fn in sorted(os.listdir(KL_BULK)):
            # Only load daily data (day_qfq_*, day_raw_*), skip m60/week/month
            if not fn.startswith(("day_qfq_", "day_raw_")):
                continue
            if not fn.endswith(".csv"):
                continue
            print(f"  loading {fn} ...", flush=True)
            for line in open(os.path.join(KL_BULK, fn), encoding="utf-8"):
                if line.startswith("#") or "|" not in line:
                    continue
                p = line.rstrip().split("|")
                if len(p) < 7:
                    continue
                try:
                    code, bar_date = p[0], p[1]
                    bar = {"date": bar_date, "o": float(p[2]), "c": float(p[3]),
                           "h": float(p[4]), "l": float(p[5]), "v": int(float(p[6]))}
                    # Prefer qfq data over raw; both go into same cache
                    cache.setdefault(code, []).append(bar)
                except (ValueError, IndexError):
                    continue
        # Deduplicate by date and sort
        for code in cache:
            seen_dates = set()
            unique = []
            for bar in cache[code]:
                if bar["date"] not in seen_dates:
                    seen_dates.add(bar["date"])
                    unique.append(bar)
            unique.sort(key=lambda x: x["date"])
            cache[code] = unique
        print(f"[lowpos] Kline cache: {len(cache)} codes in {time.time()-t0:.1f}s", flush=True)
    return cache


def ma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = [max(closes[i] - closes[i-1], 0) for i in range(len(closes)-period, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(len(closes)-period, len(closes))]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + ag / al))


def vol_ratio_arr(vols):
    if len(vols) < 20:
        return 1.0
    return vols[-1] / (sum(vols[-20:]) / 20)


def macd_hist(closes, fast=12, slow=26, signal=9):
    """Return simple MACD histogram value (approximation)."""
    if len(closes) < slow + signal:
        return 0.0
    def ema(arr, n):
        k = 2.0 / (n + 1)
        v = arr[0]
        for x in arr[1:]:
            v = v * (1 - k) + x * k
        return v
    dif_vals = []
    for i in range(len(closes)):
        sub = closes[max(0, i-slow+1):i+1]
        if len(sub) < fast:
            dif_vals.append(0.0)
            continue
        e12 = ema(sub, fast)
        e26 = ema(sub, slow)
        dif_vals.append((e12 - e26) if e12 and e26 else 0.0)
    if len(dif_vals) < signal:
        return dif_vals[-1] if dif_vals else 0.0
    dif = dif_vals[-1]
    dea = ema(dif_vals[-signal:], signal)
    return (dif - dea) * 2


def load_zt_codes(date_str):
    """Load today's limit-up pool to exclude those."""
    zt_codes = set()
    # Pool files use YYYYMMDD format (no dashes)
    date_nodash = date_str.replace("-", "")
    for fmt in [f"{date_nodash}", f"{date_str}"]:
        pool_path = os.path.join(POOL_DIR, f"{fmt}.ztpool.json")
        if os.path.exists(pool_path):
            try:
                d = json.load(open(pool_path, encoding="utf-8"))
                for p in d.get("pool", []):
                    zt_codes.add(p.get("c", ""))
            except Exception:
                pass
            break
    return zt_codes


def score_lowpos_stock(code, snap, bars):
    """Score low-position stock. Uses K-line if available (>=30 bars),
    otherwise falls back to snapshot-only scoring."""
    chg = snap.get("chg_pct", 0) if snap else 0
    if chg >= 9.5:
        return None
    
    # ---- K-line based scoring (full technicals) ----
    if bars and len(bars) >= 30:
        closes = [b["c"] for b in bars]
        vols = [b["v"] for b in bars]
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]
        current = closes[-1]
        h52 = max(highs[-min(250,len(highs)):]) if highs else 0
        l52 = min(lows[-min(250,len(lows)):]) if lows else 0
        price_pct = (current - l52) / max(h52 - l52, 0.01) * 100
        if price_pct > 50:
            return None
        m5=ma(closes,5); m10=ma(closes,10); m20=ma(closes,20)
        rsi_val=rsi(closes); vr=vol_ratio_arr(vols); mh=macd_hist(closes)
        golden_cross=False
        if m5 and m10 and len(closes)>11:
            pm5=ma(closes[:-1],5); pm10=ma(closes[:-1],10)
            if pm5 and pm10: golden_cross=(m5>m10) and (pm5<=pm10)
        ma_spread=0
        if m5 and m10 and m20:
            ma_spread=(max(m5,m10,m20)-min(m5,m10,m20))/m20*100 if m20 else 0
        dist_ma20=(current/m20-1)*100 if m20 else 0
        score=0; signals=[]
        if price_pct<15: score+=4; signals.append(f"极度低位({price_pct:.0f}%)")
        elif price_pct<30: score+=2; signals.append(f"低位({price_pct:.0f}%)")
        if golden_cross: score+=5; signals.append("MA5上穿MA10金叉")
        elif m5 and m10 and m5>m10: score+=2; signals.append("MA5>MA10")
        if 35<=rsi_val<=55: score+=2; signals.append(f"RSI={rsi_val:.0f}")
        elif rsi_val<35: score+=1; signals.append(f"RSI={rsi_val:.0f}超卖")
        elif rsi_val>70: score-=3; signals.append(f"RSI={rsi_val:.0f}超买")
        if mh>0.01: score+=2; signals.append("MACD转正")
        elif mh>-0.01: score+=1
        elif mh<-0.05: score-=2
        if vr>1.3 and chg>0: score+=3; signals.append("放量上涨")
        elif vr<0.8 and chg<0: score+=1; signals.append("缩量下跌")
        if 0<ma_spread<3: score+=2; signals.append(f"均线粘合({ma_spread:.1f}%)")
        mn=snap.get("main_net",0) if snap else 0
        if mn>5e8: score+=2; signals.append(f"主力净入{mn/1e8:.1f}亿")
        elif mn>0: score+=1
        if 1<=chg<=4: score+=2; signals.append(f"温和{chg:+.1f}%")
        elif chg<-3: score-=2; signals.append(f"大跌{chg:.1f}%")
        if -5<=dist_ma20<=5: score+=1; signals.append("贴近MA20")
        mk=snap.get("mktcap",0) if snap else 0
        if mk<3e9: score-=2; signals.append("市值过小")
        return {"score":score,"signals":signals,"price_pct":round(price_pct,1),
                "rsi":round(rsi_val,1),"vr":round(vr,2),"golden_cross":golden_cross,
                "dist_ma20":round(dist_ma20,1),"ma_spread":round(ma_spread,1),
                "macd_hist":round(mh,3),"kline_bars":len(bars)}
    
    # ---- Snapshot-only fallback (no K-line or too few bars) ----
    pe=snap.get("pe",0) if snap else 0
    mk=snap.get("mktcap",0) if snap else 0
    mn=snap.get("main_net",0) if snap else 0
    vr_sn=snap.get("vol_ratio",1) if snap else 1
    tr=snap.get("turnover",0) if snap else 0
    if pe<=0 or pe>80: return None
    if mk<3e9: return None
    if tr<0.5: return None
    score=0; signals=["快照模式"]
    if pe<20: score+=3; signals.append(f"低估值PE={pe:.1f}")
    elif pe<40: score+=1
    if 1<=chg<=4: score+=3; signals.append(f"温和{chg:+.1f}%")
    elif 0<chg<1: score+=1
    elif chg<-5: score-=2
    if vr_sn>1.3: score+=2; signals.append(f"量比{vr_sn:.1f}x")
    elif vr_sn<0.7: score-=1
    if mn>1e9: score+=3; signals.append(f"主力{mn/1e9:.1f}亿")
    elif mn>0: score+=1
    if 1<=tr<=8: score+=1
    return {"score":score,"signals":signals,"price_pct":None,
            "rsi":None,"vr":round(vr_sn,2),"golden_cross":False,
            "dist_ma20":None,"ma_spread":None,"macd_hist":None,
            "kline_bars":len(bars) if bars else 0}


def run(date_str=None):
    date_str = date_str or datetime.now(CST).strftime("%Y-%m-%d")
    print(f"[lowpool] Running for {date_str}", flush=True)
    
    snap = load_snapshot(date_str)
    zt_codes = load_zt_codes(date_str)
    print(f"[lowpool] Snapshot: {len(snap)} stocks, {len(zt_codes)} limit-up excluded", flush=True)
    
    # Filter: exclude indices, limit-up, ST, etc.
    candidates = []
    for code, s in snap.items():
        if code in NONTRADABLE:
            continue
        if code in zt_codes:
            continue
        name = s.get("name", "")
        if "ST" in name or "退" in name or name.startswith("N"):
            continue
        if s.get("price", 0) <= 0:
            continue
        chg = s.get("chg_pct", 0)
        # Already near limit-up? skip
        if chg >= 8:
            continue
        candidates.append((code, s))
    
    print(f"[lowpool] Candidates after filtering: {len(candidates)}", flush=True)
    
    # Score each candidate
    results = []
    kline_count = 0
    snap_mode_count = 0
    for code, s in candidates:
        bars = load_kline(code)
        info = score_lowpos_stock(code, s, bars)
        if info is None:
            continue
        if info.get("kline_bars", 0) >= 30:
            kline_count += 1
        else:
            snap_mode_count += 1
        info.update({
            "code": code,
            "name": s.get("name", ""),
            "price": s.get("price", 0),
            "chg_pct": s.get("chg_pct", 0),
            "industry": s.get("industry", ""),
            "vol_ratio": s.get("vol_ratio", 1),
            "mktcap": s.get("mktcap", 0),
            "pe": s.get("pe", 0),
            "main_net": s.get("main_net", 0),
            "turnover": s.get("turnover", 0),
        })
        results.append(info)
    # Sort by score descending
    results.sort(key=lambda x: -x["score"])
    
    # Categorize
    strong = [r for r in results if r["score"] >= 8]
    medium = [r for r in results if 4 <= r["score"] < 8]
    weak = [r for r in results if 0 <= r["score"] < 4]
    
    # Output JSON
    pool_out = {
        "date": date_str,
        "total_filtered": len(results),
        "strong_buy": len(strong),
        "watch": len(medium),
        "low_priority": len(weak),
        "strong": strong[:30],
        "watch": medium[:30],
    }
    
    os.makedirs(POOL_DIR, exist_ok=True)
    out_path = os.path.join(POOL_DIR, f"{date_str}.lowpool.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(pool_out, f, ensure_ascii=False, indent=2)
    print(f"[lowpool] Saved -> {out_path} (strong={len(strong)} watch={len(medium)})", flush=True)
    
    # Generate report
    lines = [f"# 低位股票池 {date_str}", "",
             f"- 候选: {len(results)} → 强烈买入 {len(strong)} | 关注 {len(medium)} | 低优先级 {len(weak)}",
             f"- K线评分: {kline_count} 只 | 快照评分: {snap_mode_count} 只",
             "- 条件: 未涨停 + 52w低位(或低估值) + 技术启动信号 + 资金流入",
             ""]
    
    lines.append("## ⭐ 强烈买入 (score >= 8)")
    lines.append("")
    lines.append("| 代码 | 名称 | 行业 | 现价 | 涨% | 位置% | RSI | 量比 | 金叉 | 核心信号 |")
    lines.append("|------|------|------|------|-----|-------|-----|------|------|---------|")
    for r in strong[:20]:
        pp = f"{r['price_pct']:.0f}%" if r.get('price_pct') is not None else "—"
        rs = f"{r['rsi']:.0f}" if r.get('rsi') is not None else "—"
        gc = "✓" if r.get("golden_cross") else "—"
        sig = "; ".join(r["signals"][:3])
        lines.append(f"| {r['code']} | {r['name'][:8]} | {r['industry'][:8]} | {r['price']:.2f} | {r['chg_pct']:+.2f}% | {pp} | {rs} | {r['vr']:.2f}x | {gc} | {sig} |")
    lines.append("")
    
    lines.append("## ⚠️ 关注 (4 <= score < 8)")
    lines.append("")
    lines.append("| 代码 | 名称 | 现价 | 涨% | 核心信号 |")
    lines.append("|------|------|------|-----|---------|")
    for r in medium[:20]:
        sig = "; ".join(r["signals"][:2])
        lines.append(f"| {r['code']} | {r['name'][:8]} | {r['price']:.2f} | {r['chg_pct']:+.2f}% | {sig} |")
    lines.append("")
    
    lines.append("## 预测逻辑")
    lines.append("")
    lines.append("| 因子 | 权重 | 说明 |")
    lines.append("|------|------|------|")
    lines.append("| 金叉信号 | +5 | MA5上穿MA10是最强启动信号 |")
    lines.append("| 超卖程度 | +4 | 52w位置越低，反弹空间越大 |")
    lines.append("| 温和上涨 | +2 | 今日涨幅1-4%最佳，表明有资金但未过热 |")
    lines.append("| 放量确认 | +3 | 量比>1.3且价格上涨 |")
    lines.append("| RSI区域 | +2 | 35-55为最佳买入区间 |")
    lines.append("| MACD转正 | +2 | 绿柱缩短或红柱放大 |")
    lines.append("| 均线粘合 | +2 | 多均线收敛预示变盘 |")
    lines.append("| 主力资金 | +2 | 主力净流入确认 |")
    lines.append("")
    
    lines.append("---")
    lines.append("*仅供研究，不构成投资建议。低位票需结合大盘周期判断入场时机。*")
    
    os.makedirs(ANA_DIR, exist_ok=True)
    report_path = os.path.join(ANA_DIR, f"lowpool_{date_str}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[lowpool] Report -> {report_path}", flush=True)
    
    # Print top picks to stdout
    print(f"\n=== 低位池 TOP10 ===")
    for r in strong[:10] + medium[:5]:
        sig_str = " | ".join(r["signals"][:3])
        print(f"  {r['code']} {r['name'][:6]} score={r['score']:.1f} chg={r['chg_pct']:+.1f}% {sig_str}")
    
    return pool_out


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    run(date)
