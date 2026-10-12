#!/usr/bin/env python3
"""全市场实时快照采集 v2: 腾讯qt批量(主力, 60只/批无限制) + 东财push2(辅助)
→ data/market_snapshot/latest.json (全市场现价/涨跌/量比/换手/主力净流入字段尽力)
"""
import urllib.request, json, os, datetime, time
OUT = "/home/agentuser/stockdb/data/market_snapshot"
os.makedirs(OUT, exist_ok=True)


def qt_batch(syms):
    """腾讯qt批量实时, 返回 {symbol: dict}"""
    out = {}
    for i in range(0, len(syms), 60):
        chunk = syms[i:i + 60]
        try:
            u = "https://qt.gtimg.cn/q=" + ",".join(chunk)
            req = urllib.request.Request(u, headers={"Referer": "https://gu.qq.com"})
            body = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
            for line in body.strip().split(";"):
                p = line.split("~")
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
            time.sleep(0.15)
        except Exception:
            time.sleep(1)
    return out


def main():
    today = datetime.date.today().isoformat()
    syms = [s.strip() for s in open("/tmp/all_mainboard.txt") if s.strip()]
    data = qt_batch(syms)
    rows = sorted(data.values(), key=lambda x: -x["pct"])
    snap = {
        "ts": datetime.datetime.now().isoformat(),
        "date": today, "n": len(rows), "source": "qt.gtimg.cn batch",
        "zt": [r for r in rows if r["pct"] >= 9.9],
        "top_gainers": rows[:50],
        "top_losers": rows[-50:][::-1],
    }
    json.dump(snap, open(f"{OUT}/{today}_{datetime.datetime.now():%H%M}.json", "w"), ensure_ascii=False)
    json.dump(snap, open(f"{OUT}/latest.json", "w"), ensure_ascii=False)
    print(f"snapshot {len(rows)}只 涨停{len(snap['zt'])}只")


if __name__ == "__main__":
    main()
