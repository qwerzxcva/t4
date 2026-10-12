#!/usr/bin/env python3
"""pages_sync.py — 把本地实时数据转成 stocks-ai-view (GitHub Pages) 的 data/latest.json 并推送
   根因: 用户访问的是 qwerzxcva.github.io/stocks-ai-view, 该站数据靠 Actions(已停), 停在10:07
   方案: 本地cron每10分钟把最新数据转格式推上去, Pages自动部署(约1分钟生效)
"""
import json, os, re, base64, datetime, urllib.request
TOKEN = None
for line in open(os.path.expanduser("~/.git-credentials")):
    m = re.search(r"ghp_[A-Za-z0-9]+", line)
    if m: TOKEN = m.group(0); break
H = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"}
BASE = "/home/agentuser/stockdb"
REPO = "stocks-ai-view"

def api(method, url, payload=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None, headers=H, method=method)
    return json.load(urllib.request.urlopen(req, timeout=30))

def jload(p):
    try: return json.load(open(p))
    except Exception: return {}

def build():
    now = datetime.datetime.now().astimezone()
    ts = now.isoformat(timespec="seconds")
    snap = jload(f"{BASE}/data/market_snapshot/latest.json")
    pools = jload(f"{BASE}/data/pools.json")
    ap = jload(f"{BASE}/data/ai_picks.json")
    glob = jload(f"{BASE}/data/global/latest.json")
    hot = jload(f"{BASE}/data/hot/latest.json")
    news = jload(f"{BASE}/data/news_map/{now.strftime('%Y-%m-%d')}.json")
    mf = jload(f"{BASE}/data/moneyflow/latest.json")

    zt = snap.get("zt", [])
    hot_local = jload(f"{BASE}/data/hot/{now.strftime('%Y%m%d')}_1500.json") or {}
    # hot的zt带lbc连板数 — 用它替换/合并
    if hot_local.get("zt"):
        hot_codes = {z.get("code"): z for z in hot_local["zt"]}
        for z in zt:
            h = hot_codes.get(z.get("code", ""))
            if h:
                z.setdefault("lbc", h.get("lbc", 1))
                z.setdefault("fbt", h.get("fbt", ""))
                z["lbc"] = h.get("lbc", 1)
                z["fbt"] = h.get("fbt", "")
    hot = hot_local or hot
    # 涨停池(带连板/首封时间)
    lup = [{"code": z.get("code", ""), "name": z.get("name", ""),
            "price": z.get("price", 0), "chg_pct": z.get("pct", 9.9),
            "lianban": z.get("lbc", 1), "zt_time": str(z.get("fbt", ""))} for z in zt[:40]]
    # 连板梯队
    tiers = {}
    for z in zt:
        lb = z.get("lbc", 1)
        tiers.setdefault(lb, []).append(z.get("code", ""))
    ladder = {"height": max(tiers) if tiers else 0,
              "tiers": [{"boards": lb, "count": len(cs), "codes": cs[:4]} for lb, cs in sorted(tiers.items(), reverse=True)]}
    nzt = len(zt)
    nzb = len(hot.get("zb", []))
    seal_rate = round(nzt / (nzt + nzb) * 100, 1) if (nzt + nzb) else 0
    first_board = sum(1 for z in zt if z.get("lbc", 1) == 1)
    ge2 = nzt - first_board
    # picks = AI推荐(带实时价)
    picks = []
    for p in (ap.get("picks") or [])[:15]:
        sym = p.get("symbol", "")
        picks.append({"code": sym[2:], "prefix": sym[:2], "name": p.get("name", ""),
                      "price": p.get("rt_price", p.get("price", 0)), "chg_pct": p.get("rt_pct", p.get("pct", 0)),
                      "score": p.get("score"), "vratio": p.get("vratio"),
                      "net3_wan": (mf.get("flows", {}).get(sym, {}) or {}).get("net3_wan")})
    # 阶段
    g = glob.get("metrics", {})
    data = {
        "ts": ts, "date": now.strftime("%Y%m%d"),
        "market": {"indices": [], "limit_up": nzt, "limit_down": len(hot.get("dt", [])), "broken": nzb,
                   "zt_rate": seal_rate},
        "phase": {"score": g.get("global_score"), "stage": g.get("global_mood", ""), "desc": g.get("suggest", ""),
                  "seal_rate": seal_rate, "first_board": first_board, "ge2": ge2,
                  "max_boards": ladder["height"], "promo_rate": round(ge2 / nzt * 100, 1) if nzt else 0},
        "limit_up_pool": lup, "limit_down_pool": [],
        "ladder": ladder,
        "picks": picks,
        "pools": {"cycle": (pools.get("cycle", {}) or {}).get("promotion_candidates", [])[:10],
                  "sector_pool": (pools.get("sector", {}) or {}).get("top", [])[:10],
                  "lowpool": (pools.get("lowpos", {}) or {}).get("picks", [])[:10],
                  "ai": picks},
        "themes": [{"name": t.get("theme", t.get("name", "")), "news_count": t.get("count", 0)}
                   for t in (hot.get("themes") or [])[:12]],
        "news_recent": (news.get("items") or [])[:30],
        "meta": {"source": "local-realtime", "engine": "stockdb v5", "ts": ts},
    }
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
    raw = json.dumps(data, ensure_ascii=False).encode()
    sha = put_file("data/latest.json", raw, f"local realtime sync {data['ts']}")
    print("pushed data/latest.json ->", sha)
    # 其余静态池文件也同步(页面可能引用)
    for name, local in [("cycle.json", f"{BASE}/data/pools.json"),
                        ("sector_pool.json", f"{BASE}/data/pools.json"),
                        ("lowpool.json", f"{BASE}/data/pools.json"),
                        ("ai_picks.json", f"{BASE}/data/ai_picks.json"),
                        ("moneyflow.json", f"{BASE}/data/moneyflow/latest.json"),
                        ("fund.json", f"{BASE}/data/fund/latest.json"),
                        ("global.json", f"{BASE}/data/global/latest.json"),
                        ("social.json", f"{BASE}/data/social/latest.json")]:
        if os.path.exists(local):
            try:
                s = put_file(f"data/{name}", open(local, "rb").read(), f"sync {name} {data['ts']}")
                print("pushed", name, "->", s)
            except Exception as e:
                print(name, "FAIL", str(e)[:60])

if __name__ == "__main__":
    main()
