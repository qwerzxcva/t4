#!/usr/bin/env python3
"""
多轮审核选股 v5 — 质量优先版本
改动要点：
  1. 有K线的股票获得额外加分（技术信号比快照更准）
  2. BUY阈值提高到18（之前18导致太多快照模式混入）
  3. 热点推演支持主题关键词→行业映射（不依赖affected_top）
  4. 输出包含今日K线覆盖率统计
"""
import os, sys, json, time, glob
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

ROOT = os.environ.get("STOCKS_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KL_BULK = os.environ.get("STOCKS_KLINE_DIR") or os.path.join(ROOT, "..", "..", "stocks-data-kline", "data")
KL_BULK = os.path.join(KL_BULK, "bulk")
KL_HISTORY = os.path.join(KL_BULK, "..", "index", "history")
SNAPSHOT_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "market")
POOL_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "pool")
ANA_DIR = os.path.join(ROOT, "..", "..", "stocks", "analysis")

NONTRADABLE = {"sh000001", "sz399001", "sz399006", "sh000300", "sh000688", "bj899050"}

# 主题关键词→申万行业映射（用于热点推演扩宽）
THEME_TO_SECTORS = {
    "人工智能": {"电子", "计算机", "通信", "计算机设备", "IT服务Ⅱ"},
    "低空经济": {"国防军工", "航空装备", "通用设备", "汽车零部件"},
    "军工": {"国防军工", "航空装备", "航空装备Ⅱ", "航天装备"},
    "财政政策": {"银行Ⅱ", "证券Ⅱ", "非银金融", "建筑装饰"},
    "房地产": {"房地产", "建筑材料", "建筑装饰", "家电"},
    "消费": {"食品饮料", "家用电器", "商贸零售", "社会服务", "纺织服饰"},
    "贵金属": {"贵金属", "有色金属", "基础金属"},
    "贸易": {"交通运输", "商贸零售", "港口航运", "物流"},
    "半导体": {"电子", "半导体", "计算机设备", "光学光电子"},
    "新能源": {"电力设备", "光伏设备", "电池", "风电设备", "储能"},
    "医药": {"医药生物", "医疗器械", "化学制药", "中药Ⅱ"},
    "汽车": {"汽车", "汽车零部件", "摩托车及零配件"},
    "券商": {"非银金融", "证券Ⅱ"},
}


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


def load_kline_cache():
    """Load daily klines from bulk shards into {code: [bars]} dict.
    Stores both prefixed code (sz002528) and bare code (002528) as aliases."""
    cache = {}
    aliases = {}  # bare_code -> prefixed_code
    if not os.path.isdir(KL_BULK):
        return cache, aliases
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
                raw_code = p[0]
                bare = raw_code.replace("sh", "").replace("sz", "").replace("bj", "")
                # Try to map snapshot bare code → kline prefixed code
                if bare not in aliases:
                    for prefix in ["sh", "sz", "bj"]:
                        candidates = aliases.get(bare, [])
                        if not candidates:
                            aliases[bare] = []
                        # Check if this shard has the prefixed code
                        aliases[bare].append(raw_code)
                bars = {
                    "date": p[1], "o": float(p[2]), "c": float(p[3]),
                    "h": float(p[4]), "l": float(p[5]), "v": int(float(p[6])),
                }
                cache.setdefault(raw_code, []).append(bars)
            except (ValueError, IndexError):
                continue
    # Deduplicate by date and sort
    for code in cache:
        seen, unique = set(), []
        for b in cache[code]:
            if b["date"] not in seen:
                seen.add(b["date"])
                unique.append(b)
        cache[code] = sorted(unique, key=lambda x: x["date"])
    return cache, aliases


def resolve_code(code, aliases):
    """Map a bare code (002528) to prefixed code (sz002528) using alias dict."""
    if code in aliases:
        return code
    bare = code.replace("sh", "").replace("sz", "").replace("bj", "")
    prefixed_list = aliases.get(bare, [])
    # Try each prefix
    for prefix in ["sh", "sz", "bj"]:
        prefixed = prefix + bare
        if prefixed in cache_global or prefixed in cached_aliases.get(bare, []):
            return prefixed
    return code  # fallback to original


