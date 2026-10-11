#!/usr/bin/env python3
"""
热点事件 → 个股影响分析 (TSP 事件传播框架 v2)
- 新闻源：东方财富 7x24（实时滚动）+ 新浪 7x24（备选）
- 概念资金流：东财 push2cl (m:90+t:2+f:!50) + eastmoney 板块资金 (stock_sector_fund_flow)
- 个股打分：板块热度 × 个股量能 × 主力净流入
输出：analysis/hotspot_impact_YYYYMMDD_HHMM.{json,md} + data-live/data/hotspot_impact/latest.json
用法: STOCKS_NEWS_DIR=... STOCKS_LIVE_DIR=... python3 hotspot_impact.py [--days 1] [--top 20]
"""
import argparse, datetime, json, os, sys, urllib.request, pathlib, re

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
      "Referer": "https://finance.eastmoney.com/"}
SEEN = set()

# ─── 关键词 → 主题 (可扩展) ──────────────────────────────────────────────
KEYWORD_THEME = {
    "降准|降息|MLF|逆回购|LPR": "货币政策",
    "关税|贸易战|制裁|出口管制": "贸易摩擦",
    "稀土|战略矿产|镓|锗": "战略资源",
    "设备更新|以旧换新": "内需刺激",
    "半导体|芯片|国产替代|卡脖子|光刻机": "半导体自主",
    "AI|大模型|算力|数据中心|GPU|英伟达|NVDA|deepseek": "人工智能",
    "低空|eVTOL|飞行汽车|无人机": "低空经济",
    "人形机器人|机器人|工业母机": "机器人",
    "固态电池|锂电池|新能源车|新能源汽车": "新能源",
    "光伏|风电|储能|氢能": "新能源",
    "黄金|白银|贵金属|避险": "贵金属",
    "创新药|医药|中药|疫苗|CRO|恒瑞|药明": "医药",
    "白酒|消费|家电|海尔|格力": "大消费",
    "券商|牛市|注册制|投行": "券商",
    "地产|保障房|限购|房地产": "房地产",
    "军工|国防|航母|歼": "军工",
    "华为|鸿蒙|荣耀": "华为链",
    "苹果|果链": "苹果链",
    "中字头|国企改革|央企": "国企改革",
    "利好|大涨|涨停|超预期": "正向",
    "利空|大跌|跌停|不及预期": "负向",
}
_KW_RE = re.compile("|".join(KEYWORD_THEME.keys()), re.IGNORECASE)

def theme_of(text):
    """Return set of themes present in text."""
    return {KEYWORD_THEME[k] for k in KEYWORD_THEME if k.lower() in text.lower()}

