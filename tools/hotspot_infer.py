#!/usr/bin/env python3
"""
实时热点推演 — Hotspot Stock Inference v1
核心逻辑：
  1. 读最新 hotspot_impact_*.json（主题+情感+关联涨停票）
  2. 对每个主题，从全市场快照找同行业（industry）股票
  3. 剔除已涨停/连板票（不在本池，在高位不追）
  4. 用多因子对候选票打分：
     - 主题热度（news_count + sentiment）
     - 行业动量（sector_score from sector_pool）
     - 技术位置（52w低位 + RSI健康 + 均线粘合）
     - 资金流（主力净流入）
     - 今日涨幅（温和上涨最佳，避免追高）
  5. 输出：按主题分组 → 每组TOP票 → 明日预测（上涨概率估计）

输入：
  - analysis/hotspot_impact_*.json （热点分析结果）
  - data/market/all_YYYY-MM-DD.txt （全市场快照）
  - data/pool/{date}.sector_pool.json （板块评分）
  - data/pool/{date}.lowpool.json （低位评分）
  - data/kline/bulk/day_qfq_*.csv （K线历史）

输出：
  - data/pool/{date}.hotspot_infer.json
  - analysis/hotspot_infer_{date}.md
"""
import os, sys, json, glob, time, csv
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

ROOT = os.environ.get("STOCKS_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KL_BULK = os.path.join(ROOT, "..", "..", "stocks-data-kline", "data", "bulk")
KL_HISTORY = os.path.join(ROOT, "..", "..", "stocks-data-kline", "data", "index", "history")
SNAPSHOT_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "market")
POOL_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "pool")
ANA_DIR = os.path.join(ROOT, "..", "..", "stocks", "analysis")

NONTRADABLE = {"sh000001", "sz399001", "sz399006", "sh000300", "sh000688", "bj899050"}


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #

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
                "code": p[0], "name": p[1], "price": float(p[2]),
                "chg_pct": float(p[3]), "amount": float(p[4]),
                "turnover": float(p[5]), "pe": float(p[6]) or 0,
                "vol_ratio": float(p[7]), "mktcap": float(p[8]),
                "pb": float(p[10]) or 0, "main_net": float(p[11]),
                "industry": p[15].strip(),
            }
        except (ValueError, IndexError):
            continue
    return rows


def load_hotspot_data(date_str):
    """Load all recent hotspot JSONs into a unified structure."""
    themes = {}
    affected_top = []
    mainlines = []
    ana_dir = os.path.join(ROOT, "..", "..", "stocks", "analysis")
    files = sorted(glob.glob(os.path.join(ana_dir, "hotspot_impact_*.json")))
    if not files:
        return themes, affected_top, mainlines
    latest = files[-1]
    try:
        d = json.load(open(latest, encoding="utf-8"))
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
                "related_codes": [],
            }
        for ml in d.get("mainlines", []):
            mainlines.append(ml)
            ml_name = ml.get("name", "")
            for sec in ml.get("themes", []):
                if sec not in themes:
                    themes[sec] = {"news_count": 0, "sentiment": 0,
                                   "mainline_score": ml.get("score", 0),
                                   "related_codes": []}
                else:
                    themes[sec]["mainline_score"] = ml.get("score", 0)
        for a in d.get("affected_top", []):
            affected_top.append(a)
            code = a.get("code", "")
            for theme in a.get("themes", []):
                if theme in themes:
                    if code not in themes[theme]["related_codes"]:
                        themes[theme]["related_codes"].append(code)
    except Exception:
        pass
    return themes, affected_top, mainlines


def load_zt_codes(date_str):
    """Get limit-up codes to exclude."""
    zt_codes = set()
    date_nodash = date_str.replace("-", "")
    for fmt in [date_nodash, date_str]:
        p = os.path.join(POOL_DIR, f"{fmt}.ztpool.json")
        if os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8"))
                for x in d.get("pool", []):
                    zt_codes.add(x.get("c", ""))
            except Exception:
                pass
            break
    return zt_codes


# K-line cache (loaded once)
_KL_CACHE = None


