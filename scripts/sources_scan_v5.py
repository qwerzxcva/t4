#!/usr/bin/env python3
"""多源交叉并行采集 v5 (2026-10-12 重构)
分工原则: 每个源抓它最强的数据, 互不重复, 全部并发:
  腾讯qt  (3720只/秒) → 全市场实时快照(现价/涨跌/换手/量比)   [37秒级]
  新浪分钟(192只/秒)  → 全市场5分钟K线                        [17秒级]
  新浪fx/hf + 腾讯全球  → 全球面板(独立任务global_panel.py)
  东财push2ex         → 涨停池梯队(腾讯没有的连板/炸板字段)
  腾讯kline           → 收盘后日线补齐
输出: data/market_snapshot/latest.json (合并qt快照+分钟衍生指标) + duckdb minute
"""
import warnings
warnings.filterwarnings("ignore")
import urllib.request, json, os, datetime, time, sys, csv
import concurrent.futures as cf
import duckdb

OUT_DIR = "/home/agentuser/stockdb/data/market_snapshot"
DB = "/home/agentuser/stockdb/stock.duckdb"
os.makedirs(OUT_DIR, exist_ok=True)
QT_HDR = {"Referer": "https://gu.qq.com"}
SINA_HDR = {"Referer": "https://finance.sina.com.cn"}


# ---------- 源1: 腾讯qt 全市场实时快照 (并发批量) ----------
def qt_chunk(syms):
    out = {}
    try:
        u = "https://qt.gtimg.cn/q=" + ",".join(syms)
        body = urllib.request.urlopen(urllib.request.Request(u, headers=QT_HDR), timeout=8).read().decode("gbk", "ignore")
        for line in body.strip().split(";"):
            line = line.strip()
            if "~" not in line or len(line) < 30:
                continue
            p = line.split('"')[1].split("~")
            if len(p) > 49:
                c6 = p[2]
                pre = "sh" if c6.startswith("6") else "sz"
                try:
                    out[pre + c6] = {
                        "code": c6, "name": p[1],
                        "price": float(p[3]), "pct": float(p[32]),
                        "volume": float(p[36]) if p[36] else 0,
                        "turnover": float(p[38]) if p[38] else 0,
                        "rt_time": p[30],
                    }
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    return out


def qt_all(symbols, workers=10):
    chunks = [symbols[i:i + 60] for i in range(0, len(symbols), 60)]
    data = {}
    with cf.ThreadPoolExecutor(workers) as ex:
        for d in ex.map(qt_chunk, chunks):
            data.update(d)
    return data


# ---------- 源2: 新浪 5分钟K线 (并发分片) ----------
def sina_minute_one(sym6):
    u = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
         f"CN_MarketDataService.getKLineData?symbol={sym6}&scale=5&ma=no&datalen=5")
    req = urllib.request.Request(u, headers=SINA_HDR)
    b = urllib.request.urlopen(req, timeout=8).read().decode()
    if "=(" not in b:
        return sym6, []
    s = b[b.index("=(") + 2:b.rindex(")")]
    return sym6, json.loads(s)


def sina_minute_all(symbols, today, workers=32):
    """返回 {sym: [(hhmm, close, volume), ...]}, 并写入duckdb"""
    ok_rows = []
    t0 = time.time()
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(sina_minute_one, ("sh" if s.startswith("sh") else "sz") + s[2:]): s for s in symbols}
        for f in cf.as_completed(futs):
            sym = futs[f]
            try:
                sym6, bars = f.result()
                for x in bars:
                    if x["day"].startswith(today):
                        ok_rows.append((sym, today, x["day"][11:16], float(x["close"]), float(x["volume"])))
            except Exception:
                pass
    el = time.time() - t0
    return ok_rows, el


def minute_to_duckdb(rows):
    con = duckdb.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS minute (
        symbol VARCHAR, date VARCHAR, hhmm VARCHAR, price DOUBLE, volume DOUBLE)""")
    today = rows[0][1] if rows else ""
    syms = list({r[0] for r in rows})
    # 增量去重: 查库内已有(symbol,hhmm), 只插缺的 (避免全删重插5万行)
    existing = set()
    if syms:
        res = con.execute("SELECT symbol, hhmm FROM minute WHERE date=? AND symbol IN ({})".format(
            ",".join("?" * len(syms))), [today] + syms).fetchall()
        existing = {(a, b) for a, b in res}
    new_rows = [r for r in rows if (r[0], r[2]) not in existing]
    if new_rows:
        con.executemany("INSERT INTO minute VALUES (?,?,?,?,?)", new_rows)
    con.close()
    return len(new_rows)


# ---------- 源3: 东财push2ex 涨停池梯队 (腾讯没有连板字段) ----------
def em_zt_pool():
    try:
        u = ("https://push2ex.eastmoney.com/getTopicZTPool?"
             "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=300&sort=fbt%3Aasc")
        d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=8))
        pool = d.get("data", {}).get("pool", []) or []
        return [{"code": x["c"], "name": x["n"], "lbc": x.get("lbc", 1),
                 "fbt": x.get("fbt"), "hs": x.get("hs")} for x in pool]
    except Exception:
        return []


def main(collect_minute=True):
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now()
    symbols = [s.strip() for s in open("/tmp/all_mainboard.txt") if s.strip()]

    # ---- 并行跑两大源(线程级交叉) ----
    with cf.ThreadPoolExecutor(2) as ex:
        fut_qt = ex.submit(qt_all, symbols, 10)
        fut_min = ex.submit(sina_minute_all, symbols, today, 32) if collect_minute else None
        snap = fut_qt.result()
        if fut_min:
            min_rows, min_el = fut_min.result()

    rows = sorted(snap.values(), key=lambda x: -x["pct"])
    doc = {
        "ts": now.isoformat(), "date": today, "n": len(rows),
        "source": "qt(10并发批)+sina(32并发片) cross",
        "zt": [r for r in rows if r["pct"] >= 9.9],
        "zt_pool_em": em_zt_pool(),   # 东财源: 连板字段
        "top_gainers": rows[:50],
        "top_losers": rows[-50:][::-1],
    }
    if collect_minute:
        doc["minute_rows"] = len(min_rows)
        doc["minute_sec"] = round(min_el, 1)
        if min_rows:
            minute_to_duckdb(min_rows)

    # 全量CSV
    full = f"{OUT_DIR}/{today}_full.csv"
    with open(full, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "price", "pct", "volume", "turnover", "rt_time"])
        for r in rows:
            w.writerow([r["code"], r["name"], r["price"], r["pct"], r["volume"], r["turnover"], r["rt_time"]])
    json.dump(doc, open(f"{OUT_DIR}/latest.json", "w"), ensure_ascii=False)
    print(f"[{now:%H:%M:%S}] qt快照{len(rows)}只 涨停{len(doc['zt'])} 东财梯队{len(doc['zt_pool_em'])}"
          + (f" 分钟{len(min_rows)}行({min_el:.0f}s)" if collect_minute else ""))


if __name__ == "__main__":
    main(collect_minute=("--no-minute" not in sys.argv))
