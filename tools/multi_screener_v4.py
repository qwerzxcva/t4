#!/usr/bin/env python3
"""
多轮审核选股 — Multi-Round Screener v4
整合热点池 + 周期池 + 板块池 + 低位池，输出综合推荐

流程：
  轮次1: 热点扫描（从新闻+涨停池提取主题）
  轮次2: 周期定位（宏观周期 + 板块轮动窗口）
  轮次3: 板块筛选（板块动量 + 资金流向排序）
  轮次4: 个股精选（低位启动 + 技术评分 + 上涨概率）
  轮次5: 综合打分（四池加权 → TOP N）

数据依赖（按优先级）:
  1. data/market/all_{date}.txt          (全市场快照)
  2. data/pool/{date}.ztpool.json        (涨停池)
  3. data/pool/{date}.dtpool.json        (跌停池)
  4. data/pool/{date}.cycle.json         (周期池 — 先跑 pool_cycle.py)
  5. data/pool/{date}.sector_pool.json   (板块池 — 先跑 pool_sector.py)
  6. data/pool/{date}.lowpool.json       (低位池 — 先跑 pool_lowpos.py)
  7. analysis/hotspot_impact_*.json      (热点分析)
"""
import os, sys, json, time, glob
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

NONTRADABLE = {"sh000001", "sz399001", "sz399006", "sh000300", "sh000688", "bj899050"}


# ===================================================================
# Data loaders
# ===================================================================

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


def load_pool_json(date_str, name):
    # Try both formats: with-dash (new files) and no-dash (legacy pool files)
    date_nodash = date_str.replace("-", "")
    for fmt in [date_str, date_nodash]:
        path = os.path.join(POOL_DIR, f"{fmt}.{name}.json")
        if os.path.exists(path):
            try:
                return json.load(open(path, encoding="utf-8"))
            except Exception:
                pass
    return {}


_KLINE_CACHE = None


def _build_kline_cache():
    """Pre-load all daily K-lines into a dict (one-pass, fast for 4k+ lookups)."""
    global _KLINE_CACHE
    if _KLINE_CACHE is not None:
        return _KLINE_CACHE
    cache = {}
    # Try JSON snapshot cache first (from previous run)
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bulk_kline_cache.json")
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
            print(f"[multi_v4] Loaded Kline cache from JSON: {len(cache)} codes", flush=True)
            _KLINE_CACHE = cache
            return cache
        except Exception:
            pass
    # Fallback: build from bulk CSVs
    if os.path.isdir(KL_BULK):
        import time
        t0 = time.time()
        for fn in sorted(os.listdir(KL_BULK)):
            if not fn.startswith(("day_qfq_", "day_raw_")) or not fn.endswith(".csv"):
                continue
            for line in open(os.path.join(KL_BULK, fn), encoding="utf-8"):
                if line.startswith("#") or "|" not in line:
                    continue
                p = line.rstrip().split("|")
                if len(p) < 7:
                    continue
                try:
                    code, bar_date = p[0], p[1]
                    cache.setdefault(code, []).append({
                        "date": bar_date, "o": float(p[2]), "c": float(p[3]),
                        "h": float(p[4]), "l": float(p[5]), "v": int(float(p[6])),
                    })
                except (ValueError, IndexError):
                    continue
        for code in cache:
            seen, unique = set(), []
            for bar in cache[code]:
                if bar["date"] not in seen:
                    seen.add(bar["date"])
                    unique.append(bar)
            cache[code] = sorted(unique, key=lambda x: x["date"])
        # Save to JSON for next run
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        import time as _t
        print(f"[multi_v4] Kline cache: {len(cache)} codes in {_t.time()-t0:.1f}s", flush=True)
    _KLINE_CACHE = cache
    return cache


def load_kline(code):
    """Fast K-line lookup using pre-loaded cache."""
    return _build_kline_cache().get(code, [])


