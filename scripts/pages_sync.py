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

    # AI推荐(实时价+评分) — qt实时补名称/现价(ai_picks无name/rt)
    picks = []
    syms = [p.get("symbol", "") for p in (ap.get("picks") or [])[:15]]
    rt = {}
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
        picks.append({"code": sym[2:], "name": r[0], "price": r[1], "chg_pct": r[2],
                      "lianban": 1, "zt_time": "", "theme_hit": False,
                      "rps5": None, "rps20": None, "score": round((p.get("score", 0) or 0) * 100)})

    lup = [{"code": z.get("code", ""), "name": z.get("name", ""), "price": z.get("price", 0),
            "chg_pct": z.get("pct", 9.9), "lianban": z.get("lbc", 1), "zt_time": str(z.get("fbt", ""))}
           for z in zt[:40]]

    items = news.get("items") or []
    news_recent = [{"ts": x.get("time", ""), "title": (x.get("title", "") or "")[:80],
                    "theme": (x.get("themes") or [])[:2], "sentiment": x.get("sentiment", "neu"),
                    "src": x.get("source", "em"), "digest": ""} for x in items[:40]] or data.get("news_recent", [])

    data.update({
        "ts": ts, "date": now.strftime("%Y%m%d"),
        "market": {**data.get("market", {}), "limit_up": nzt, "limit_down": len(hot.get("dt", [])),
                   "broken": nzb, "zt_rate": seal},
        "phase": {**data.get("phase", {}), "score": g.get("global_score"),
                  "stage": g.get("global_mood", ""), "desc": g.get("suggest", ""),
                  "seal_rate": seal, "first_board": first, "ge2": nzt - first,
                  "max_boards": ladder["height"],
                  "promo_rate": round((nzt - first) / nzt * 100, 1) if nzt else 0},
        "ladder": ladder, "limit_up_pool": lup, "picks": picks,
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
