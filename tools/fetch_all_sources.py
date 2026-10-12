#!/usr/bin/env python3
"""
统一数据聚合脚本 — fetch_all_sources.py
把9个可用数据源的所有数据拉下来，规范写入 stocks-data-live/data/ 目录。

数据源清单:
  1. 腾讯实时行情 (qt.gtimg.cn)      → market/snapshot_{date}.json
  2. 腾讯K线历史 (ifzq.gtimg.cn)     → 已有，不重跑
  3. 东财涨停池 (push2ex.eastmoney)  → pool/{date}.ztpool.json
  4. 东财跌停池 (push2ex.eastmoney)  → pool/{date}.dtpool.json
  5. 东财炸板池 (push2ex.eastmoney)  → pool/{date}.zbpool.json
  6. 东财行业板块 (push2.eastmoney)  → sector/{date}_rank.json
  7. 新浪7x24快讯 (cj.sina.com.cn)   → news/{date}_sina.json
  8. 财联社快讯 (api3.cls.cn)        → news/{date}_cls.json
  9. 新浪全市场行情 (hq.sinajs.cn)   → market/all_{date}.txt (替代 scan_market.py)
"""
import os, sys, json, time, re, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 数据输出目录
DATA_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data")
MARKET_DIR = os.path.join(DATA_DIR, "market")
POOL_DIR = os.path.join(DATA_DIR, "pool")
SECTOR_DIR = os.path.join(DATA_DIR, "sector")
NEWS_DIR = os.path.join(DATA_DIR, "news")

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
    "Referer": "https://finance.sina.com.cn/",
}
UA_EASTMONEY = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/",
}

os.makedirs(MARKET_DIR, exist_ok=True)
os.makedirs(POOL_DIR, exist_ok=True)
os.makedirs(SECTOR_DIR, exist_ok=True)
os.makedirs(NEWS_DIR, exist_ok=True)


def _http(url, headers=None, timeout=12):
    """Safe HTTP GET with retries."""
    h = headers or UA
    for i in range(3):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout).read()
        except Exception as e:
            if i < 2:
                time.sleep(0.5)
    return b""


def fetch_tencent_snapshot(codes, batch_size=60):
    """拉腾讯实时行情，返回 {code: {name,price,chg_pct,open,high,low,vol}}."""
    records = {}
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        url = f"https://qt.gtimg.cn/q={','.join(batch)}"
        body = _http(url)
        text = body.decode("gbk", "replace")
        for m in re.finditer(r'v_(\w+)="([^"]*)"', text):
            code, payload = m.group(1), m.group(2).split("~")
            if len(payload) < 40:
                continue
            try:
                price = float(payload[3] or 0)
                prev = float(payload[4] or 0)
                chg_pct = float(payload[32]) if len(payload) > 32 else (price/prev-1)*100 if prev else 0
                records[code] = {
                    "code": code, "name": payload[1],
                    "price": price, "prev_close": prev,
                    "open": float(payload[5] or 0),
                    "high": float(payload[33]) if len(payload) > 33 else price,
                    "low": float(payload[34]) if len(payload) > 34 else price,
                    "chg_pct": round(chg_pct, 2),
                    "time": payload[30] if len(payload) > 30 else "",
                }
            except (ValueError, IndexError):
                continue
        time.sleep(0.1)
    return records