def load_hotspot_themes(date_str):
    """Extract themes from latest hotspot impact JSON.
    JSON structure: {'themes_detected': [{name, count, pos, neg}, ...],
                    'mainlines': [{name, score, themes, ...}, ...],
                    'affected_top': [{code, name, themes, ...}, ...]}"""
    themes = {}
    ana_dir = os.path.join(ROOT, "..", "..", "stocks", "analysis")
    files = sorted(glob.glob(os.path.join(ana_dir, "hotspot_impact_*.json")))
    if not files:
        return themes
    latest = files[-1]
    try:
        d = json.load(open(latest, encoding="utf-8"))
        # themes_detected: list of {name, count, pos, neg}
        for t in d.get("themes_detected", []):
            name = t.get("name", "")
            if not name:
                continue
            net = t.get("pos", 0) - t.get("neg", 0)
            themes[name] = {
                "news_count": t.get("count", 0),
                "sentiment": net,
                "pos": t.get("pos", 0),
                "neg": t.get("neg", 0),
            }
        # mainlines: add sector mapping
        for ml in d.get("mainlines", []):
            ml_name = ml.get("name", "")
            if ml_name in themes:
                themes[ml_name]["mainline_score"] = ml.get("score", 0)
            for sec in ml.get("themes", []):
                if sec not in themes:
                    themes[sec] = {"news_count": 0, "sentiment": 0, "mainline_score": ml.get("score", 0)}
        # affected_top: collect related stock codes per theme
        for a in d.get("affected_top", []):
            code = a.get("code", "")
            for theme in a.get("themes", []):
                if theme in themes:
                    themes[theme].setdefault("related_codes", [])
                    if code not in themes[theme]["related_codes"]:
                        themes[theme]["related_codes"].append(code)
    except Exception:
        pass
    return themes


# ===================================================================
# Technical helpers
# ===================================================================

def ma(closes, n):
    if len(closes) < n: return None
    return sum(closes[-n:]) / n

def rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains = [max(closes[i]-closes[i-1], 0) for i in range(len(closes)-period, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(len(closes)-period, len(closes))]
    ag, al = sum(gains)/period, sum(losses)/period
    if al == 0: return 100.0
    return 100.0 - (100.0/(1.0+ag/al))

def vol_ratio_arr(vols):
    if len(vols) < 20: return 1.0
    return vols[-1] / (sum(vols[-20:])/20)


# ===================================================================
# Round 1: Hotspot Scan
# ===================================================================

def round1_hotspot(snap, themes):
    """Map stocks to hot themes and score by theme strength."""
    theme_stock_scores = defaultdict(lambda: {"score": 0, "themes": [], "detail": {}})
    
    for theme, info in themes.items():
        for stock_code in info.get("related_stocks", []):
            key = stock_code.replace("sh","").replace("sz","").replace("bj","")
            # Try to match with snapshot
            for sc in snap:
                sn = sc.replace("sh","").replace("sz","").replace("bj","")
                if sn == key:
                    theme_stock_scores[key]["themes"].append(theme)
                    theme_stock_scores[key]["score"] += info.get("sentiment", 0)
                    theme_stock_scores[key]["detail"][theme] = info
                    break
    
    return dict(theme_stock_scores)


# ===================================================================
# Round 2-4: Integrated scoring
# ===================================================================

def compute_composite_score(code, snap, cycle_info, sector_info, lowpos_info, themes, kl_cache=None):
    """
    Composite score combining all four pools.
    Returns (total_score, breakdown, signals)
    """
    if code not in snap:
        return None, {}, []
    
    s = snap[code]
    chg = s.get("chg_pct", 0)
    
    # Exclude: already limit-up or near limit-up
    if chg >= 9.5:
        return None, {"excluded": "limit_up"}, []
    
    # Exclude: ST
    name = s.get("name", "")
    if "ST" in name or "退" in name:
        return None, {"excluded": "ST"}, []
    
    breakdown = {}
    signals = []
    total = 0
    
    # --- Cycle signal (weight: 15%) ---
    cycle_score = 0
    if cycle_info:
        phase = cycle_info.get("phase", "consolidation")
        if phase in ("recovery", "acceleration"):
            cycle_score = 5
            signals.append(f"周期{phase}")
        elif phase == "bottoming":
            cycle_score = 3
            signals.append("筑底期")
        elif phase in ("decline", "recession"):
            cycle_score = -3
            signals.append(f"周期{phase}(回避)")
    breakdown["cycle"] = cycle_score
    total += cycle_score
    
    # --- Sector signal (weight: 20%) ---
    sector_score = 0
    industry = s.get("industry", "")
    if sector_info and industry in sector_info:
        ss = sector_info[industry]
        sector_score = min(ss.get("score", 0), 10)  # cap at 10
        if sector_score >= 8:
            signals.append(f"强势板块{industry}")
        elif sector_score >= 4:
            signals.append(f"中性板块{industry}")
        elif sector_score < 0:
            signals.append(f"弱势板块{industry}")
    elif sector_info:
        for sec, info in sector_info.items():
            if info.get("score", 0) >= 6:
                sector_score = min(info["score"], 5)
                signals.append(f"关联强势板块{sec}")
                break
    breakdown["sector"] = sector_score
    total += sector_score
    
    # --- Low position signal (weight: 35%) ---
    lowpos_score = 0
    if lowpos_info and code in lowpos_info:
        lp = lowpos_info[code]
        lowpos_score = lp.get("score", 0)
        for sig in lp.get("signals", [])[:3]:
            signals.append(sig)
    breakdown["lowpos"] = lowpos_score
    total += lowpos_score
    
    # --- Theme/Hotspot signal (weight: 15%) ---
    theme_score = 0
    if themes:
        key = code.replace("sh","").replace("sz","").replace("bj","")
        for theme, info in themes.items():
            for sc in info.get("related_stocks", []):
                if sc.replace("sh","").replace("sz","").replace("bj","") == key:
                    theme_score += info.get("sentiment", 0)
                    signals.append(f"热点:{theme}")
                    break
    breakdown["theme"] = theme_score
    total += theme_score
    
    # --- Technical overlay (weight: 15%) — only if K-line available ---
    tech_score = 0
    if kl_cache and code in kl_cache:
        bars = kl_cache[code]
        if len(bars) >= 30:
            closes = [b["c"] for b in bars]
            vols = [b["v"] for b in bars]
            rsi_val = rsi(closes)
            vr = (vols[-1] / (sum(vols[-20:]) / 20)) if len(vols) >= 20 else 1.0
            
            if rsi_val < 40:
                tech_score += 2
                signals.append(f"RSI={rsi_val:.0f}超卖")
            elif rsi_val > 75:
                tech_score -= 2
                signals.append(f"RSI={rsi_val:.0f}超买")
            
            if vr > 1.3 and chg > 0:
                tech_score += 2
                signals.append("放量上涨")
            elif vr < 0.7 and chg < 0:
                tech_score += 1
                signals.append("缩量下跌")
            
            m5_v = ma(closes, 5)
            m10_v = ma(closes, 10)
            if m5_v and m10_v and m5_v > m10_v:
                tech_score += 1
                signals.append("MA5>MA10")
    
    breakdown["tech"] = tech_score
    total += tech_score
    
    # --- Today's momentum (weight: 10%) ---
    mom_score = 0
    if 1 <= chg <= 5:
        mom_score = 3
        signals.append(f"温和上涨{chg:+.1f}%")
    elif chg > 5:
        mom_score = 1
        signals.append(f"大涨{chg:+.1f}%")
    elif chg < -3:
        mom_score = -3
        signals.append(f"大跌{chg:.1f}%")
    elif 0 < chg < 1:
        mom_score = 1
    
    breakdown["momentum"] = mom_score
    total += mom_score
    
    return total, breakdown, signals


def run(date_str=None):
    date_str = date_str or datetime.now(CST).strftime("%Y-%m-%d")
    ts = datetime.now(CST).strftime("%H%M")
    print(f"[multi_screener_v4] Running for {date_str}", flush=True)
    
    # Load all data
    snap = load_snapshot(date_str)
    themes = load_hotspot_themes(date_str)
    
    cycle_pool = load_pool_json(date_str, "cycle")
    sector_pool = load_pool_json(date_str, "sector_pool")
    lowpos_pool = load_pool_json(date_str, "lowpool")
    
    # Extract lookup maps
    sector_info = {s["sector"]: s for s in sector_pool.get("tier1", []) + sector_pool.get("tier2", [])}
    lowpos_map = {}
    for item in lowpos_pool.get("strong", []) + lowpos_pool.get("watch", []):
        lowpos_map[item["code"]] = item
    # Also include all lowpool entries
    for item in lowpos_pool.get("strong", []) + lowpos_pool.get("watch", []):
        lowpos_map[item["code"]] = item
    
    cycle_phase = cycle_pool.get("market_phase", "consolidation")
    
    print(f"[multi_screener_v4] Snap: {len(snap)} | Themes: {len(themes)} | "
          f"Cycle: {cycle_phase} | Sectors: {len(sector_info)} | Lowpos: {len(lowpos_map)}", flush=True)
    
    # Build candidate list
    candidates = []
    for code in snap:
        if code in NONTRADABLE:
            continue
        s = snap[code]
        chg = s.get("chg_pct", 0)
        name = s.get("name", "")
        if "ST" in name or "退" in name or name.startswith("N"):
            continue
        if chg >= 9.5:
            continue  # skip limit-up
        if s.get("mktcap", 0) < 3e9:
            continue
        candidates.append(code)
    
    print(f"[multi_screener_v4] Candidates: {len(candidates)}", flush=True)
    
    # Build K-line cache once (fast, one pass)
    kl_cache = _build_kline_cache()
    kl_codes = set(kl_cache.keys())
    print(f"[multi_screener_v4] K-line available for: {len(kl_codes)} stocks", flush=True)
    
    # Score each candidate across all 4 pools
    results = []
    for code in candidates:
        score, breakdown, signals = compute_composite_score(
            code, snap,
            cycle_pool.get("phase_pools", {}).get(cycle_phase, {}),
            sector_info, lowpos_map, themes,
            kl_cache=kl_cache
        )
        if score is None:
            continue
        
        s = snap[code]
        results.append({
            "code": code,
            "name": s.get("name", ""),
            "price": s.get("price", 0),
            "chg_pct": s.get("chg_pct", 0),
            "industry": s.get("industry", ""),
            "score": round(score, 2),
            "breakdown": breakdown,
            "signals": signals,
            "vol_ratio": s.get("vol_ratio", 1),
            "mktcap": s.get("mktcap", 0),
            "pe": s.get("pe", 0),
        })
    
    # Sort by composite score
    results.sort(key=lambda x: -x["score"])
    
    # Classify into tiers (thresholds tuned for multi-factor scoring)
    tier_buy = [r for r in results if r["score"] >= 18]
    tier_watch = [r for r in results if 8 <= r["score"] < 18]
    tier_avoid = [r for r in results if r["score"] < 8]
    
    # Output JSON
    output = {
        "date": date_str,
        "ts": ts,
        "market_phase": cycle_phase,
        "rounds": 5,
        "candidates": len(candidates),
        "tier_buy_count": len(tier_buy),
        "tier_watch_count": len(tier_watch),
        "tier_avoid_count": len(tier_avoid),
        "buy": tier_buy[:30],
        "watch": tier_watch[:30],
        "avoid": tier_avoid[:20],
        "summary": {
            "hotspot_themes": list(themes.keys()),
            "top_sectors": [s["sector"] for s in sector_pool.get("tier1", [])[:5]],
            "cycle_phase": cycle_phase,
        }
    }
    
    os.makedirs(POOL_DIR, exist_ok=True)
    out_path = os.path.join(POOL_DIR, f"{date_str}_multi_v4.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    # Generate markdown report
    lines = [f"# 多轮审核选股 v4 — {date_str} {ts} CST", "",
             f"- 审核轮次: 5 | 候选: {len(candidates)} | 市场周期: **{cycle_phase}**",
             "- 数据源: 东财全市场快照 + 涨停池 + 周期算法 + 板块轮动 + 低位因子",
             "- 热点主题: " + ", ".join(themes.keys())[:80] if themes else "无",
             ""]
    
    # Round 1: Hotspot
    lines.append("## 轮次 1: 热点扫描")
    lines.append("")
    if themes:
        for theme, info in themes.items():
            lines.append(f"- **{theme}** ({info.get('news_count', 0)}条, 热度 {info.get('sentiment', 0)}%)")
    else:
        lines.append("- 今日无明显热点（等待新闻数据采集）")
    lines.append("")
    
    # Round 2: Cycle
    lines.append("## 轮次 2: 周期定位")
    lines.append("")
    lines.append(f"- **宏观周期**: {cycle_phase}")
    if "macro_signals" in cycle_pool:
        lines.append("")
        lines.append("| 指数 | 阶段 | 评分 | RSI | 量比 |")
        lines.append("|------|------|------|-----|------|")
        for code, info in cycle_pool["macro_signals"].items():
            lines.append(f"| {code} | {info['phase']} | {info['score']} | {info['rsi']:.0f} | {info['vr']:.2f} |")
    lines.append("")
    
    # Round 3: Sector
    lines.append("## 轮次 3: 板块轮动")
    lines.append("")
    lines.append("### 强势板块（Tier 1）")
    lines.append("")
    lines.append("| 板块 | 得分 | 5日均涨跌% | 涨停数 | 主力流入亿 |")
    lines.append("|------|------|-----------|--------|-----------|")
    for s in sector_pool.get("tier1", [])[:8]:
        lines.append(f"| {s['sector']} | {s['score']} | {s['avg_chg_1d']:+.2f}% | {s['zt_count']} | {s['total_main_net']/1e9:+.1f} |")
    lines.append("")
    
    # Round 4: Low position
    lines.append("## 轮次 4: 低位启动候选")
    lines.append("")
    lines.append(f"- 低位候选总数: {lowpos_pool.get('total_filtered', 0)}")
    lines.append(f"- 强烈买入: {lowpos_pool.get('strong_buy', 0)} | 关注: {lowpos_pool.get('watch', 0)}")
    lines.append("")
    if lowpos_pool.get("strong"):
        lines.append("| 代码 | 名称 | 现价 | 涨% | 52w位置% | 核心信号 |")
        lines.append("|------|------|------|-----|---------|---------|")
        for r in lowpos_pool["strong"][:10]:
            sig = "; ".join(r.get("signals", [])[:2])
            pp = f"{r['price_pct']:.0f}%" if r.get('price_pct') is not None else "—"
            lines.append(f"| {r['code']} | {r.get('name','')[:6]} | {r['price']:.2f} | {r['chg_pct']:+.2f}% | {pp} | {sig} |")
    lines.append("")
    
    # Round 5: Composite
    lines.append("## 轮次 5: 综合打分 TOP")
    lines.append("")
    lines.append(f"- 强烈推荐 (score>=6): {len(tier_buy)} 只")
    lines.append(f"- 建议关注 (2<=score<6): {len(tier_watch)} 只")
    lines.append(f"- 回避 (score<2): {len(tier_avoid)} 只")
    lines.append("")
    
    lines.append("### ⭐ 强烈推荐")
    lines.append("")
    lines.append("| 代码 | 名称 | 现价 | 涨% | 综合分 | 周期 | 板块 | 低位 | 信号 |")
    lines.append("|------|------|------|-----|-------|------|------|------|------|")
    for r in tier_buy[:20]:
        bd = r["breakdown"]
        cycle_sig = "—"
        sector_sig = "—"
        lowpos_sig = "—"
        if bd.get("cycle", 0) != 0:
            cycle_sig = f"{bd['cycle']:+d}"
        if bd.get("sector", 0) != 0:
            sector_sig = f"{bd['sector']:+d}"
        if bd.get("lowpos", 0) != 0:
            lowpos_sig = f"{bd['lowpos']:+d}"
        sig_str = " | ".join(r["signals"][:4])
        lines.append(f"| {r['code']} | {r['name'][:8]} | {r['price']:.2f} | {r['chg_pct']:+.2f}% | **{r['score']}** | {cycle_sig} | {sector_sig} | {lowpos_sig} | {sig_str} |")
    lines.append("")
    
    lines.append("### ⚠️ 建议关注")
    lines.append("")
    lines.append("| 代码 | 名称 | 现价 | 综合分 | 信号 |")
    lines.append("|------|------|------|-------|------|")
    for r in tier_watch[:15]:
        sig_str = " | ".join(r["signals"][:3])
        lines.append(f"| {r['code']} | {r['name'][:8]} | {r['price']:.2f} | {r['score']} | {sig_str} |")
    lines.append("")
    
    lines.append("## 评分因子权重")
    lines.append("")
    lines.append("| 因子 | 权重 | 说明 |")
    lines.append("|------|------|------|")
    lines.append("| 低位启动信号 | 35% | 52w低位 + 金叉 + 放量 + RSI区间的综合打分 |")
    lines.append("| 板块轮动 | 20% | 板块动量 + 资金流向 + 当前周期适配性 |")
    lines.append("| 宏观周期 | 15% | 当前市场处于牛/熊/震荡的哪个阶段 |")
    lines.append("| 热点共振 | 15% | 新闻热点与个股关联度 |")
    lines.append("| 技术叠加 | 15% | RSI + 量比 + MA排列的即时确认 |")
    lines.append("")
    
    lines.append("---")
    lines.append("*仅供研究，不构成投资建议*")
    
    os.makedirs(ANA_DIR, exist_ok=True)
    report_path = os.path.join(ANA_DIR, f"multi_screener_{date_str}_{ts}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    
    # Also save JSON
    with open(os.path.join(ANA_DIR, f"multi_screener_{date_str}_{ts}.json"), "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"[multi_screener_v4] Done: BUY={len(tier_buy)} WATCH={len(tier_watch)} AVOID={len(tier_avoid)}", flush=True)
    print(f"[multi_screener_v4] -> {report_path}", flush=True)
    print(f"[multi_screener_v4] -> {out_path}", flush=True)
    
    return output


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    run(date)
