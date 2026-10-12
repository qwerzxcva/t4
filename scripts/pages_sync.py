#!/usr/bin/env python3
"""pages_sync.py v2 — 本地实时数据 → stocks-ai-view (GitHub Pages)
   策略: 以原版 latest.json(Actions停摆前的完整版)为结构模板 — pools五池/fund_rank/RPS全保留,
   只覆盖实时字段: ts/market/phase/ladder/limit_up_pool/picks(实时价)/news_recent
   cron */10 推送, Pages自动部署~1.5分钟生效
"""
import json, os, re, base64, datetime, urllib.request, glob, ssl

def _sslctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c

TOKEN = None
for line in open(os.path.expanduser("~/.git-credentials")):
    m = re.search(r"ghp_[A-Za-z0-9]+", line)
    if m: TOKEN = m.group(0); break
H = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"}
BASE = "/home/agentuser/stockdb"
REPO = "stocks-ai-view"
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template_latest.json")

def api(method, url, payload=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None, headers=H, method=method)
    return json.load(urllib.request.urlopen(req, timeout=30))

def jload(p):
    try: return json.load(open(p))
    except Exception: return {}

def latest_hot():
    fs = sorted(glob.glob(f"{BASE}/data/hot/*.json"))
    return jload(fs[-1]) if fs else {}