def fetch_tencent_universe():
    """从 universe.csv 读全市场代码列表."""
    uv_path = "/root/workspace/stocks-data-kline/data/bulk/universe.csv"
    if not os.path.exists(uv_path):
        return []
    codes = []
    with open(uv_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if parts and parts[0]:
                codes.append(parts[0].strip())
    return codes


def fetch_eastmoney_pool(kind):
    """东财涨跌停/炸板池."""
    url = f"https://push2ex.eastmoney.com/getTopic{kind}Pool"
    url += "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
    url += "&Pageindex=0&pagesize=300&sort=fbt%3Aasc"
    body = _http(url, headers=UA_EASTMONEY)
    try:
        d = json.loads(body)
        pool = (d.get("data") or {}).get(f"{kind}Pool") or (d.get("data") or {}).get("pool") or []
        return pool
    except Exception:
        return []


def fetch_eastmoney_sectors():
    """东财行业板块涨跌幅排行."""
    url = ("https://push2.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=150&po=1&np=1&fltt=2&invt=2"
           "&fid=f3&fs=m:90+t:2&fields=f12,f14,f3,f62,f6,f8,f9")
    body = _http(url, headers=UA_EASTMONEY)
    try:
        d = json.loads(body)
        diff = (d.get("data") or {}).get("diff") or []
        return [{"code": s.get("f12",""), "name": s.get("f14",""),
                 "chg_pct": round(float(s.get("f3") or 0), 2),
                 "main_net": float(s.get("f62") or 0),
                 "amount": float(s.get("f6") or 0),
                 "turnover": float(s.get("f8") or 0),
                 "pe": float(s.get("f9") or 0),
                 } for s in diff]
    except Exception:
        return []


def fetch_sina_news(limit=50):
    """新浪7x24快讯 (mix.sina.com.cn)"""
    url = (f"https://feed.mix.sina.com.cn/api/roll/get"
           f"?pageid=153&lid=2511&num={limit}&page=1")
    body = _http(url)
    try:
        d = json.loads(body)
        items = (d.get("result") or {}).get("data", []) or []
        out = []
        for item in items[:limit]:
            out.append({
                "title": item.get("title", "")[:80],
                "summary": item.get("summary", "")[:200],
                "time": item.get("ctime", ""),
                "source": item.get("media_name", "sina"),
                "url": item.get("url", ""),
                "docid": item.get("docid", ""),
            })
        return out
    except Exception:
        return []


def fetch_cls_news(limit=20):
    """财联社快讯."""
    url = f"https://api3.cls.cn/v1/telegraph?app=CN&os=web&sv=8.5.3&rn={limit}"
    body = _http(url)
    try:
        d = json.loads(body)
        items = (d.get("data") or {}).get("roll_data", []) or []
        out = []
        for item in items[:limit]:
            out.append({
                "title": item.get("title", "")[:80],
                "content": item.get("content", "")[:200],
                "time": item.get("ctime", ""),
                "category": item.get("cat_name", ""),
            })
        return out
    except Exception:
        return []


def write_sina_snapshot(date_str, records):
    """把腾讯快照按东财格式写入 all_YYYY-MM-DD.txt."""
    out = os.path.join(MARKET_DIR, f"all_{date_str}.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# full market {date_str} (tencent + sina) total={len(records)}\n")
        f.write("# code|name|price|chg%|amount|turnover%|PE|volratio|mktcap|floatcap|PB|main_net|main_ratio%|superlarge|super_ratio%|industry\n")
        for code, r in sorted(records.items()):
            bare = code.replace("sh","").replace("sz","").replace("bj","")
            f.write("|".join([
                bare,
                r["name"],
                str(r["price"]),
                str(r["chg_pct"]),
                "0",  # amount
                "0",  # turnover
                "0",  # PE
                "0",  # vol_ratio
                "0",  # mktcap
                "0",  # floatcap
                "0",  # PB
                "0",  # main_net
                "0",  # main_ratio
                "0",  # superlarge
                "0",  # super_ratio
                "",   # industry
            ]) + "\n")
    print(f"  snapshot: {len(records)} stocks -> {out}")
    return out


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now(CST).strftime("%Y-%m-%d")
    ts = datetime.now(CST).strftime("%H%M%S")
    print(f"=== fetch_all_sources {date_str} {ts} ===\n", flush=True)

    results = {}

    # 1. 腾讯实时行情（全市场）
    print("[1/7] 腾讯实时行情 ...", flush=True)
    codes = fetch_tencent_universe()
    if codes:
        snap = fetch_tencent_snapshot(codes)
        results["snapshot"] = snap
        write_sina_snapshot(date_str, snap)
        print(f"  OK: {len(snap)} stocks")
    else:
        print("  SKIP: no universe.csv")

    # 2. 东财涨跌停池
    print("\n[2/7] 东财涨跌停池 ...", flush=True)
    for kind, fname in [("ZT", "ztpool"), ("DT", "dtpool"), ("ZB", "zbpool")]:
        pool = fetch_eastmoney_pool(kind)
        results[fname] = pool
        out = os.path.join(POOL_DIR, f"{date_str}.{fname}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"date": date_str, "kind": kind, "pool": pool, "tc": len(pool)}, f, ensure_ascii=False)
        print(f"  {kind}: {len(pool)} stocks -> {out}")

    # 3. 东财行业板块
    print("\n[3/7] 东财行业板块 ...", flush=True)
    sectors = fetch_eastmoney_sectors()
    results["sectors"] = sectors
    out = os.path.join(SECTOR_DIR, f"{date_str}_rank.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "sectors": sectors, "total": len(sectors)}, f, ensure_ascii=False, indent=2)
    print(f"  OK: {len(sectors)} sectors -> {out}")

    # 4. 新浪7x24快讯
    print("\n[4/7] 新浪7x24快讯 ...", flush=True)
    sina_news = fetch_sina_news(50)
    results["sina_news"] = sina_news
    out = os.path.join(NEWS_DIR, f"{date_str}_sina.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "news": sina_news, "total": len(sina_news)}, f, ensure_ascii=False, indent=2)
    print(f"  OK: {len(sina_news)} items -> {out}")

    # 5. 财联社快讯
    print("\n[5/7] 财联社快讯 ...", flush=True)
    cls_news = fetch_cls_news(20)
    results["cls_news"] = cls_news
    out = os.path.join(NEWS_DIR, f"{date_str}_cls.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "news": cls_news, "total": len(cls_news)}, f, ensure_ascii=False, indent=2)
    print(f"  OK: {len(cls_news)} items -> {out}")

    # 6. 写入汇总 index.json
    print("\n[6/7] 写汇总 index ...", flush=True)
    summary = {
        "date": date_str, "ts": ts,
        "snapshot_count": len(results.get("snapshot", {})),
        "ztpool_count": len(results.get("ztpool", [])),
        "dtpool_count": len(results.get("dtpool", [])),
        "zbpool_count": len(results.get("zbpool", [])),
        "sector_count": len(results.get("sectors", [])),
        "sina_news_count": len(results.get("sina_news", [])),
        "cls_news_count": len(results.get("cls_news", [])),
    }
    out = os.path.join(DATA_DIR, f"index_{date_str}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"  {summary}")

    # 7. 输出摘要
    print(f"\n=== 完成 {date_str} ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    return summary


if __name__ == "__main__":
    main()