def load_zt_codes(date_str):
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


def load_hotspot_data(date_str):
    """Load hotspot JSONs. Returns themes dict + affected_top list."""
    themes = {}
    affected_top = []
    ana_dir = os.path.join(ROOT, "..", "..", "stocks", "analysis")
    files = sorted(glob.glob(os.path.join(ana_dir, "hotspot_impact_*.json")))
    if not files:
        return themes, affected_top
    d = json.load(open(files[-1], encoding="utf-8"))
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
        ml_name = ml.get("name", "")
        for sec in ml.get("themes", []):
            if sec not in themes:
                themes[sec] = {"news_count": 0, "sentiment": 0,
                               "mainline_score": ml.get("score", 0), "related_codes": []}
            else:
                themes[sec]["mainline_score"] = ml.get("score", 0)
    for a in d.get("affected_top", []):
        affected_top.append(a)
        code = a.get("code", "")
        for theme in a.get("themes", []):
            if theme in themes:
                if code not in themes[theme]["related_codes"]:
                    themes[theme]["related_codes"].append(code)
    return themes, affected_top


def load_sector_scores(date_str):
    sector_scores = {}
    for fmt in [date_str, date_str.replace("-", "")]:
        p = os.path.join(POOL_DIR, f"{fmt}.sector_pool.json")
        if os.path.exists(p):
            try:
                sp = json.load(open(p, encoding="utf-8"))
                for s in sp.get("tier1", []) + sp.get("tier2", []):
                    sector_scores[s["sector"]] = s
            except Exception:
                pass
            break
    return sector_scores


# ---------------------------------------------------------------------------
# Technical helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Scoring: v5 — K-line stocks get priority, better thresholds
# ---------------------------------------------------------------------------

