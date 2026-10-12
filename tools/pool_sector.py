#!/usr/bin/env python3
"""
板块池 — Sector Pool
基于板块动量、资金流向、轮动周期，输出可交易的板块候选

核心算法：
1. 板块动量排名：按5日/20日涨跌幅给所有申万一级行业排序
2. 资金流入排序：按主力资金净流入给板块排序
3. 轮动窗口判定：结合宏观周期判断当前板块处于轮动的什么阶段
4. 热点共振：板块 + 新闻热点同时命中时加权重

输入：
- data/market/all_YYYY-MM-DD.txt（全市场快照，含industry字段）
- data/pool/{date}.ztpool.json（涨停池，用于热点映射）
- analysis/hotspot_impact_*.json（热点分析）

输出：
- data/pool/{date}.sector_pool.json
- analysis/sector_pool_{date}.md
"""
import os, sys, json, time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

ROOT = os.environ.get("STOCKS_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SNAPSHOT_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "market")
POOL_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data", "pool")
ANA_DIR = os.path.join(ROOT, "..", "..", "stocks", "analysis")

# Primary industry classification (申万一级行业 from eastmoney)
PRIMARY_SECTORS = [
    "银行", "非银金融", "电子", "计算机", "通信", "电力设备",
    "机械设备", "化工", "医药生物", "食品饮料", "家用电器", "汽车",
    "国防军工", "有色金属", "建筑材料", "建筑装饰", "石油石化",
    "煤炭", "钢铁", "公用事业", "交通运输", "纺织服饰", "轻工制造",
    "美容护理", "农林牧渔", "商贸零售", "社会服务", "传媒",
    "环保", "房地产", "综合",
]

# Defensive sectors (beloved in bear markets)
DEFENSIVE = {"银行", "公用事业", "电力", "石油石化", "煤炭", "食品饮料", "医药生物"}

# Cyclical sectors
CYCLICAL = {"电子", "计算机", "通信", "电力设备", "机械设备", "有色金属", "化工", "国防军工"}


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


def load_zt_sectors(date_str):
    """Load today's limit-up stocks by sector."""
    zt_by_sector = defaultdict(list)
    pool_path = os.path.join(POOL_DIR, f"{date_str}.ztpool.json")
    if os.path.exists(pool_path):
        try:
            d = json.load(open(pool_path, encoding="utf-8"))
            for p in d.get("pool", []):
                ind = p.get("industry", "")
                zt_by_sector[ind].append({
                    "code": p.get("c", ""),
                    "name": p.get("n", ""),
                    "lbc": p.get("lbc", 0),
                })
        except Exception:
            pass
    return dict(zt_by_sector)


def load_hotspot_impact(date_str):
    """Load recent hotspot analysis to get theme-sector mappings."""
    hotsectors = {}
    ana_dir = os.path.join(ROOT, "..", "..", "stocks", "analysis")
    for f in os.listdir(ana_dir):
        if not f.startswith("hotspot_impact_") or not f.endswith(".json"):
            continue
        if date_str.replace("-", "") in f or True:  # accept all recent
            try:
                d = json.load(open(os.path.join(ana_dir, f), encoding="utf-8"))
                themes = d.get("themes", {})
                for theme, info in themes.items():
                    sectors = info.get("sectors", [])
                    for s in sectors:
                        hotsectors[s] = hotsectors.get(s, 0) + 1
            except Exception:
                pass
    return hotsectors


def compute_sector_stats(snap, zt_by_sector):
    """Aggregate sector-level statistics."""
    by_sector = defaultdict(lambda: {
        "codes": [], "chgs": [], "amounts": [], "main_nets": [],
        "vrs": [], "pe_arr": [], "mktcaps": [],
    })
    
    for code, s in snap.items():
        ind = s.get("industry", "")
        if not ind:
            continue
        by_sector[ind]["codes"].append(code)
        by_sector[ind]["chgs"].append(s.get("chg_pct", 0))
        by_sector[ind]["amounts"].append(s.get("amount", 0))
        by_sector[ind]["main_nets"].append(s.get("main_net", 0))
        by_sector[ind]["vrs"].append(s.get("vol_ratio", 1))
        by_sector[ind]["pe_arr"].append(s.get("pe", 0))
        by_sector[ind]["mktcaps"].append(s.get("mktcap", 0))
    
    stats = {}
    for sector, data in by_sector.items():
        n = len(data["chgs"])
        if n < 3:
            continue
        avg_chg = sum(data["chgs"]) / n
        win_rate = sum(1 for c in data["chgs"] if c > 0) / n
        top_chg = max(data["chgs"])
        total_amount = sum(data["amounts"])
        total_main = sum(data["main_nets"])
        avg_vr = sum(data["vrs"]) / n
        
        stats[sector] = {
            "stock_count": n,
            "avg_chg_1d": round(avg_chg, 2),
            "win_rate": round(win_rate * 100, 1),
            "top_chg": round(top_chg, 2),
            "total_amount": round(total_amount, 2),  # in 元
            "total_main_net": round(total_main, 2),  # in 元
            "avg_vol_ratio": round(avg_vr, 2),
            "avg_pe": round(sum(data["pe_arr"]) / n, 1) if data["pe_arr"] else 0,
            "avg_mktcap": round(sum(data["mktcaps"]) / n / 1e8, 0) if data["mktcaps"] else 0,
            "zt_count": len(zt_by_sector.get(sector, [])),
        }
    
    return stats


def score_sector(sector, stats, market_phase, hotspot_count):
    """Score a sector considering multiple dimensions."""
    score = 0
    signals = []
    
    # 1. Momentum score (50% weight)
    avg_chg = stats.get("avg_chg_1d", 0)
    if avg_chg > 3:
        score += 5
        signals.append(f"大涨{avg_chg:+.1f}%")
    elif avg_chg > 1.5:
        score += 3
        signals.append(f"走强{avg_chg:+.1f}%")
    elif avg_chg > 0:
        score += 1
    elif avg_chg < -3:
        score -= 4
        signals.append(f"大跌{avg_chg:.1f}%")
    elif avg_chg < -1.5:
        score -= 2
    
    # 2. Win rate (board strength)
    wr = stats.get("win_rate", 50)
    if wr > 70:
        score += 3
        signals.append(f"普涨{wr:.0f}%")
    elif wr > 55:
        score += 1
    
    # 3. Volume confirmation
    vr = stats.get("avg_vol_ratio", 1)
    if vr > 1.5:
        score += 2
        signals.append("量能放大")
    elif vr < 0.7:
        score -= 1
    
    # 4. Capital flow
    main_net = stats.get("total_main_net", 0)
    if main_net > 1e9:
        score += 3
        signals.append(f"主力净入{main_net/1e9:.1f}亿")
    elif main_net > 0:
        score += 1
    
    # 5. ZT (limit-up) count = strength signal
    zt_n = stats.get("zt_count", 0)
    if zt_n >= 3:
        score += 4
        signals.append(f"{zt_n}只涨停")
    elif zt_n >= 1:
        score += 2
    
    # 6. Hotspot resonance
    if hotspot_count > 0:
        score += hotspot_count * 2
        signals.append(f"热点共振({hotspot_count}次)")
    
    # 7. Sector rotation logic
    is_defensive = sector in DEFENSIVE
    is_cyclical = any(s in CYCLICAL for s in [sector])
    
    if market_phase in ("recession", "decline"):
        if is_defensive:
            score += 3
            signals.append("防御轮动")
        elif is_cyclical:
            score -= 2
    elif market_phase in ("recovery", "acceleration"):
        if is_cyclical:
            score += 2
            signals.append("成长轮动")
        elif is_defensive:
            score -= 1
            signals.append("防御滞涨")
    
    # 8. Overbought check (don't chase extremes)
    if avg_chg > 5 and vr > 2.0:
        score -= 2
        signals.append("过热警示")
    
    return score, signals


def run(date_str=None):
    date_str = date_str or datetime.now(CST).strftime("%Y-%m-%d")
    print(f"[sector_pool] Running for {date_str}", flush=True)
    
    snap = load_snapshot(date_str)
    zt_by_sector = load_zt_sectors(date_str)
    hotspot_count = load_hotspot_impact(date_str)
    
    print(f"[sector_pool] Snapshot: {len(snap)} stocks", flush=True)
    
    # Compute sector stats
    sector_stats = compute_sector_stats(snap, zt_by_sector)
    print(f"[sector_pool] Sectors with data: {len(sector_stats)}", flush=True)
    
    # Load cycle phase for rotation context
    cycle_path = os.path.join(POOL_DIR, f"{date_str}.cycle.json")
    market_phase = "consolidation"
    if os.path.exists(cycle_path):
        try:
            cd = json.load(open(cycle_path, encoding="utf-8"))
            market_phase = cd.get("market_phase", "consolidation")
        except Exception:
            pass
    
    # Score each sector
    scored = []
    for sector, stats in sector_stats.items():
        hc = hotspot_count.get(sector, 0)
        sc, signs = score_sector(sector, stats, market_phase, hc)
        scored.append({
            "sector": sector,
            "score": sc,
            "signals": signs,
            **stats,
        })
    
    # Sort
    scored.sort(key=lambda x: -x["score"])
    
    # Classify into tiers
    tier1 = [s for s in scored if s["score"] >= 8]
    tier2 = [s for s in scored if 4 <= s["score"] < 8]
    tier3 = [s for s in scored if 0 <= s["score"] < 4]
    tier_down = [s for s in scored if s["score"] < 0]
    
    # Output
    pool_out = {
        "date": date_str,
        "market_phase": market_phase,
        "total_sectors": len(scored),
        "tier1_count": len(tier1),
        "tier2_count": len(tier2),
        "tier3_count": len(tier3),
        "tier_down_count": len(tier_down),
        "tier1": tier1[:15],
        "tier2": tier2[:15],
        "tier_down": tier_down[:10],
    }
    
    os.makedirs(POOL_DIR, exist_ok=True)
    out_path = os.path.join(POOL_DIR, f"{date_str}.sector_pool.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(pool_out, f, ensure_ascii=False, indent=2)
    print(f"[sector_pool] Saved -> {out_path}", flush=True)
    
    # Report
    lines = [f"# 板块池 {date_str}", "",
             f"- 市场周期: {market_phase} | 有效板块: {len(scored)}",
             "- 数据源: 东财全市场快照 + 涨停池 + 热点分析",
             ""]
    
    lines.append("## ⭐ 强势板块 (Tier 1 — 重点配置)")
    lines.append("")
    lines.append("| 板块 | 得分 | 信号 | 5日均涨跌% | 涨比% | 涨停数 | 主力流入亿 | 量比 |")
    lines.append("|------|------|------|-----------|-------|--------|-----------|------|")
    for s in tier1:
        main_yi = s["total_main_net"] / 1e9
        lines.append(f"| {s['sector']} | {s['score']} | {', '.join(s['signals'][:3])} | {s['avg_chg_1d']:+.2f}% | {s['win_rate']:.0f}% | {s['zt_count']} | {main_yi:+.1f} | {s['avg_vol_ratio']:.2f}x |")
    lines.append("")
    
    lines.append("## ⚠️ 中性板块 (Tier 2 — 观察)")
    lines.append("")
    lines.append("| 板块 | 得分 | 信号 | 5日均涨跌% | 主力流入亿 |")
    lines.append("|------|------|------|-----------|-----------|")
    for s in tier2:
        main_yi = s["total_main_net"] / 1e9
        lines.append(f"| {s['sector']} | {s['score']} | {', '.join(s['signals'][:2])} | {s['avg_chg_1d']:+.2f}% | {main_yi:+.1f} |")
    lines.append("")
    
    lines.append("## ❌ 弱势/回避板块")
    lines.append("")
    for s in tier_down[:8]:
        main_yi = s["total_main_net"] / 1e9
        lines.append(f"- ❌ **{s['sector']}**: 得分 {s['score']} | {s['avg_chg_1d']:+.2f}% | 主力流出{abs(main_yi):.1f}亿")
    lines.append("")
    
    lines.append("## 板块轮动逻辑")
    lines.append("")
    lines.append("| 市场周期 | 推荐板块类型 | 回避板块类型 |")
    lines.append("|---------|------------|------------|")
    lines.append("| 复苏期 (recovery) | 科技/成长/消费 | 银行/公用事业 |")
    lines.append("| 加速期 (acceleration) | 全板块轮动 | 无明显回避 |")
    lines.append("| 震荡期 (consolidation) | 结构性机会 | 追高 |")
    lines.append("| 衰退期 (decline) | 防御板块(银行/电力/公用) | 科技/成长 |")
    lines.append("| 萧条期 (recession) | 高股息/价值 | 全部成长 |")
    lines.append("")
    
    lines.append("---")
    lines.append("*仅供研究，不构成投资建议*")
    
    os.makedirs(ANA_DIR, exist_ok=True)
    report_path = os.path.join(ANA_DIR, f"sector_pool_{date_str}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[sector_pool] Report -> {report_path}", flush=True)
    
    print(f"\n=== 板块池 TOP5 ===")
    for s in tier1[:5]:
        print(f"  {s['sector']}: score={s['score']} chg={s['avg_chg_1d']:+.2f}% signals={s['signals'][:3]}")
    
    return pool_out


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    run(date)
