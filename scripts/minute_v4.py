#!/usr/bin/env python3
"""分钟级全市场采集 v4: 新浪分钟接口高频(9.6只/秒实测)
盘中每30分钟全市场一轮(3175只≈5.5分钟), 5分钟OHLCV入duckdb minute表
去重: 同票同hhmm覆盖(先删后插)
"""
import warnings
warnings.filterwarnings("ignore")
import urllib.request, json, duckdb, os, datetime, time, sys

DB = "/home/agentuser/stockdb/stock.duckdb"
SNAP = "/home/agentuser/stockdb/data/market_snapshot/2026-10-12_full.csv"  # 不用; 票列表来自daily
HDR = {"Referer": "https://finance.sina.com.cn"}


def fetch_minute(sym6, scale=5, datalen=16):
    u = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
         f"CN_MarketDataService.getKLineData?symbol={sym6}&scale={scale}&ma=no&datalen={datalen}")
    req = urllib.request.Request(u, headers=HDR)
    b = urllib.request.urlopen(req, timeout=6).read().decode()
    if "=(" not in b:
        return []
    s = b[b.index("=(") + 2:b.rindex(")")]
    return json.loads(s)


def main(limit=None, datalen=16):
    today = datetime.date.today().isoformat()
    con = duckdb.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS minute (
        symbol VARCHAR, date VARCHAR, hhmm VARCHAR, price DOUBLE, volume DOUBLE)""")
    syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily").fetchall()]
    if limit:
        syms = syms[:limit]
    ok = fail = 0
    t0 = time.time()
    for sym in syms:
        sym6 = ("sh" if sym.startswith("sh") else "sz") + sym[2:]
        try:
            bars = fetch_minute(sym6, datalen=datalen)
            rows = [(sym, today, x["day"][11:16], float(x["close"]), float(x["volume"]))
                    for x in bars if x["day"].startswith(today)]
            if rows:
                # 去重: 删今日该票再插
                con.execute("DELETE FROM minute WHERE symbol=? AND date=?", [sym, today])
                con.executemany("INSERT INTO minute VALUES (?,?,?,?,?)", rows)
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
            time.sleep(0.2)
    n = con.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM minute WHERE date=?", [today]).fetchone()
    el = time.time() - t0
    con.close()
    print(f"[{datetime.datetime.now():%H:%M:%S}] 本轮: ok={ok} fail={fail} 耗时{el:.0f}秒 "
          f"({ok/el:.1f}只/秒) | 库内今日 {n[1]}票/{n[0]}行")


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(limit=limit)