# ─── 新闻采集 (多源，容错) ────────────────────────────────────────────────
def fetch_eastmoney_news(count=80):
    """东方财富 7x24 滚动 (金融/政策/产业新闻). Returns [(date, title, url, content)]."""
    # em 7x24: https://np-listapi.eastmoney.com/comm/wap/getListInfo?type=8&pageSize=N&pageNo=1
    # type=8 是 7x24 财经, type=11 是政策, type=12 是产经
    results = []
    for ntype, label in [(8, "财经"), (11, "政策"), (12, "产经")]:
        url = (f"https://np-listapi.eastmoney.com/comm/wap/getListInfo"
               f"?type={ntype}&pageSize={count}&pageNo=1"
               f"&client=web&source=XHH&channel=web")
        try:
            body = urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=12).read()
            d = json.loads(body.decode("utf-8", errors="replace"))
            items = (d or {}).get("data") or (d or {}).get("list") or []
            if not items and isinstance(d, dict):
                items = d.get("data", {}).get("list", []) if isinstance(d.get("data"), dict) else []
            for it in items[:count//3]:
                title = str(it.get("title", "")).strip()
                digest = str(it.get("digest", it.get("content", "")))[:200]
                url = str(it.get("url", it.get("urlStr", "")))
                dt = str(it.get("showtime", it.get("ctime", "")))[:16]
                if not title or title in SEEN:
                    continue
                SEEN.add(title)
                results.append((dt, title, url, digest))
        except Exception as e:
            print(f"[em news] err: {e}", file=sys.stderr)
    return results

def fetch_sina_news(count=60):
    """新浪 7x24 (备用源). Returns same format."""
    url = f"https://app.cj.sina.com.cn/api/news/pc?page=1&num={count}"
    try:
        body = urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=12).read()
        d = json.loads(body.decode("utf-8", errors="replace"))
        items = (d or {}).get("data") or []
        for it in items[:count//2]:
            title = str(it.get("title", "")).strip()
            digest = str(it.get("digest", ""))[:200]
            dt = str(it.get("ctime", "")[:16])
            if not title or title in SEEN:
                continue
            SEEN.add(title)
            results.append((dt, title, "", digest))
    except Exception as e:
        print(f"[sina news] err: {e}", file=sys.stderr)
    return results

# ─── 东财概念资金流 (多端点容错) ──────────────────────────────────────────
def fetch_concept_flow():
    """东财概念板块资金流 (push2 clist m:90+t:2). Returns list of dicts."""
    results = []
    # primary: 概念板块排行 (按涨跌幅)
    url1 = ("https://push2.eastmoney.com/api/qt/clist/get"
            "?pn=1&pz=120&po=1&np=1&fltt=2&invt=2"
            "&fid=f3&fs=m:90+t:2+f:!50"
            "&fields=f12,f14,f2,f3,f4,f5,f6,f7,f12,f13,f14,f15,f16,f17,f18")
    for url in [url1]:
        try:
            body = urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=12).read()
            d = json.loads(body.decode("utf-8", errors="replace"))
            diff = (d or {}).get("data") or {}
            for item in (diff.get("diff") or []):
                results.append({
                    "code": str(item.get("f12", "")),
                    "name": str(item.get("f14", "")),
                    "price": float(item.get("f2") or 0),
                    "chg_pct": float(item.get("f3") or 0),
                    "vol": float(item.get("f5") or 0),
                    "amount": float(item.get("f6") or 0),
                    "turnover": float(item.get("f8") or 0),
                    "main_net_in": float(item.get("f10") or 0),
                    "pct_chg_5d": float(item.get("f15") or 0),
                })
            break  # success
        except Exception as e:
            print(f"[concept flow] err: {e}", file=sys.stderr)
    return results

# ─── 评分引擎 (TSP signal_engine 风格) ───────────────────────────────────
def score_stock(stock, concept_hit, theme_score, news_count):
    """Score a stock on [-1, 1] scale. 0.6=strong BUY, -0.6=strong SELL."""
    chg = stock.get("chg_pct", 0)
    main_in = stock.get("main_net_in", 0)
    vol_ratio = stock.get("turnover", 0)
    # normalized signals
    s_chg = min(max(chg / 10, -1), 1)          # ±10% → [-1, 1]
    s_main = min(max(main_in / 5, -1), 1)     # ±5亿 → [-1, 1]
    s_vol = min(max(vol_ratio / 3, -1), 1)    # 量比
    # weighted sum (TSP-style: additive signals with different weights)
    composite = (0.35 * s_chg + 0.35 * s_main + 0.20 * s_vol + 0.10 * theme_score)
    return round(composite, 3), {
        "chg_norm": round(s_chg, 3),
        "main_norm": round(s_main, 3),
        "vol_norm": round(s_vol, 3),
        "theme_norm": round(theme_score, 3),
    }