def build():
    now = datetime.datetime.now().astimezone()
    ts = now.isoformat(timespec="seconds")
    data = jload(TEMPLATE)
    if not data:
        raise SystemExit("no template: " + TEMPLATE)
    snap = jload(f"{BASE}/data/market_snapshot/latest.json")
    ap = jload(f"{BASE}/data/ai_picks.json")
    glob = jload(f"{BASE}/data/global/latest.json")
    news = jload(f"{BASE}/data/news_map/{now.strftime('%Y-%m-%d')}.json")
    hot = latest_hot()

    zt = snap.get("zt", [])
    hot_codes = {z.get("code"): z for z in hot.get("zt", [])}
    for z in zt:
        h = hot_codes.get(z.get("code", ""))
        if h:
            z["lbc"] = h.get("lbc", 1)
            z["fbt"] = h.get("fbt", "")
    tiers = {}
    for z in zt:
        tiers.setdefault(z.get("lbc", 1), []).append(z.get("code", ""))
    ladder = {"height": max(tiers) if tiers else 0,
              "tiers": [{"boards": lb, "count": len(cs), "codes": cs[:4]}
                        for lb, cs in sorted(tiers.items(), reverse=True)]}
    nzt, nzb = len(zt), len(hot.get("zb", []))
    seal = round(nzt / (nzt + nzb) * 100, 1) if (nzt + nzb) else 0
    first = sum(1 for z in zt if z.get("lbc", 1) == 1)
    g = glob.get("metrics", {})

    # 打板池picks = 今日涨停+连板票(页面期望连板梯队), 按连板高→低排序; 旧AI推荐放不下这里
    tpl_picks = {p.get("code"): p for p in (data.get("picks") or [])}
    picks = []
    for z in sorted(zt, key=lambda x: -(x.get("lbc", 1) * 10000 - (x.get("fbt", 0) or 0))):
        code = z.get("code", "")
        old = tpl_picks.get(code, {})
        picks.append({"code": code, "name": z.get("name", ""), "price": z.get("price", 0),
                      "chg_pct": z.get("pct", 9.9), "lianban": z.get("lbc", 1),
                      "zt_time": str(z.get("fbt", "")), "theme_hit": old.get("theme_hit", False),
                      "rps5": old.get("rps5"), "rps20": old.get("rps20"),
                      "score": round(min(0.99, 0.3 + 0.1 * z.get("lbc", 1)), 3)})

    # 指数实时(qt): sh000001/sz399001/sh000300/sz399006/sh000688
    idx_defs = [("sh000001","上证指数"),("sz399001","深证成指"),("sh000300","沪深300"),
                ("sz399006","创业板指"),("sh000688","科创50")]
    indices = []
    try:
        url = "https://qt.gtimg.cn/q=" + ",".join(c for c,_ in idx_defs)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        b = urllib.request.urlopen(req, timeout=15, context=_sslctx()).read().decode("gbk", "ignore")
        rt_idx = {}
        for line in b.strip().split(";"):
            line = line.strip()
            if "=" not in line: continue
            k, v = line.split("=", 1)
            pp = v.strip('"').split("~")
            if len(pp) > 45 and pp[3]:
                rt_idx[k.split("_")[-1].lower()] = (float(pp[3]), float(pp[4]), float(pp[32] or 0))
        for code, name in idx_defs:
            r = rt_idx.get(code)
            if r:
                indices.append({"code": code, "name": name, "price": r[0], "prev": r[1], "chg_pct": r[2]})
    except Exception:
        indices = data.get("market", {}).get("indices", [])
    if not indices:
        indices = data.get("market", {}).get("indices", [])

    # AI推荐(未涨停, 五因子) — 单独字段供页面"AI 推荐标的"区, 与打板池picks分开
    ai_picks = []
    syms = [p.get("symbol", "") for p in (ap.get("picks") or [])[:15]]
    rt = {}
    if syms:
        try:
            url = "https://qt.gtimg.cn/q=" + ",".join(syms)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            b = urllib.request.urlopen(req, timeout=15, context=_sslctx()).read().decode("gbk", "ignore")
            for line in b.strip().split(";"):
                line = line.strip()
                if "=" not in line: continue
                k, v = line.split("=", 1)
                pp = v.strip('"').split("~")
                if len(pp) > 45 and pp[1]:
                    rt[k.split("_")[-1].lower()] = (pp[1], float(pp[3] or 0), float(pp[32] or 0))
        except Exception:
            pass
    for p in (ap.get("picks") or [])[:15]:
        sym = p.get("symbol", "")
        r = rt.get(sym, ("", 0, 0))
        ai_picks.append({"code": sym[2:], "name": r[0], "price": r[1], "chg_pct": r[2],
                         "lianban": 1, "zt_time": "", "theme_hit": False,
                         "rps5": None, "rps20": None, "score": round((p.get("score", 0) or 0), 3)})

    # 每日总结(来自brief)
    brf = jload(f"{BASE}/data/brief/latest.json")
    summary = None
    if brf:
        lines = []
        for sec in brf.get("sections", []):
            t = sec.get("title", "")
            if "兑现" in t:
                its = sec.get("items", [])
                if its:
                    avg = sec.get("avg")
                    lines.append(f"### 昨日推荐·今日兑现（均{avg:+.2f}%）" if isinstance(avg, (int, float)) else "### 昨日推荐·今日兑现")
                    for it in its:
                        lines.append(f"- {it.get('name','')} {it.get('pct',0):+.2f}%")
            elif "盘面" in t:
                lines.append(f"### 今日盘面：涨停{sec.get('zt',0)} 炸板{sec.get('zb',0)} 跌停{sec.get('dt',0)} 涨>5% {sec.get('up5',0)}")
                names = sec.get("zt_names") or []
                if names: lines.append("- 龙头: " + " ".join(names[:6]))
            elif "全球" in t:
                sug = sec.get('suggest') or ''
                lines.append(f"### 全球情绪：{sec.get('score')} ({sec.get('mood','')})" + (f" → {sug}" if sug else ""))
            elif "候选" in t:
                its = sec.get("items", [])
                if its:
                    lines.append("### AI池·明日候选（已核实时价未涨停）")
                    for it in its[:8]:
                        if isinstance(it, dict):
                            lines.append(f"- {it.get('name', it.get('symbol',''))} {it.get('pct', it.get('rt_pct', 0)):+.2f}% 评分{it.get('score','')}")
        summary = {"ts": ts, "text": chr(10).join(lines)}

    lup = [{"code": z.get("code", ""), "name": z.get("name", ""), "price": z.get("price", 0),
            "chg_pct": z.get("pct", 9.9), "lianban": z.get("lbc", 1), "zt_time": str(z.get("fbt", ""))}
           for z in zt[:40]]

    items = news.get("items") or []
    news_recent = [{"ts": x.get("time", ""), "title": (x.get("title", "") or "")[:80],
                    "theme": (x.get("themes") or [])[:2], "sentiment": x.get("sentiment", "neu"),
                    "src": x.get("source", "em"), "digest": ""} for x in items[:40]] or data.get("news_recent", [])
    # 实时新浪7x24(每次运行直抓, 不依赖9/19点cron)
    try:
        req = urllib.request.Request("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=30&zhibo_id=152&tag_id=0&type=0",
                                     headers={"User-Agent": "Mozilla/5.0"})
        zb = json.loads(urllib.request.urlopen(req, timeout=15, context=_sslctx()).read())
        for f in (zb.get("result", {}).get("data", {}).get("feed", {}).get("list", [])):
            raw = f.get("rich_text", "") or ""
            news_recent.append({"ts": (f.get("create_time", "") or "")[:19], "title": raw[:90],
                                "theme": [], "sentiment": "neu", "src": "sina", "digest": ""})
    except Exception as e:
        print("sina live err:", str(e)[:40])
    # 多源热榜(同花顺/抖音/B站/头条)
    hf = jload(f"{BASE}/data/hotfeeds/latest.json")
    hf_ts = (hf.get("ts") or "")[:19]
    for f in (hf.get("feeds") or [])[:40]:
        fts = f.get("ts") or ""
        # 同花顺给的是epoch秒 → 转时间; 其他源空ts用抓取时刻
        if fts.isdigit() and len(fts) == 10:
            fts = datetime.datetime.fromtimestamp(int(fts)).strftime("%Y-%m-%d %H:%M:%S")
        elif not fts:
            fts = hf_ts
        news_recent.append({"ts": fts, "title": "[" + f.get("src", "") + "] " + (f.get("title", "") or "")[:70],
                            "theme": [], "sentiment": "neu", "src": f.get("src", ""), "digest": f.get("digest", "")})
    # 去重(标题) + 按时间倒序
    seen, uniq = set(), []
    for n in news_recent:
        t = n.get("title", "")
        if t and t not in seen:
            seen.add(t); uniq.append(n)
    news_recent = sorted(uniq, key=lambda x: x.get("ts", ""), reverse=True)[:80]

    # cycle.macro_signals: 每个value补code字段(页面渲染列用)
    cyc = data.get("pools", {}).get("cycle", {})
    ms = cyc.get("macro_signals")
    if isinstance(ms, dict):
        for k, v in ms.items():
            if isinstance(v, dict) and "code" not in v:
                v["code"] = k

    data.update({
        "ts": ts, "date": now.strftime("%Y%m%d"),
        "market": {**data.get("market", {}), "indices": indices, "limit_up": nzt, "limit_down": len(hot.get("dt", [])),
                   "broken": nzb, "zt_rate": seal},
        "phase": {**data.get("phase", {}), "score": g.get("global_score"),
                  "stage": g.get("global_mood", ""), "desc": g.get("suggest", ""),
                  "seal_rate": seal, "first_board": first, "ge2": nzt - first,
                  "max_boards": ladder["height"],
                  "promo_rate": round((nzt - first) / nzt * 100, 1) if nzt else 0},
        "ladder": ladder, "limit_up_pool": lup, "picks": picks, "ai_picks": ai_picks,
        "summary": summary,
        "news_recent": news_recent,
        "meta": {**data.get("meta", {}), "source": "local-realtime", "ts": ts},
    })
    return data

def put_file(path, content_bytes, msg):
    sha = None
    try: sha = api("GET", f"https://api.github.com/repos/qwerzxcva/{REPO}/contents/{path}")["sha"]
    except Exception: pass
    body = {"message": msg, "content": base64.b64encode(content_bytes).decode()}
    if sha: body["sha"] = sha
    r = api("PUT", f"https://api.github.com/repos/qwerzxcva/{REPO}/contents/{path}", body)
    return r["commit"]["sha"][:8]

def main():
    data = build()
    sha = put_file("data/latest.json", json.dumps(data, ensure_ascii=False).encode(),
                   f"local realtime sync {data['ts']}")
    print("pushed data/latest.json ->", sha)

if __name__ == "__main__":
    main()
