#!/usr/bin/env python3
"""社交/社区数据源采集 v1 (2026-10-12)
可实装的源(全部实测通过):
  1. 雪球 xueqiu.com — cookie流绕waf: 个股quote(量比/换手) + 财务指标
  2. 知乎热榜 api.zhihu.com/topstory/hot-list — 财经情绪参考(30条)
  3. 集思录 jisilu.cn — 投资日历(新股/REITs/转股价下修)
被墙/放弃(有墙): 知乎搜索页403 / 贴吧403 / 东财股吧滑块 / 微博登录墙 / 问财需签名 / 百度股市API 403
输出: data/social/latest.json
"""
import urllib.request, http.cookiejar, json, gzip, os, datetime, time, re

OUT = "/home/agentuser/stockdb/data/social"
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0 Safari/537.36"}
MOBILE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"}


def _gz(raw):
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


# ---------- 雪球 ----------
class Xueqiu:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.op.open(urllib.request.Request("https://xueqiu.com/hq", headers=UA), timeout=10).read()
        self.h = {**UA, "Referer": "https://xueqiu.com/"}

    def quote(self, symbol):
        """symbol如 SZ001300 / SH603810"""
        req = urllib.request.Request(f"https://stock.xueqiu.com/v5/stock/quote.json?symbol={symbol}&extend=detail", headers=self.h)
        d = json.loads(self.op.open(req, timeout=10).read())
        q = d["data"]["quote"]
        return {"name": q["name"], "price": q["current"], "pct": q["percent"],
                "volume_ratio": q.get("volume_ratio"), "turnover": q.get("turnover_rate"),
                "pe": q.get("pe_ttm"), "market_cap": q.get("market_capital")}

    def finance(self, symbol):
        req = urllib.request.Request(f"https://stock.xueqiu.com/v5/stock/finance/cn/indicator.json?symbol={symbol}&type=all", headers=self.h)
        d = json.loads(self.op.open(req, timeout=10).read())
        lst = (d.get("data") or {}).get("list") or []
        if not lst:
            return {}
        r = lst[0]
        return {"report": r.get("last_report_name"), "roe": r.get("avg_roe", [None])[0],
                "eps": r.get("basic_eps", [None])[0]}


# ---------- 知乎热榜 ----------
def zhihu_hot(tag="finance", limit=20):
    u = f"https://api.zhihu.com/topstory/hot-list?limit={limit}&tag={tag}"
    d = json.loads(_gz(urllib.request.urlopen(urllib.request.Request(u, headers=MOBILE), timeout=10).read()))
    out = []
    for x in d.get("data", []):
        t = (x.get("target") or {}).get("title", "")
        out.append({"title": t, "heat": (x.get("detail_text") or "")})
    return out


# ---------- 集思录投资日历 ----------
def jisilu_calendar():
    raw = _gz(urllib.request.urlopen(urllib.request.Request("https://www.jisilu.cn/", headers=UA), timeout=10).read())
    html = raw.decode("utf-8", "ignore")
    # 投资日历条目(新股/REITs/下修)
    items = re.findall(r"<td[^>]*>(\d{6}[^<]{0,20})</td>", html)[:20]
    return {"raw_items": items, "note": "新股/REITs/转股价下修, 详见jisilu.cn"}


def main(symbols=None):
    doc = {"ts": datetime.datetime.now().isoformat(), "date": datetime.date.today().isoformat()}
    # 雪球: 关注池个股quote
    try:
        xq = Xueqiu()
        syms = symbols or []
        if not syms:
            try:
                picks = json.load(open("/home/agentuser/stockdb/data/ai_picks.json"))
                syms = [p["symbol"].replace("sh", "SH").replace("sz", "SZ") for p in picks["picks"][:8]]
            except Exception:
                syms = ["SZ001300", "SH600812"]
        doc["xueqiu_quotes"] = {}
        for s in syms:
            try:
                doc["xueqiu_quotes"][s] = xq.quote(s)
                time.sleep(0.2)
            except Exception:
                pass
        doc["xq_ok"] = len(doc["xueqiu_quotes"])
    except Exception as e:
        doc["xq_ok"] = 0
        doc["xq_err"] = str(e)[:80]
    # 知乎热榜(财经)
    try:
        doc["zhihu_finance"] = zhihu_hot("finance", 15)
        doc["zhihu_ok"] = len(doc["zhihu_finance"])
    except Exception:
        doc["zhihu_ok"] = 0
    # 集思录
    try:
        doc["jisilu"] = jisilu_calendar()
        doc["jisilu_ok"] = 1
    except Exception:
        doc["jisilu_ok"] = 0
    json.dump(doc, open(f"{OUT}/latest.json", "w"), ensure_ascii=False, indent=1)
    json.dump(doc, open(f"{OUT}/{doc['date']}_{datetime.datetime.now():%H%M}.json", "w"), ensure_ascii=False)
    print(f"社交源: 雪球{doc.get('xq_ok',0)}只 知乎{doc.get('zhihu_ok',0)}条 集思录{'✓' if doc.get('jisilu_ok') else '✗'}")


if __name__ == "__main__":
    main()
