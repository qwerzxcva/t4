#!/usr/bin/env python3
"""全市场分钟级采集 v3: akshare新浪源(5520只全A, 含深市)
每日15:35全量5分钟线 → duckdb minute表 + GitHub归档
"""
import warnings
warnings.filterwarnings("ignore")
import akshare as ak
import duckdb, json, os, datetime, time

DB = "/home/agentuser/stockdb/stock.duckdb"
TODAY = datetime.date.today().isoformat()

def main(symbols=None, limit=None):
    con = duckdb.connect(DB)
    # 确保minute表存在
    con.execute("""CREATE TABLE IF NOT EXISTS minute (
        symbol VARCHAR, date VARCHAR, hhmm VARCHAR, price DOUBLE, volume DOUBLE)""")
    syms = symbols or [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM daily").fetchall()]
    if limit:
        syms = syms[:limit]
    ok = fail = 0
    for sym in syms:
        mkt = "sh" if sym.startswith("sh") else "sz"
        try:
            df = ak.stock_zh_a_minute(symbol=f"{mkt}{sym[2:]}", period="5")
            if df is None or df.empty:
                fail += 1
                continue
            df = df[df["day"].str.startswith(TODAY)]
            rows = [(sym, TODAY, d[11:16], float(c), float(v))
                    for d, c, v in zip(df["day"], df["close"], df["volume"])]
            if rows:
                con.executemany("INSERT INTO minute VALUES (?,?,?,?,?)", rows)
                ok += 1
            time.sleep(0.12)
        except Exception:
            fail += 1
            time.sleep(0.3)
    n = con.execute("SELECT COUNT(*) FROM minute WHERE date=?", [TODAY]).fetchone()[0]
    con.close()
    print(f"minute采集: ok={ok} fail={fail} 今日累计{n}行")

if __name__ == "__main__":
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(limit=limit)