# ─── 主流程 ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    news_dir = os.environ.get("STOCKS_NEWS_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "..", "stocks-data-news", "消息面")
    live_dir = os.environ.get("STOCKS_LIVE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "..", "stocks-data-live", "data")
    ana_dir = os.environ.get("STOCKS_ANA_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "..", "stocks", "analysis")
    out_dir = pathlib.Path(args.out) if args.out else pathlib.Path(ana_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    ts = now.strftime("%Y%m%d_%H%M")

    # 1. 拉新闻 (多源)
    news = fetch_eastmoney_news(80) + fetch_sina_news(60)
    print(f"[news] fetched {len(news)} items", flush=True)
    if not news:
        print("[hotspot] no news fetched, using fallback local news dir", flush=True)
        # fallback: read local .md files
        p = pathlib.Path(news_dir)
        if p.exists():
            for f in sorted(p.glob("*.md"))[-5:]:
                text = f.read_text(encoding="utf-8", errors="replace")
                news.append((f.stem, f.name, "", text[:500]))

    # 2. 新闻 → 主题映射
    theme_to_news = {}
    for dt, title, url, digest in news:
        for t in theme_of(title + " " + digest):
            theme_to_news.setdefault(t, []).append((dt, title[:60]))
    print(f"[themes] {len(theme_to_news)}: {sorted(theme_to_news.keys())}", flush=True)
    if not news:
        print("[hotspot] no news available; concept flow may still surface active boards", flush=True)

    # 3. 概念板块资金流
    concepts = fetch_concept_flow()
    print(f"[concepts] fetched {len(concepts)} boards", flush=True)

    # 4. 概念 → 主题匹配，计算板块热度
    concept_hit = {}  # concept_name → (theme, hit_strength)
    for c in concepts:
        name = c["name"]
        themes_hit = [t for t in theme_to_news if t in name]
        if themes_hit:
            theme_score = len(themes_hit) / max(1, len(theme_to_news))
            concept_hit[name] = (themes_hit, theme_score)

    # 5. 打分 + 排序
    scored = []
    for c in concepts:
        if c["name"] not in concept_hit:
            continue
        themes, t_score = concept_hit[c["name"]]
        # 个股打分 (用板块资金流近似个股影响力)
        composite, splits = score_stock(c, True, t_score, len(news))
        scored.append({**c, "themes": themes, "theme_score": round(t_score, 3),
                       "composite": composite, **splits})

    scored.sort(key=lambda x: abs(x["composite"]), reverse=True)
    top = scored[:args.top]
    print(f"[top {len(top)} concepts affected]", flush=True)
    for c in top[:10]:
        print(f"  {c['name']}: chg={c['chg_pct']:+.2f}%  main={c['main_net_in']:+.2f}亿  composite={c['composite']:+.3f}  themes={c['themes']}", flush=True)

    # 6. 输出 JSON + MD
    out_json = out_dir / f"hotspot_impact_{ts}.json"
    out_md = out_dir / f"hotspot_impact_{ts}.md"
    report = {
        "ts": now.isoformat(),
        "news_count": len(news),
        "themes_detected": sorted(theme_to_news.keys()),
        "concepts_hit": theme_to_news,
        "concept_impact": top,
        "all_concepts_top50": concepts[:50],
    }
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [f"# 热点影响分析 {now.strftime('%Y-%m-%d %H:%M')} CST", "",
          f"- 新闻数: {len(news)} | 命中主题: {len(theme_to_news)} | 影响板块: {len(top)}", "",
          "## TOP 影响板块", "",
          "| 板块 | 涨跌% | 主力净流入(亿) | 量比 | 综合分 | 关联主题 |",
          "|------|------|-------------|------|--------|----------|"]
    for c in top:
        md.append(f"| {c['name']} | {c['chg_pct']:+.2f}% | {c['main_net_in']:+.2f} | {c['turnover']:.2f} | {c['composite']:+.3f} | {','.join(c['themes'])} |")
    md += ["", "## 主题词命中详情", "", "| 主题 | 相关新闻数 |", "|------|---------|"]
    for t, items in sorted(theme_to_news.items()):
        md.append(f"| {t} | {len(items)} |")
    md += ["", "---", f"*生成: {now.isoformat()} CST · 数据源: 东财+新浪 7x24 · TSP 信号合成框架 v2*",
           "*仅供研究，不构成投资建议*"]
    out_md.write_text("\n".join(md), encoding="utf-8")
    print(f"[output] {out_json}  {out_md}", flush=True)


if __name__ == "__main__":
    main()
