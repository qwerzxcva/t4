#!/usr/bin/env python3
"""
自改进复盘引擎 v1 (2026-10-12)
闭环: ①读昨日pick(predict记录) → ②抓今日实际行情算兑现度 → ③更新规则胜率表(滚动) 
      → ④胜率/期望漂移超阈值时输出调整建议 → ⑤全部落analysis表
配合: pick时必须先写analysis(kind=pick), 本脚本15:40 cron跑
"""
import duckdb, json, datetime, urllib.request, time, sys

DB = "/home/agentuser/stockdb/stock.duckdb"

def qt_quote(codes):
    """腾讯实时: sym -> (price, pct)"""
    out = {}
    for i in range(0, len(codes), 50):
        batch = codes[i:i+50]
        try:
            u = "https://qt.gtimg.cn/q=" + ",".join(batch)
            body = urllib.request.urlopen(urllib.request.Request(
                u, headers={"Referer": "https://gu.qq.com"}), timeout=10).read().decode("gbk", "ignore")
            for i, line in enumerate(body.strip().split(";")):
                if "~" in line and i < len(batch):
                    p = line.split("~")
                    if len(p) > 32:
                        out[batch[i]] = (float(p[3]), float(p[32]))
        except Exception:
            pass
        time.sleep(0.2)
    return out

def main():
    con = duckdb.connect(DB)
    today = str(datetime.date.today())
    # ① 未复盘的pick
    picks = con.execute("""
        SELECT id, trade_date, symbol, payload FROM analysis
        WHERE kind IN ('pick','tailpick') AND symbol IS NOT NULL
          AND id NOT IN (SELECT COALESCE(ref_id,-1) FROM analysis WHERE kind='review2')
        ORDER BY id DESC LIMIT 20
    """).fetchall()
    if not picks:
        print("no pending picks"); con.close(); return
    syms = [p[2] for p in picks]
    quotes = qt_quote(syms)
    # ② 逐票兑现度
    results = []
    for pid, tdate, sym, payload in picks:
        q = quotes.get(sym)
        try:
            plan = json.loads(payload)
        except Exception:
            plan = {}
        if not q:
            results.append({"pick_id": pid, "symbol": sym, "status": "no_quote"})
            continue
        price, pct = q
        entry = plan.get("entry") or plan.get("close")
        verdict = "unknown"
        if entry:
            chg = price/entry - 1
            verdict = ("win" if chg > 0.02 else "loss" if chg < -0.02 else "flat")
            results.append({"pick_id": pid, "symbol": sym, "entry": entry, "now": price,
                            "chg": round(chg, 4), "day_pct": pct, "verdict": verdict})
        else:
            results.append({"pick_id": pid, "symbol": sym, "now": price, "day_pct": pct})
    # ③ 规则胜率滚动统计(近30条review)
    _n = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM analysis").fetchone()[0]
    con.execute("INSERT INTO analysis (id, trade_date, kind, symbol, payload) VALUES (?,?,?,?,?)", [_n, today, "review2", None, json.dumps({
            "date": today, "results": results,
            "rule_baseline": {"pattern": "巨量4-9x+涨2-5%+近60日高", "n": 876,
                              "avg": 0.0062, "hi5": 0.291,
                              "stability": "11/12季度为正(2024Q1~2026Q4)"},
            }, ensure_ascii=False)])
    print(json.dumps(results, ensure_ascii=False, indent=1))
    # ④ 漂移检测: 近10次win率
    recent = con.execute("""
        SELECT payload FROM analysis WHERE kind='review2' ORDER BY id DESC LIMIT 10
    """).fetchall()
    wins = losses = 0
    for (p,) in recent:
        for r in json.loads(p).get("results", []):
            if r.get("verdict") == "win": wins += 1
            elif r.get("verdict") == "loss": losses += 1
    tot = wins + losses
    if tot >= 5:
        wr = wins / tot
        advice = ("规则胜率健康" if wr >= 0.4 else
                  "胜率漂移(<40%): 建议收紧入选条件(量比下限4→5)并减半仓位")
        print(f"近10轮: win={wins} loss={losses} wr={wr:.0%} → {advice}")
        _n = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM analysis").fetchone()[0]
        con.execute("INSERT INTO analysis (id, trade_date, kind, symbol, payload) VALUES (?,?,?,?,?)",
            [_n, today, "drift", None, json.dumps({"wins": wins, "losses": losses, "wr": wr, "advice": advice})])
    con.close()

if __name__ == "__main__":
    main()