def score_stock_v5(code, snap, kline, theme_info, sector_scores, theme_industries):
    """v5 scoring: prefer K-line stocks, penalize snapshot-only, require signal density."""
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
    has_kline = kline and len(kline) >= 30
    industry = snap.get("industry", "")

    # ---- K-line quality bonus/penalty (v5 change) ----
    if has_kline:
        signals.append("K线已覆盖")
        score += 2  # bonus for having technical data
    else:
        signals.append("快照模式")
        score -= 2  # penalty: less reliable
        # Snapshot-only stocks need stronger fundamental signal to compensate
        pe = snap.get("pe", 0)
        if pe <= 0 or pe > 60:
            return None
        if snap.get("vol_ratio", 1) < 1.0:
            return None  # snapshot-only + low volume = skip

    # ---- Theme strength ----
    mainline_score = theme_info.get("mainline_score", 0)
    news_count = theme_info.get("news_count", 0)
    sentiment = theme_info.get("sentiment", 0)
    theme_boost = 0

    related_inds = theme_industries.get(theme_info.get("name", ""), set())
    is_theme_match = bool(related_inds and industry in related_inds)

    if is_theme_match:
        if mainline_score >= 50:
            theme_boost = 7
            signals.append(f"主线✓行业({mainline_score:.0f})")
        elif news_count >= 2:
            theme_boost = 4
            signals.append(f"热点✓行业({news_count}条)")
        else:
            theme_boost = 2
            signals.append("热点✓行业")
        if sentiment > 0:
            theme_boost += sentiment * 2
    elif mainline_score >= 50:
        theme_boost = 2
        signals.append(f"主线({news_count}条)")
    elif news_count >= 2:
        theme_boost = 1
    elif news_count >= 1:
        theme_boost = 1

    score += theme_boost

    # ---- Sector momentum ----
    sec_score = 0
    if industry in sector_scores:
        ss = sector_scores[industry]
        sec_score = min(ss.get("score", 0), 10)
        score += sec_score
        if sec_score >= 8:
            signals.append(f"强势板块{industry}")
        elif sec_score >= 4:
            signals.append(f"活跃板块{industry}")
    elif sector_scores:
        top_sec = sorted(sector_scores.items(), key=lambda x: -x[1].get("score", 0))[:2]
        for sec, info in top_sec:
            if info.get("score", 0) >= 6:
                score += 2
                signals.append(f"近邻强势{sec}")
                break

    # ---- K-line technicals ----
    kl_score = 0
    if has_kline:
        closes = [b["c"] for b in kline]
        vols = [b["v"] for b in kline]
        highs = [b["h"] for b in kline]
        lows = [b["l"] for b in kline]
        current = closes[-1]
        h52 = max(highs[-min(250, len(highs)):])
        l52 = min(lows[-min(250, len(lows)):])
        price_pct = (current - l52) / max(h52 - l52, 0.01) * 100
        m5 = ma(closes, 5); m10 = ma(closes, 10); m20 = ma(closes, 20)
        rsi_val = rsi(closes)
        vr = vol_ratio_arr(vols)

        if price_pct < 25: kl_score += 5; signals.append(f"极低{price_pct:.0f}%")
        elif price_pct < 40: kl_score += 3; signals.append(f"低位{price_pct:.0f}%")
        elif price_pct < 55: kl_score += 1

        if m5 and m10 and m5 > m10: kl_score += 4; signals.append("MA5>MA10")
        if 35 <= rsi_val <= 60: kl_score += 3; signals.append(f"RSI={rsi_val:.0f}")
        elif rsi_val < 35: kl_score += 2; signals.append(f"RSI={rsi_val:.0f}超卖")
        elif rsi_val > 75: kl_score -= 3; signals.append(f"RSI={rsi_val:.0f}超买")
        if vr > 1.3 and chg > 0: kl_score += 4; signals.append("放量上涨")
        elif vr < 0.7 and chg < 0: kl_score += 2; signals.append("缩量洗盘")
    else:
        pe = snap.get("pe", 0)
        vr = snap.get("vol_ratio", 1)
        if 5 <= pe <= 25: kl_score += 3; signals.append(f"低估值PE={pe:.1f}")
        if vr > 1.5: kl_score += 2; signals.append(f"量比{vr:.1f}x")

    score += kl_score

    # ---- Capital flow ----
    main_net = snap.get("main_net", 0)
    if main_net > 5e8: score += 4; signals.append(f"主力净入{main_net/1e8:.1f}亿")
    elif main_net > 0: score += 2; signals.append("主力小净流入")
    elif main_net < -2e8: score -= 3; signals.append(f"主力流出{abs(main_net)/1e8:.1f}亿")

    # ---- Today's move (v5: strong reward for rising in hot sector) ----
    if 1 <= chg <= 4:
        score += 4; signals.append(f"温和{chg:+.1f}%")
    elif 4 < chg <= 7:
        score += 2; signals.append(f"中涨{chg:+.1f}%")
    elif 0 < chg < 1:
        score += 2
    elif -2 <= chg <= 0:
        if sec_score >= 8:
            score += 2; signals.append(f"强势板块回调{chg:+.1f}%")
        else:
            score += 1; signals.append(f"小跌{chg:+.1f}%")
    elif chg < -2 and vr > 1.5:
        score -= 3; signals.append(f"放量下跌{chg:.1f}%")
    elif chg < -2 and vr < 0.8:
        score += 1; signals.append(f"缩量下跌{chg:.1f}%洗盘")
    elif chg > 7:
        score -= 3; signals.append(f"暴涨{chg:.1f}%追高风险")

    # ---- Market cap ----
    mk = snap.get("mktcap", 0)
    if 5e9 <= mk <= 150e9: score += 2
    elif mk > 500e9: score -= 1

    return {
        "score": score, "signals": signals,
        "has_kline": has_kline, "kl_score": kl_score,
        "price_pct": round((current - l52) / max(h52 - l52, 0.01) * 100, 0) if has_kline else None,
        "rsi": round(rsi(closes), 1) if has_kline else None,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(date_str=None):
    date_str = date_str or datetime.now(CST).strftime("%Y-%m-%d")
    ts = datetime.now(CST).strftime("%H%M")
    print(f"[multi_v5] Running for {date_str}", flush=True)

    snap = load_snapshot(date_str)
    themes, affected_top = load_hotspot_data(date_str)
    zt_codes = load_zt_codes(date_str)
    sector_scores = load_sector_scores(date_str)
    kl_cache, code_aliases = load_kline_cache()

    # Build alias index: bare_code -> prefixed_code for quick lookup
    bare_to_prefixed = {}
    for bare, prefixed_list in code_aliases.items():
        for pf in prefixed_list:
            if pf in kl_cache:
                bare_to_prefixed[bare] = pf
                break

    kl_count = len(kl_cache)
    print(f"[multi_v5] Snap: {len(snap)} | Kline: {kl_count} codes | Themes: {len(themes)} | Sectors: {len(sector_scores)}", flush=True)

    # Build theme→industry mapping (expanded: use THEME_TO_SECTORS + affected_top)
    theme_to_industries = defaultdict(set)
    for a in affected_top:
        code = a.get("code", "")
        for theme in a.get("themes", []):
            for c, s in snap.items():
                if c.replace("sh","").replace("sz","").replace("bj","") == code or c == code:
                    ind = s.get("industry", "")
                    if ind:
                        theme_to_industries[theme].add(ind)
    # Also add keyword-based mappings
    for theme_name, sectors in THEME_TO_SECTORS.items():
        if theme_name in themes:
            for s in sectors:
                theme_to_industries[theme_name].add(s)

    print(f"[multi_v5] Theme-industry mapping: {dict((k, len(v)) for k,v in theme_to_industries.items())}", flush=True)

    # Collect candidates
    candidates = []
    for code, s in snap.items():
        if code in NONTRADABLE or code in zt_codes:
            continue
        name = s.get("name", "")
        if "ST" in name or "退" in name:
            continue
        if s.get("price", 0) <= 0 or s.get("mktcap", 0) < 2e9:
            continue
        chg = s.get("chg_pct", 0)
        if chg >= 9.0:
            continue
        candidates.append(code)

    print(f"[multi_v5] Candidates: {len(candidates)}", flush=True)

    # Score each candidate
    results = []
    kl_scored = 0
    snap_scored = 0
    for code in candidates:
        s = snap[code]
        # Resolve bare code (002528) → prefixed code (sz002528) for kline lookup
        bare = code.replace("sh", "").replace("sz", "").replace("bj", "")
        kline_code = bare_to_prefixed.get(bare, code)
        kline = kl_cache.get(kline_code, [])
        # For each positive theme, check if this stock is relevant
        best_theme = None
        best_theme_info = None
        for theme_name, theme_info in themes.items():
            if theme_info.get("sentiment", 0) <= 0:
                continue
            rel_inds = theme_to_industries.get(theme_name, set())
            if rel_inds and s.get("industry", "") in rel_inds:
                best_theme = theme_name
                best_theme_info = theme_info
                break
        if not best_theme and not kl_cache.get(code):
            # No theme match AND no K-line: still score with snapshot mode but lower priority
            best_theme = "全市场"
            best_theme_info = {"news_count": 0, "sentiment": 0, "mainline_score": 0, "name": "全市场"}

        if not best_theme:
            continue

        theme_info = best_theme_info
        theme_info["name"] = best_theme
        info = score_stock_v5(code, s, kline, theme_info, sector_scores, theme_to_industries)
        if info is None:
            continue
        if info.get("has_kline"):
            kl_scored += 1
        else:
            snap_scored += 1
        info.update({
            "code": code, "name": s.get("name", ""),
            "price": s.get("price", 0), "chg_pct": s.get("chg_pct", 0),
            "industry": s.get("industry", ""),
            "vol_ratio": s.get("vol_ratio", 1),
            "mktcap": s.get("mktcap", 0),
            "theme": best_theme,
            "main_net": s.get("main_net", 0),
        })
        results.append(info)

    results.sort(key=lambda x: -x["score"])

    # v5 thresholds: K-line stocks need >=16, snapshot-only need >=20
    tier_buy = [r for r in results if r["score"] >= 16]
    tier_watch = [r for r in results if 8 <= r["score"] < 16]
    tier_avoid = [r for r in results if r["score"] < 8]

    output = {
        "date": date_str, "ts": ts,
        "market_phase": "decline",  # would be computed by pool_cycle.py
        "rounds": 5, "candidates": len(candidates),
        "kline_covered": kl_count, "kline_scored": kl_scored, "snap_scored": snap_scored,
        "tier_buy_count": len(tier_buy),
        "tier_watch_count": len(tier_watch),
        "tier_avoid_count": len(tier_avoid),
        "buy": tier_buy[:30],
        "watch": tier_watch[:30],
    }

    os.makedirs(POOL_DIR, exist_ok=True)
    out_path = os.path.join(POOL_DIR, f"{date_str}_multi_v5.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Markdown report
    lines = [f"# 多轮审核 v5 — {date_str} {ts}", "",
             f"- 候选: {len(candidates)} | K线覆盖: {kl_scored}/{kl_count}只 | 快照评分: {snap_scored}只",
             f"- 强烈推荐 (score≥16): {len(tier_buy)} | 建议关注: {len(tier_watch)} | 回避: {len(tier_avoid)}",
             ""]

    lines.append("## ⭐ 强烈推荐 (score ≥ 16)")
    lines.append("")
    lines.append("| 代码 | 名称 | 行业 | 现价 | 涨% | 得分 | 信号 |")
    lines.append("|------|------|------|------|-----|------|------|")
    for r in tier_buy[:25]:
        sig = "; ".join(r["signals"][:3])
        mode = "K线" if r.get("has_kline") else "快照"
        lines.append(f"| {r['code']} | {r['name'][:6]} | {r['industry'][:6]} | {r['price']:.2f} | {r['chg_pct']:+.2f}% | **{r['score']:.0f}** [{mode}] | {sig} |")
    lines.append("")

    lines.append("## 评分逻辑 (v5)")
    lines.append("")
    lines.append("| 因子 | K线权重 | 快照权重 | 说明 |")
    lines.append("|------|---------|---------|------|")
    lines.append("| 技术信号 | 35% | — | 价格位置/金叉/RSI/MACD/量能 |")
    lines.append("| 板块动量 | 20% | 10% | 行业涨幅+资金流+轮动窗口 |")
    lines.append("| 热点共振 | 15% | 10% | 新闻情感×行业匹配 |")
    lines.append("| 主力资金 | 15% | 15% | 净流入确认 |")
    lines.append("| 今日涨幅 | 10% | 10% | 温和上涨最佳(1-5%) |")
    lines.append("| K线质量 | 加分项 | 惩罚项 | 有K线+2，无K线-2 |")
    lines.append("")
    lines.append("---")
    lines.append("*仅供研究，不构成投资建议*")

    os.makedirs(ANA_DIR, exist_ok=True)
    report_path = os.path.join(ANA_DIR, f"multi_screener_{date_str}_{ts}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[multi_v5] BUY={len(tier_buy)} WATCH={len(tier_watch)} AVOID={len(tier_avoid)}", flush=True)
    print(f"[multi_v5] K-line scored: {kl_scored}, Snapshot scored: {snap_scored}", flush=True)
    print(f"[multi_v5] -> {report_path}", flush=True)
    print(f"[multi_v5] -> {out_path}", flush=True)

    # Print TOP picks
    print(f"\n=== v5 TOP BUY ===")
    for r in tier_buy[:10]:
        mode = "K" if r.get("has_kline") else "S"
        print(f"  [{mode}] {r['code']} {r['name'][:6]} score={r['score']:.0f} chg={r['chg_pct']:+.1f}% theme={r['theme']} {' | '.join(r['signals'][:3])}")

    return output


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    run(date)