def _build_kline_cache():
    global _KL_CACHE
    if _KL_CACHE is not None:
        return _KL_CACHE
    cache = {}
    if os.path.isdir(KL_BULK):
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
                    code = p[0]
                    cache.setdefault(code, []).append({
                        "date": p[1], "o": float(p[2]), "c": float(p[3]),
                        "h": float(p[4]), "l": float(p[5]), "v": int(float(p[6])),
                    })
                except (ValueError, IndexError):
                    continue
        for code in cache:
            seen, unique = set(), []
            for b in cache[code]:
                if b["date"] not in seen:
                    seen.add(b["date"])
                    unique.append(b)
            cache[code] = sorted(unique, key=lambda x: x["date"])
        print(f"[hotspot_infer] Kline cache: {len(cache)} codes in {time.time()-t0:.1f}s", flush=True)
    _KL_CACHE = cache
    return cache


def get_kline(code):
    return _build_kline_cache().get(code, [])


# --------------------------------------------------------------------------- #
# Technical helpers
# --------------------------------------------------------------------------- #

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
    return vols[-1] / (sum(vols[-20:]) / 20)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def score_hotspot_stock(code, snap, kline, theme_info, sector_scores, theme_industries):
    """Score a stock based on hot theme exposure + technical + fundamental.
    
    Theme boost ONLY applies if the stock's industry is in theme_industries[theme].
    Without industry match, the stock gets zero theme boost (no false positives).
    """
    chg = snap.get("chg_pct", 0)
    name = snap.get("name", "")
    if "ST" in name or "退" in name:
        return None
    if chg >= 9.0:
        return None
    if snap.get("mktcap", 0) < 2e9:
        return None

    signals = []
    score = 0
    industry = snap.get("industry", "")
    
    # Theme boost: only if industry matches this theme's related industries
    theme_boost = 0
    mainline_score = theme_info.get("mainline_score", 0)
    news_count = theme_info.get("news_count", 0)
    sentiment = theme_info.get("sentiment", 0)
    
    related_industries = theme_industries.get(theme_info.get("name", ""), set())
    is_industry_match = bool(related_industries and industry in related_industries)
    
    if is_industry_match:
        # Strong signal: stock is in a hot industry for this theme
        if mainline_score >= 50:
            theme_boost = 8
            signals.append(f"主线({mainline_score:.0f})✓行业")
        elif news_count >= 2:
            theme_boost = 5
            signals.append(f"热点✓行业({news_count}条)")
        elif news_count >= 1:
            theme_boost = 3
            signals.append(f"热点✓行业")
        else:
            theme_boost = 2
    elif mainline_score >= 50:
        # Stock not in related industry but theme is major — small baseline signal
        theme_boost = 1
        signals.append(f"主线主题({news_count}条)")
    elif news_count >= 3:
        theme_boost = 2
        signals.append(f"热点密集({news_count}条)")
    elif news_count >= 1:
        theme_boost = 1
    
    if sentiment > 0 and is_industry_match:
        theme_boost += sentiment * 2
        signals.append(f"正情绪{sentiment:+d}")
    elif sentiment < 0:
        theme_boost -= abs(sentiment)

    # --- Sector momentum (weight: 20%) ---
    sector_score_val = 0
    if industry in sector_scores:
        ss = sector_scores[industry]
        sector_score_val = min(ss.get("score", 0), 10)
        score += sector_score_val
        if sector_score_val >= 8:
            signals.append(f"强势板块{industry}")
        elif sector_score_val >= 4:
            signals.append(f"活跃板块{industry}")
    elif sector_scores:
        for sec, info in sorted(sector_scores.items(), key=lambda x: -x[1].get("score", 0))[:3]:
            if info.get("score", 0) >= 6:
                score += 2
                signals.append(f"近邻强势{sec}")
                break

    # --- K-line / snapshot technicals (weight: 30%) ---
    kl_score = 0
    if kline and len(kline) >= 30:
        closes = [b["c"] for b in kline]
        vols = [b["v"] for b in kline]
        highs = [b["h"] for b in kline]
        lows = [b["l"] for b in kline]
        current = closes[-1]
        h52 = max(highs[-min(250, len(highs)):])
        l52 = min(lows[-min(250, len(lows)):])
        price_pct = (current - l52) / max(h52 - l52, 0.01) * 100
        m5 = ma(closes, 5); m10 = ma(closes, 10); m20 = ma(closes, 20)
        rsi_val = rsi(closes); vr = vol_ratio_arr(vols)

        if price_pct < 30: kl_score += 4; signals.append(f"低位{price_pct:.0f}%")
        elif price_pct < 50: kl_score += 2
        if m5 and m10 and m5 > m10: kl_score += 3; signals.append("MA5>MA10")
        if 35 <= rsi_val <= 60: kl_score += 2; signals.append(f"RSI={rsi_val:.0f}")
        elif rsi_val < 35: kl_score += 1; signals.append(f"RSI={rsi_val:.0f}超卖")
        elif rsi_val > 75: kl_score -= 2; signals.append(f"RSI={rsi_val:.0f}超买")
        if vr > 1.3 and chg > 0: kl_score += 3; signals.append("放量上涨")
        elif vr < 0.7 and chg < 0: kl_score += 1
    else:
        pe = snap.get("pe", 0); vr = snap.get("vol_ratio", 1)
        if 5 <= pe <= 25: kl_score += 3; signals.append(f"低估值PE={pe:.1f}")
        elif pe <= 50: kl_score += 1
        if vr > 1.3: kl_score += 2; signals.append(f"量比{vr:.1f}x")

    score += kl_score

    # --- Capital flow (weight: 15%) ---
    main_net = snap.get("main_net", 0)
    if main_net > 5e8: score += 4; signals.append(f"主力净入{main_net/1e8:.1f}亿")
    elif main_net > 0: score += 2; signals.append("主力小净流入")
    elif main_net < -2e8: score -= 3; signals.append(f"主力流出{abs(main_net)/1e8:.1f}亿")

    # --- Today's move (weight: 10%) ---
    if 1 <= chg <= 5: score += 3; signals.append(f"温和{chg:+.1f}%")
    elif 0 < chg < 1: score += 1
    elif chg < -3: score -= 2; signals.append(f"大跌{chg:.1f}%")
    elif chg > 7: score -= 3; signals.append(f"暴涨{chg:.1f}%追高风险")

    # --- Market cap ---
    mk = snap.get("mktcap", 0)
    if 10e9 <= mk <= 200e9: score += 2
    elif mk > 500e9: score -= 1

    # Apply theme boost last (small weight)
    score += theme_boost

    return {
        "score": score, "signals": signals, "kl_score": kl_score,
        "theme_boost": theme_boost, "industry": industry, "pe": snap.get("pe", 0),
        "price": snap.get("price", 0), "chg_pct": chg, "vr": snap.get("vol_ratio", 1),
        "main_net": main_net, "mktcap": mk,
    }


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #

