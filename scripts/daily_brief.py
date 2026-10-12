#!/usr/bin/env python3
"""每日自动推演简报 (16:00 收盘后) — 全链路最后一环:
   采集(已cron) → 算法推演(本脚本) → 落duckdb analysis → 网站复盘Tab展示
内容: 市场阶段 + 当日推荐兑现 + 涨停梯队结构 + AI池明日候选 + 全球情绪
"""
import duckdb, json, os, datetime, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "/home/agentuser/stockdb"
DB = f"{BASE}/stock.duckdb"
TODAY = datetime.date.today().isoformat()
PY = os.path.basename(__file__)

def qt(syms):
    """腾讯实时批量"""
    out = {}
    for i in range(0, len(syms), 60):
        chunk = syms[i:i+60]
        full = ",".join(chunk)
        try:
            txt = urllib.request.urlopen(urllib.request.Request(
                f"https://qt.gtimg.cn/q={full}", headers={"User-Agent": "Mozilla/5.0"}), timeout=8).read().decode("gbk", "ignore")
            for line in txt.split(";"):
                line = line.strip()
                if "~" not in line: continue
                f = line.split("~")
                out[f[2]] = {"name": f[1], "price": float(f[3] or 0), "pct": float(f[32] or 0)}
        except Exception:
            pass
    return out

def main():
    con = duckdb.connect(DB, read_only=True)
    brief = {"date": TODAY, "sections": []}

    # 1) 昨日推荐兑现 (analysis里最近的pick, 用今天的收盘数据核)
    try:
        picks = con.execute("""SELECT id, symbol, payload FROM analysis
            WHERE kind='pick' AND trade_date < ? ORDER BY id DESC LIMIT 8""", [TODAY]).fetchall()
        syms = list({p[1] for p in picks if p[1]})
        rt = qt(syms) if syms else {}
        realized = []
        for pid, sym, payload in picks:
            key = sym[-6:] if sym and sym not in rt else sym
            if key not in rt: continue
            p = json.loads(payload) if payload else {}
            name = rt[key]["name"]
            pct = rt[key]["pct"]
            realized.append({"id": pid, "sym": sym, "name": name, "pct": pct,
                             "note": p.get("note", "")[:40]})
        avg = round(sum(r["pct"] for r in realized)/len(realized), 2) if realized else None
        brief["sections"].append({"title": "昨日推荐·今日兑现", "items": realized, "avg": avg})
    except Exception as e:
        brief["sections"].append({"title": "昨日推荐·今日兑现", "error": str(e)})

    # 2) 今日盘面结构 (快照json)
    try:
        snap = json.load(open(f"{BASE}/data/market_snapshot/latest.json"))
        zt = snap.get("zt", [])
        lbc = {}
        # 从hot拿连板
        hot = json.load(open(f"{BASE}/data/hot/latest.json")) if os.path.exists(f"{BASE}/data/hot/latest.json") else {}
        zb = len(hot.get("zb", []))
        dt = len(hot.get("dt", []))
        up = sum(1 for x in snap.get("top_gainers", []) if x.get("pct", 0) > 5)
        brief["sections"].append({"title": "今日盘面", "zt": len(zt), "zb": zb, "dt": dt,
                                  "up5": up, "zt_names": [x.get("name") for x in zt[:8]]})
    except Exception as e:
        brief["sections"].append({"title": "今日盘面", "error": str(e)})

    # 3) 全球情绪
    try:
        g = json.load(open(f"{BASE}/data/global/latest.json"))
        m = g.get("metrics", {})
        brief["sections"].append({"title": "全球情绪", "score": m.get("global_score"),
                                  "mood": m.get("global_mood"), "suggest": m.get("suggest")})
    except Exception as e:
        brief["sections"].append({"title": "全球情绪", "error": str(e)})

    # 4) AI池明日候选 (最新ai_picks前5, 附实时价核未涨停)
    try:
        ap = json.load(open(f"{BASE}/data/ai_picks.json"))
        picks5 = ap.get("picks", [])[:5]
        syms = [p["symbol"] for p in picks5]
        rt = qt(syms) if syms else {}
        cand = []
        for p in picks5:
            sym = p["symbol"]; r6 = sym[-6:]
            r = rt.get(r6, {})
            cand.append({"sym": r6, "name": r.get("name", p.get("name", "")),
                         "price": r.get("price"), "pct": r.get("pct"),
                         "score": p.get("score"), "zt": r.get("pct", 0) >= 9.7})
        brief["sections"].append({"title": "AI池·明日候选(核实时价)", "items": cand})
    except Exception as e:
        brief["sections"].append({"title": "AI池·明日候选", "error": str(e)})
    con.close()

    # 落库 analysis (网站复盘Tab自动显示)
    try:
        import duckdb as _dd
        c = _dd.connect(DB)
        nid = c.execute("SELECT COALESCE(MAX(id),0)+1 FROM analysis").fetchone()[0]
        # 幂等: 同日brief先删旧的, 保证盘中重跑不堆行且最新
        c.execute("DELETE FROM analysis WHERE kind='brief' AND trade_date=?", [TODAY])
        c.execute("INSERT INTO analysis (id, trade_date, kind, symbol, payload) VALUES (?,?,?,?,?)",
                  [nid, TODAY, "brief", "", json.dumps(brief, ensure_ascii=False)])
        c.close()
    except Exception as e:
        print("db insert err:", e)

    # 也存json供直接读
    os.makedirs(f"{BASE}/data/brief", exist_ok=True)
    json.dump(brief, open(f"{BASE}/data/brief/latest.json", "w"), ensure_ascii=False, indent=1)
    print(f"brief done: {len(brief['sections'])} sections")

if __name__ == "__main__":
    main()
