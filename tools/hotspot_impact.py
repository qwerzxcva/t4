#!/usr/bin/env python3
"""
热点事件 → 个股影响分析 (TSP 事件传播框架 v3)
复用 ai_screener v3 的抓取层 (新浪7x24 主 + 东财/财联社备 + 远端 data-news 回退),
东财概念资金流 WAF 不稳定时自动降级为涨停池主题聚类。
输出: analysis/hotspot_impact_YYYYMMDD_HHMM.{json,md} + data-live/data/hotspot_impact/latest.json
用法: python3 hotspot_impact.py --out-dir /path [--top 20] [--limit 120] [--days 3]
"""
import argparse, datetime, json, os, pathlib, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai_screener import (fetch_indices, fetch_pool, fetch_concepts, fetch_news,
                         fetch_remote_news, fetch_rps, market_phase,
                         mainline_rank, theme_of, rank_norm, prefixed, THEMES)


def build_report(top=20, limit=120, days=3):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    date_str = now.strftime("%Y%m%d")
    ydate = (now - datetime.timedelta(days=1)).strftime("%Y%m%d")

    zt = fetch_pool("ZT", date_str)
    dt = fetch_pool("DT", date_str)
    zb = fetch_pool("ZB", date_str)
    yzt = fetch_pool("ZT", ydate)
    concepts = fetch_concepts(150)
    news = fetch_news(limit)
    if len(news) < 30:
        news += fetch_remote_news(days=days)

    theme_news = {}
    for n in news:
        for t in n["theme"]:
            theme_news.setdefault(t, {"count": 0, "pos": 0, "neg": 0, "sample": []})
            theme_news[t]["count"] += 1
            if n["sentiment"] == "pos":
                theme_news[t]["pos"] += 1
            elif n["sentiment"] == "neg":
                theme_news[t]["neg"] += 1
            if len(theme_news[t]["sample"]) < 3:
                theme_news[t]["sample"].append(n["title"][:50])
    hot_themes = sorted(theme_news, key=lambda t: theme_news[t]["count"], reverse=True)

    phase = market_phase(zt, dt, zb, yzt, fetch_indices())
    mainlines = mainline_rank(concepts, hot_themes[:10], zt)

    # 涨停股 RPS (top 40)
    pool_codes = [z["code"] for z in zt[:40]]
    rps = fetch_rps(pool_codes, limit=40)
    r5 = rank_norm({k: v["chg5"] for k, v in rps.items()})
    r20 = rank_norm({k: v["chg20"] for k, v in rps.items()})

    affected = []
    for z in zt[:top * 2]:
        hits = sorted(theme_of(z["name"]) & set(hot_themes))
        affected.append({
            "code": z["code"], "name": z["name"], "lianban": z["lianban"],
            "zt_time": z["zt_time"], "chg_pct": z["chg_pct"],
            "rps5": round(r5.get(z["code"], 0.5) * 100),
            "rps20": round(r20.get(z["code"], 0.5) * 100),
            "themes": hits, "news_refs": [s for s in theme_news.get(hits[0], {}).get("sample", [])][:2] if hits else [],
        })
    affected.sort(key=lambda x: (bool(x["themes"]), x["lianban"], x["rps5"]), reverse=True)

    return {
        "ts": now.isoformat(),
        "date": date_str,
        "market": {"limit_up": len(zt), "limit_down": len(dt), "broken": len(zb),
                    "seal_rate": round(len(zt) / max(1, len(zt) + len(zb)) * 100, 1)},
        "news_count": len(news),
        "themes_detected": [{"name": t, **{k: theme_news[t][k] for k in ("count", "pos", "neg")}} for t in hot_themes],
        "phase": phase,
        "mainlines": mainlines[:15],
        "concept_source": "eastmoney-clist" if concepts else "zt-theme-cluster (concept API degraded)",
        "affected_top": affected[:top],
        "news_recent": news[:60],
        "meta": {"algo": "tsp-signal-v3", "ref": "shy3130/tick-stock-panel", "zero_cred": True},
    }


def render_md(r):
    ts = r["ts"][:16].replace("T", " ")
    lines = [f"# 热点影响分析 {ts} CST", ""]
    ph = r["phase"]
    lines += [f"- 新闻 {r['news_count']} 条 | 命中主题 {len(r['themes_detected'])} 个 | 情绪阶段: **{ph['stage']}** ({ph['score']}分) | 梯队高度 {ph['max_boards']} 板 | 晋级率 {ph['promo_rate']:.0%}",
              f"- 涨停 {r['market']['limit_up']} / 跌停 {r['market']['limit_down']} / 炸板 {r['market']['broken']} / 封板率 {r['market']['seal_rate']}% · 概念数据源: {r['concept_source']}", ""]
    lines += ["## TOP 热点主题", "", "| 主题 | 新闻数 | 正向 | 负向 | 样本 |", "|------|------|------|------|------|"]
    for t in r["themes_detected"][:12]:
        sample = " / ".join(t.get("sample", [])[:2])[:60]
        lines.append(f"| {t['name']} | {t['count']} | {t['pos']} | {t['neg']} | {sample} |")
    lines += ["", "## 受影响标的 (热点关联 + 连板 + RPS)", "",
              "| 代码 | 名称 | 连板 | 热点关联 | RPS5 | RPS20 | 首封时间 |",
              "|------|------|------|----------|------|-------|----------|"]
    for a in r["affected_top"]:
        lines.append(f"| {a['code']} | {a['name']} | {a['lianban']} | {','.join(a['themes']) or '—'} "
                     f"| {a['rps5']} | {a['rps20']} | {a['zt_time']} |")
    lines += ["", "## 主线排行", "", "| 主线 | 热度分 | 领涨 | 关联主题 |", "|------|--------|------|----------|"]
    for m in r["mainlines"][:10]:
        lines.append(f"| {m['name']} | {m['score']} | {m.get('leading_name', '—')} | {','.join(m.get('themes', [])) or '—'} |")
    lines += ["", "---", f"*生成: {ts} CST · TSP 信号合成 v3 · 借鉴 shy3130/tick-stock-panel · 仅供研究, 不构成投资建议*"]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    ts = now.strftime("%Y%m%d_%H%M")
    report = build_report(top=args.top, limit=args.limit, days=args.days)

    out_dir = pathlib.Path(args.out_dir) if args.out_dir else pathlib.Path(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "..", "stocks", "analysis"))
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"hotspot_impact_{ts}.json"
    out_md = out_dir / f"hotspot_impact_{ts}.md"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_md(report), encoding="utf-8")
    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[output] {out_json}\n[output] {out_md}", flush=True)


if __name__ == "__main__":
    main()