def run(date_str=None):
    date_str = date_str or datetime.now(CST).strftime("%Y-%m-%d")
    ts = datetime.now(CST).strftime("%H%M")
    print(f"[hotspot_infer] Running for {date_str}", flush=True)

    snap = load_snapshot(date_str)
    print(f"[hotspot_infer] Snapshot: {len(snap)} stocks", flush=True)

    themes, affected_top, mainlines = load_hotspot_data(date_str)
    print(f"[hotspot_infer] Themes: {len(themes)} | Affected top: {len(affected_top)}", flush=True)

    zt_codes = load_zt_codes(date_str)

    # Load sector scores
    sector_scores = {}
    sp_path = os.path.join(POOL_DIR, f"{date_str}.sector_pool.json")
    if not os.path.exists(sp_path):
        for fmt in [date_str.replace("-", ""), date_str]:
            alt = os.path.join(POOL_DIR, f"{fmt}.sector_pool.json")
            if os.path.exists(alt):
                sp_path = alt
                break
    if os.path.exists(sp_path):
        try:
            sp = json.load(open(sp_path, encoding="utf-8"))
            for s in sp.get("tier1", []) + sp.get("tier2", []):
                sector_scores[s["sector"]] = s
        except Exception:
            pass

    # Build theme -> affected industries map from affected_top data
    # This ensures theme boost only applies to genuinely related industries
    theme_to_industries = defaultdict(set)
    theme_to_codes = defaultdict(set)
    for a in affected_top:
        code = a.get("code", "")
        for theme in a.get("themes", []):
            theme_to_codes[theme].add(code)
    # Map industries from snapshot for affected codes
    for code, s in snap.items():
        if code in zt_codes:
            continue
        ind = s.get("industry", "")
        if not ind:
            continue
        # Check if this code is related to any theme via affected_top
        clean_code = code.replace("sh","").replace("sz","").replace("bj","")
        for theme, codes in theme_to_codes.items():
            if clean_code in codes or code in codes:
                theme_to_industries[theme].add(ind)

    # Also build: for each theme, which industries are hot (from sector pool top sectors)
    # This is a heuristic: if a theme's mainline includes sectors that are strong,
    # all stocks in those sectors get the theme boost
    for ml in mainlines:
        ml_name = ml.get("name", "")
        if ml_name in themes and ml.get("score", 0) > 0:
            # The mainline's themes field may list related sectors
            for sec in ml.get("themes", []):
                if sec in sector_scores and sector_scores[sec].get("score", 0) >= 6:
                    for code, s in snap.items():
                        if s.get("industry") == sec:
                            theme_to_industries[ml_name].add(sec)

    print(f"[hotspot_infer] Theme-industry mapping: {dict((k, len(v)) for k,v in theme_to_industries.items())}", flush=True)

    # Build inverted index: industry -> list of codes from snapshot
    industry_to_codes = defaultdict(list)
    for code, s in snap.items():
        ind = s.get("industry", "")
        if ind:
            industry_to_codes[ind].append(code)

    # For each theme, find candidate stocks in related industries
    results_by_theme = defaultdict(list)
    all_results = []

    for theme_name, theme_info in themes.items():
        if theme_info.get("sentiment", 0) <= 0:
            continue  # only positive themes are actionable
        
        # Get the set of industries related to this theme
        rel_inds = theme_to_industries.get(theme_name, set())
        
        # If no industry mapping found, skip this theme (can't reliably infer stocks)
        if not rel_inds:
            continue
        
        target_codes = [c for c, s in snap.items()
                      if s.get("industry", "") in rel_inds
                      and c not in zt_codes and c not in NONTRADABLE
                      and s.get("mktcap", 0) >= 2e9
                      and s.get("chg_pct", 0) < 9.0
                      and "ST" not in s.get("name", "") and "退" not in s.get("name", "")]

        if not target_codes:
            continue

        # Add theme_info field needed by score_hotspot_stock
        theme_info["name"] = theme_name

        for code in target_codes:
            s = snap[code]
            kline = get_kline(code)
            info = score_hotspot_stock(code, s, kline, theme_info, sector_scores, theme_to_industries)
            if info is None:
                continue
            info["theme"] = theme_name
            info["code"] = code
            info["name"] = s.get("name", "")
            all_results.append(info)
            results_by_theme[theme_name].append(info)

    # Sort within each theme
    for theme in results_by_theme:
        results_by_theme[theme].sort(key=lambda x: -x["score"])

    all_results.sort(key=lambda x: -x["score"])

    # Classify
    strong = [r for r in all_results if r["score"] >= 12]
    watch = [r for r in all_results if 6 <= r["score"] < 12]
    low = [r for r in all_results if r["score"] < 6]

    # Output JSON
    output = {
        "date": date_str,
        "ts": ts,
        "themes_processed": len(results_by_theme),
        "total_candidates": len(all_results),
        "strong_count": len(strong),
        "watch_count": len(watch),
        "strong": strong[:50],
        "watch": watch[:50],
        "by_theme": {},
    }
    for theme, stocks in results_by_theme.items():
        output["by_theme"][theme] = {
            "sentiment": themes[theme].get("sentiment", 0),
            "mainline_score": themes[theme].get("mainline_score", 0),
            "news_count": themes[theme].get("news_count", 0),
            "top3": stocks[:3],
        }

    os.makedirs(POOL_DIR, exist_ok=True)
    out_path = os.path.join(POOL_DIR, f"{date_str}.hotspot_infer.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Markdown report
    lines = [f"# 热点推演 {date_str} {ts}", "",
             f"- 热点主题: {len(themes)} | 有效(正向): {len(results_by_theme)} | 候选: {len(all_results)}",
             f"- 强烈买入 (score>=12): {len(strong)} | 关注 (6<=score<12): {len(watch)}",
             ""]

    if not themes:
        lines.append("## ⚠️ 未检测到热点数据")
        lines.append("")
        lines.append("请运行 `python3 tools/news_collect.py` 获取最新新闻，")
        lines.append("然后运行 `python3 tools/hotspot_analysis.py` 生成热点分析 JSON。")
        lines.append("")
    else:
        for theme_name, info in themes.items():
            if info.get("sentiment", 0) <= 0:
                continue
            stocks = results_by_theme.get(theme_name, [])
            if not stocks:
                continue
            lines.append(f"## 🔥 {theme_name} ( sentiment={info['sentiment']:+.0f}, news={info['news_count']}, mainline={info.get('mainline_score',0):.0f} )")
            lines.append("")
            lines.append("| 代码 | 名称 | 现价 | 涨% | 得分 | 行业 | 核心信号 |")
            lines.append("|------|------|------|-----|------|------|---------|")
            for s in stocks[:8]:
                sig = "; ".join(s["signals"][:3])
                lines.append(f"| {s['code']} | {s['name'][:6]} | {s['price']:.2f} | {s['chg_pct']:+.2f}% | **{s['score']:.0f}** | {s['industry'][:6]} | {sig} |")
            lines.append("")

        lines.append("## ⭐ 综合 TOP (跨主题最高分)")
        lines.append("")
        lines.append("| 代码 | 名称 | 现价 | 涨% | 得分 | 主题 | 行业 | 核心信号 |")
        lines.append("|------|------|------|-----|------|------|------|---------|")
        for r in strong[:20]:
            sig = "; ".join(r["signals"][:3])
            lines.append(f"| {r['code']} | {r['name'][:6]} | {r['price']:.2f} | {r['chg_pct']:+.2f}% | **{r['score']:.0f}** | {r['theme']} | {r['industry'][:6]} | {sig} |")
        lines.append("")

    lines.append("## 预测逻辑")
    lines.append("")
    lines.append("| 因子 | 权重 | 说明 |")
    lines.append("|------|------|------|")
    lines.append("| 主题热度 | 25% | 新闻条数 + 主线性得分 + 情感方向 |")
    lines.append("| 行业动量 | 20% | 板块今日涨跌 + 资金流向 + 涨停数 |")
    lines.append("| 技术位置 | 30% | 52w低位/均线金叉/RSI区域/量能确认 |")
    lines.append("| 主力资金 | 15% | 主力净流入确认 |")
    lines.append("| 今日涨幅 | 10% | 温和上涨最佳，暴涨回避 |")
    lines.append("")
    lines.append("### 上涨概率估算")
    lines.append("")
    lines.append("| 评分区间 | 明日上涨概率估计 | 操作建议 |")
    lines.append("|---------|---------------|---------|")
    lines.append("| ≥ 15 | 65-75% | 重点建仓，可追 |")
    lines.append("| 12-15 | 55-65% | 逢低买入，设置止损 |")
    lines.append("| 8-12 | 45-55% | 轻仓试水，快进快出 |")
    lines.append("| < 8 | <45% | 观望，等回调 |")
    lines.append("")
    lines.append("---")
    lines.append("*仅供研究，不构成投资建议。热点推演基于当日新闻+技术面，实际走势受多重因素影响。*")

    os.makedirs(ANA_DIR, exist_ok=True)
    report_path = os.path.join(ANA_DIR, f"hotspot_infer_{date_str}_{ts}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[hotspot_infer] Done: STRONG={len(strong)} WATCH={len(watch)} AVOID={len(low)}", flush=True)
    print(f"[hotspot_infer] -> {report_path}", flush=True)
    print(f"[hotspot_infer] -> {out_path}", flush=True)

    # Print summary
    print(f"\n=== 热点推演 TOP10 ===")
    for r in strong[:10]:
        print(f"  {r['code']} {r['name'][:6]} score={r['score']:.0f} theme={r['theme']} chg={r['chg_pct']:+.1f}% {' | '.join(r['signals'][:3])}")

    return output


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    run(date)
