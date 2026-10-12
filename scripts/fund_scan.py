#!/usr/bin/env python3
"""基金+币圈数据采集 v1 (2026-10-12) — 9源实测全通:
  ①天天基金批量估值 FundValuationLast(盘中实时) ②新浪分时估值JSONP ③东财历史净值f10
  ④蛋卷detail+growth ⑤天天基金搜索 ⑥场内ETF(腾讯qt已覆盖) ⑦chainthink币圈快讯(破解guest token+X-App-ID=101)
  ⑧深潮techflow快讯(RSC列表→SSR详情) ⑨养基宝/小贝养基=App壳, 上游即①, 无需私有接口
"""
import json, os, datetime, re, gzip, ssl, uuid, urllib.parse, urllib.request

BASE = "/home/agentuser/stockdb"
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

def _get(url, hdrs=None, timeout=15):
    h = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15", "Accept": "*/*", "Accept-Encoding": "gzip"}
    if hdrs: h.update(hdrs)
    req = urllib.request.Request(url, headers=h)
    b = urllib.request.urlopen(req, timeout=timeout, context=ctx).read()
    if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
    return b.decode("utf-8", "ignore")

def _post(url, data, hdrs=None, timeout=15):
    h = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)", "Accept": "application/json", "Content-Type": "application/json"}
    if hdrs: h.update(hdrs)
    r = urllib.request.Request(url, headers=h, data=json.dumps(data).encode(), method="POST")
    b = urllib.request.urlopen(r, timeout=timeout, context=ctx).read()
    if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
    return b.decode("utf-8", "ignore")

# ---------- ① 天天基金批量估值 ----------
def ttjj_valuation(codes):
    """盘中实时估值 {code: {gsz, gszzl}}"""
    out = {}
    for i in range(0, len(codes), 50):
        chunk = codes[i:i+50]
        try:
            t = _get(f"https://fundcomapi.tiantianfunds.com/mm/newCore/FundValuationLast?FCODES={','.join(chunk)}&FIELDS=FCODE,GSZ,GSZZL,NAVDATE")
            d = json.loads(t)
            for x in d.get("data") or []:
                if x.get("GSZ") is not None:
                    out[x["FCODE"]] = {"gsz": x["GSZ"], "gszzl": x["GSZZL"]}
        except Exception:
            pass
    return out

# ---------- ③ 东财历史净值 ----------
def em_nav_history(code, days=30):
    try:
        t = _get(f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex=1&pageSize={days}",
                 {"Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html"})
        d = json.loads(t)
        return [{"date": x["FSRQ"], "nav": float(x["DWJZ"]), "pct": float(x["JZZZL"] or 0)} for x in (d.get("Data", {}) or {}).get("LSJZList", [])]
    except Exception:
        return []

# ---------- ④ 蛋卷 detail + growth ----------
def danjuan_detail(code):
    try:
        d = json.loads(_get(f"https://danjuanfunds.com/djapi/fund/detail/{code}")).get("data", {})
        g = json.loads(_get(f"https://danjuanfunds.com/djapi/fund/growth/{code}?day=30")).get("data", {}).get("fund_nav_growth", [])
        return {"name": d.get("fd_name", ""), "growth": [{"date": x["date"], "nav": float(x["nav"]), "pct": float(x.get("percentage") or 0)} for x in g[-30:]]}
    except Exception:
        return {}

# ---------- ⑤ 基金搜索 ----------
def fund_search(kw, size=8):
    try:
        t = _get(f"https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx?m=1&key={urllib.parse.quote(kw)}&pagesize={size}&pageindex=0",
                 {"Referer": "https://fund.eastmoney.com/"})
        d = json.loads(t)
        return [{"code": x.get("CODE"), "name": x.get("NAME"), "type": x.get("FundBaseInfo", {}).get("FTYPE", "")} for x in d.get("Datas", [])]
    except Exception:
        return []

# ---------- ⑦ chainthink 币圈快讯 ----------
def chainthink_news(num=20):
    try:
        resp = urllib.request.urlopen(urllib.request.Request(
            "https://chainthink.cn/zh-CN/news", headers={"User-Agent": "Mozilla/5.0"}), timeout=15, context=ctx)
        guest = None
        for c in (resp.headers.get_all("Set-Cookie") or []):
            m = re.search(r'cookie_guest_token=([^;]+)', c)
            if m: guest = urllib.parse.unquote(m.group(1))
        h = {"Authorization": guest, "X-App-ID": "101", "X-Device-ID": str(uuid.uuid4()),
             "Content-Type": "application/json", "Referer": "https://chainthink.cn/"}
        t = _post("https://api-v2.chainthink.cn/v1/content/list_home_new", {"num": num}, h)
        d = json.loads(t)
        out = []
        for item in d.get("data", {}).get("list", []):
            info = item.get("info", {})
            title = re.sub(r"<[^>]+>", "", info.get("title") or "")
            abstract = re.sub(r"<[^>]+>", "", info.get("abstract") or "")[:150]
            out.append({"src": "chainthink", "title": title, "text": abstract})
        return out
    except Exception:
        return []

# ---------- ⑧ 深潮 techflow 快讯 ----------
def techflow_news(limit=10):
    out = []
    try:
        t = _get("https://m.techflowpost.com/newsletter", {"RSC": "1"})
        ids = list(dict.fromkeys(re.findall(r'/newsletter/(\d+)', t)))[:limit]
        for nid in ids:
            try:
                d = _get(f"https://m.techflowpost.com/newsletter/{nid}")
                title = (re.search(r'<title>([^<]+)</title>', d) or [None, ""])[1]
                desc = (re.search(r'property="og:description" content="([^"]{5,200})"', d) or [None, ""])[1]
                tm = (re.search(r'article:published_time" content="([^"]+)"', d) or [None, ""])[1]
                if title:
                    out.append({"src": "techflow", "id": nid, "title": title, "text": desc, "ts": tm[:19]})
            except Exception:
                pass
    except Exception:
        pass
    return out

# ---------- 主流程 ----------
def main():
    result = {"ts": datetime.datetime.now().isoformat(), "date": datetime.date.today().isoformat()}

    # 关注基金池(可扩展): 宽基+行业ETF联接+QDII
    watch = ["001594", "161725", "005827", "110022", "260108", "090010", "519674", "161005",
             "320007", "163406", "000961", "001102", "005918", "519066", "002190"]
    result["valuation"] = ttjj_valuation(watch)
    result["valuation_ok"] = len(result["valuation"])

    # 快讯
    result["crypto_news"] = chainthink_news(15)
    result["crypto_ok"] = len(result["crypto_news"])
    result["techflow_news"] = techflow_news(8)
    result["techflow_ok"] = len(result["techflow_news"])

    # 演示: 首只基金净值历史(其余按需)
    if watch:
        result["sample_nav"] = {watch[0]: em_nav_history(watch[0], 10)}
        result["sample_dj"] = danjuan_detail(watch[0])

    os.makedirs(f"{BASE}/data/fund", exist_ok=True)
    json.dump(result, open(f"{BASE}/data/fund/latest.json", "w"), ensure_ascii=False, indent=1)
    print(f"fund_scan: 估值{result['valuation_ok']} 币圈{result['crypto_ok']} 深潮{result['techflow_ok']}")

if __name__ == "__main__":
    main()
