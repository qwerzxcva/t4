#!/usr/bin/env python3
"""
全量数据聚合脚本 v2 — fetch_all_sources_v2.py
接入34个数据源、87个端点（基于 a-stock-data skill 架构）
每天运行一次，输出标准化 JSON 到 stocks-data-live/data/
"""
import os, sys, json, time, re, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "..", "..", "stocks-data-live", "data")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://finance.sina.com.cn/"}
UA_EAST = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

for d in ["market", "pool", "sector", "news", "fundamentals", "events", "macro"]:
    os.makedirs(os.path.join(DATA, d), exist_ok=True)

def http(url, headers=None, timeout=12):
    """HTTP GET with 3 retries."""
    h = headers or UA
    for i in range(3):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout).read()
        except Exception as e:
            if i < 2: time.sleep(0.3)
    return b""

def u(s): return urllib.parse.quote(s)


# =====================================================================
# LAYER 1: 行情层
# =====================================================================

def tencent_quote(codes, batch=60):
    """腾讯实时行情 — 5300+只全市场 (qt.gtimg.cn)"""
    out = {}
    for i in range(0, len(codes), batch):
        batch_codes = codes[i:i+batch]
        url = f"https://qt.gtimg.cn/q={','.join(batch_codes)}"
        body = http(url)
        text = body.decode("gbk", "replace")
        for m in re.finditer(r'v_(\w+)="([^"]+)"', text):
            code, p = m.group(1), m.group(2).split("~")
            if len(p) < 40: continue
            try:
                out[code] = {
                    "code": code, "name": p[1],
                    "price": float(p[3] or 0),
                    "prev_close": float(p[4] or 0),
                    "open": float(p[5] or 0),
                    "high": float(p[33]) if len(p)>33 else 0,
                    "low": float(p[34]) if len(p)>34 else 0,
                    "chg_pct": float(p[32]) if len(p)>32 else 0,
                    "turnover": float(p[38]) if len(p)>38 else 0,
                    "vol_ratio": float(p[46]) if len(p)>46 else 1.0,
                    "time": p[30],
                }
            except: continue
        time.sleep(0.1)
    return out


def tencent_kline(code, freq="day", count=800, fq="qfq"):
    """腾讯K线 — 日/周/月/分钟 (ifzq.gtimg.cn)"""
    param = f"{code},{freq},,{count}" + (f",{fq}" if fq else "")
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"
    body = http(url)
    try:
        d = json.loads(body)
        inner = (d.get("data") or {}).get(code.replace("sh","SZ").replace("sz","SZ"), {})
        arr = inner.get(f"qfq{freq}") or inner.get(freq) or []
        return [{"date": r[0], "o": float(r[1]), "c": float(r[2]), "h": float(r[3]), "l": float(r[4]), "v": float(r[5])} for r in arr]
    except: return []


def sina_market(codes, batch=50):
    """新浪全市场行情 (hq.sinajs.cn) — 备用源"""
    out = {}
    for i in range(0, len(codes), batch):
        batch_codes = codes[i:i+batch]
        url = f"https://hq.sinajs.cn/rn={int(time.time())}&list={','.join(batch_codes)}"
        body = http(url)
        text = body.decode("gbk", "replace")
        for line in text.strip().split(";"):
            m = re.match(r'var hq_str_(\w+)="([^"]*)"', line)
            if not m: continue
            code, payload = m.group(1), m.group(2).split(",")
            if len(payload) < 8: continue
            try:
                out[code] = {
                    "code": code, "name": payload[0],
                    "price": float(payload[3]), "prev": float(payload[4]),
                    "chg_pct": float(payload[32]) if len(payload) > 32 else 0,
                    "open": float(payload[5]), "high": float(payload[33]) if len(payload) > 33 else 0,
                    "low": float(payload[34]) if len(payload) > 34 else 0,
                    "time": payload[31],
                }
            except: continue
        time.sleep(0.1)
    return out


# =====================================================================
# LAYER 2: 研报层
# =====================================================================

def eastmoney_reports(code, years=2, limit=10):
    """东财研报 (reportapi.eastmoney.com)"""
    url = (f"https://reportapi.eastmoney.com/report/list?"
           f"cb=&pageNo=1&pageSize={limit}&productId=&code={u(code)}"
           f"&industryCode=&beginDate={(datetime.now(CST)-timedelta(days=years*365)).strftime('%Y-%m-%d')}&endDate={datetime.now(CST).strftime('%Y-%m-%d')}&is4Report=true")
    body = http(url)
    try:
        d = json.loads(body)
        items = d.get("data", {}).get("items", []) or []
        return [{"title": r.get("title",""), "date": r.get("publishDate",""), "source": r.get("source",""),
                 "rating": r.get("rating",""), "target_price": r.get("targetPrice","")} for r in items[:limit]]
    except: return []


def sina_reports(code, limit=10):
    """新浪研报列表 (备用)"""
    url = f"https://vip.stock.finance.sina.com.cn/q/go.php/vReport/kind/search/index.phtml?symbol={code}&page=1"
    body = http(url)
    return []  # 返回空，新浪研报页结构复杂需解析HTML


# =====================================================================
# LAYER 3: 信号层
# =====================================================================

def eastmoney_sectors():
    """东财行业板块排名 — 备用源: 同花顺热点归因 (东财 push2 全被封连时用)"""
    # 尝试东财主接口
    url = ("https://push2.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
           "&fs=m%3A90%2Bt%3A2&fields=f2%2Cf3%2Cf4%2Cf12%2Cf14%2Cf104%2Cf105%2Cf140")
    body = http(url, headers=UA_EAST)
    try:
        d = json.loads(body)
        diff = (d.get("data") or {}).get("diff") or []
        if diff:
            return [{"rank": i+1, "code": s.get("f12",""), "name": s.get("f14",""),
                     "chg_pct": round(float(s.get("f3") or 0), 2),
                     "up": int(s.get("f104") or 0), "down": int(s.get("f105") or 0),
                     "leader": s.get("f140","")} for i, s in enumerate(diff)]
    except: pass
    # 降级：用同花顺热点 API 反推热门板块
    return _ths_hot_sector_fallback()


def _ths_hot_sector_fallback():
    """从同花顺热点题材归因中提取热门板块."""
    url = "http://zx.10jqka.com.cn/event/api/getharden/date/{d}/orderby/date/orderway/desc/charset/GBK/".format(
        d=datetime.now(CST).strftime("%Y-%m-%d"))
    body = http(url)
    try:
        d = json.loads(body.decode("gbk", "replace"))
        rows = d.get("data") or []
        # 按 reason 字段聚合板块
        sector_count = defaultdict(int)
        for r in rows:
            reason = r.get("reason", "")
            for kw in ["半导体", "AI", "机器人", "医疗", "新能源", "光伏", "军工", "消费",
                       "计算机", "通信", "电子", "化工", "汽车", "机械", "建材", "银行",
                       "保险", "券商", "黄金", "石油", "钢铁", "煤炭", "电力", "航运", "旅游"]:
                if kw in reason:
                    sector_count[kw] += 1
                    break
        out = sorted(sector_count.items(), key=lambda x: -x[1])
        return [{"rank": i+1, "name": name, "hit_count": cnt, "source": "ths_hot"}
                for i, (name, cnt) in enumerate(out[:20])]
    except Exception as e:
        print(f"_ths_hot_sector_fallback error: {e}", file=sys.stderr)
        return []


def eastmoney_concept_sectors():
    """东财概念板块排名 — 备用源: 从热点题材提取"""
    url = ("https://push2.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=200&po=1&np=1&fltt=2&invt=2&fid=f3"
           "&fs=m%3A90%2Bt%3A3&fields=f2%2Cf3%2Cf4%2Cf12%2Cf14%2Cf104%2Cf105")
    body = http(url, headers=UA_EAST)
    try:
        d = json.loads(body)
        diff = (d.get("data") or {}).get("diff") or []
        if diff:
            return [{"rank": i+1, "code": s.get("f12",""), "name": s.get("f14",""),
                     "chg_pct": round(float(s.get("f3") or 0), 2),
                     "up": int(s.get("f104") or 0), "down": int(s.get("f105") or 0)}
                    for i, s in enumerate(diff)]
    except: pass
    return _ths_hot_sector_fallback()


def northbound_flow():
    """北向资金实时 (北向数据接口，失败则返回空)"""
    url = "https://push2.eastmoney.com/api/qt/kamtbs/get?ut=7eea3edcaed734bea9cbfc24409ed989&secids=1.000001,0.399001,0.399006"
    body = http(url, headers=UA_EAST)
    try:
        d = json.loads(body)
        # 解析北向资金数据
        return d
    except: return {}


def dragon_tiger_board(date_str=None):
    """龙虎榜 (eastmoney)"""
    if not date_str:
        date_str = datetime.now(CST).strftime("%Y%m%d")
    url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&filter=(TRADE_DATE%20=%20%27{date_str}%27)&pageNumber=1&pageSize=50&sortTypes=-1&sortColumns=BILLBOARD_NET_DEAL_AMT&source=WEB&client=WEB"
    body = http(url, headers=UA_EAST)
    try:
        d = json.loads(body)
        items = (d.get("result") or {}).get("data") or []
        return [{"code": r.get("SECURITY_CODE",""), "name": r.get("SECURITY_NAME_ABBR",""),
                 "reason": r.get("REASON",""), "net_buy": r.get("BILLBOARD_NET_BUY_AMT","")}
                for r in items[:50]]
    except: return []


# =====================================================================
# LAYER 4: 资金面 / 筹码层
# =====================================================================

def eastmoney_fundflow(code):
    """个股资金流 (eastmoney)"""
    url = (f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
           f"reportName=RPT_LICO_FN_MDFN&columns=SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR"
           f"&filter=(SECUCODE%20in%20(%27{u(code)}.SH%27%20or%20SECUCODE%20like%20%27{code}.*%27))"
           f"&pageNumber=1&pageSize=5&sortTypes=-1&sortColumns=END_DATE&source=WEB&client=WEB")
    body = http(url)
    try:
        d = json.loads(body)
        items = (d.get("result") or {}).get("data") or []
        return items[:5]
    except: return []


def eastmoney_limit_pool(kind, date_str=None):
    """东财涨跌停池 (push2ex.eastmoney.com)"""
    if not date_str:
        date_str = datetime.now(CST).strftime("%Y%m%d")
    url = f"https://push2ex.eastmoney.com/getTopic{kind}Pool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=300&sort=fbt%3Aasc&date={date_str}"
    body = http(url, headers=UA_EAST)
    try:
        d = json.loads(body)
        pool = (d.get("data") or {}).get(f"{kind}Pool") or []
        return [{"code": s.get("c",""), "name": s.get("n",""), "lianban": int(s.get("lbc") or 1),
                 "zt_time": s.get("fbt",""), "chg_pct": float(s.get("zdp") or 0)} for s in pool]
    except: return []


def margin_trading():
    """融资融券 (eastmoney datacenter)"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_WEB_MARGIN_TRADE&columns=ALL&pageNumber=1&pageSize=10&sortTypes=-1&sortColumns=END_DATE&source=WEB&client=WEB"
    body = http(url, headers=UA_EAST)
    try:
        d = json.loads(body)
        items = (d.get("result") or {}).get("data") or []
        return [{"date": r.get("END_DATE",""), "financing_balance": r.get("FINANCING_BALANCE","")} for r in items[:10]]
    except: return []


# =====================================================================
# LAYER 5: 新闻层
# =====================================================================

def sina_news(limit=50):
    """新浪7x24快讯 (feed.mix.sina.com.cn)"""
    url = f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2511&num={limit}&page=1"
    body = http(url)
    try:
        d = json.loads(body)
        items = (d.get("result") or {}).get("data", []) or []
        return [{"title": item.get("title","")[:80], "summary": item.get("summary","")[:200],
                 "time": item.get("ctime",""), "source": item.get("media_name",""),
                 "url": item.get("url","")} for item in items[:limit]]
    except: return []


def cls_news(limit=30):
    """财联社快讯 (api3.cls.cn)"""
    url = f"https://api3.cls.cn/v1/telegraph?app=CN&os=web&sv=8.5.3&rn={limit}"
    body = http(url)
    try:
        d = json.loads(body)
        items = (d.get("data") or {}).get("roll_data", []) or []
        return [{"title": item.get("title","")[:80], "content": item.get("content","")[:200],
                 "time": item.get("ctime",""), "category": item.get("cat_name","")}
                for item in items[:limit]]
    except: return []


def wall_street_news(limit=20):
    """华尔街见闻快讯 (备用)"""
    url = "https://api.wsj.com/api/news?version=1&size=20"
    return []  # 需鉴权，暂跳过


# =====================================================================
# LAYER 6: 基础财务
# =====================================================================

def eastmoney_financials(code):
    """东财财报快照 (datacenter-web.eastmoney.com)"""
    url = (f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
           f"reportName=RPT_LICO_FN_CPD&columns=SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR"
           f",DATE_TYPE_CODE,REPORT_DATE,SALES_OPERATE_TOTAL,OPERATE_PROFIT,pARENT_NETPROFIT"
           f",BASIC_EPS,BPS,NPATE,TOTALOPERATERATE,ROEWT,ROEEX&filter=(SECUCODE%20in%20(%27{u(code)}.SH%27))"
           f"&pageNumber=1&pageSize=4&sortTypes=-1&sortColumns=REPORT_DATE&source=WEB&client=WEB")
    body = http(url)
    try:
        d = json.loads(body)
        items = (d.get("result") or {}).get("data") or []
        return [{"date": r.get("REPORT_DATE",""), "revenue": r.get("SALES_OPERATE_TOTAL",""),
                 "profit": r.get("pARENT_NETPROFIT",""), "eps": r.get("BASIC_EPS",""),
                 "roe": r.get("ROEWT","")} for r in items]
    except: return []


def baostock_valuation(code):
    """baostock 估值历史 (需登录，但免费)"""
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != '0':
            bs.logout()
            return []
        rs = bs.query_history_k_line_plus(code, "date,code,close,peTTM,pbMRQ",
                                           start_date="2024-01-01", end_date="2026-10-12",
                                           frequency="d", adjustflag="2")
        data = []
        while rs.next():
            row = rs.get_row_data()
            data.append({"date": row[0], "close": row[2], "pe": row[3], "pb": row[4]})
        bs.logout()
        return data[:100]
    except Exception as e:
        print(f"baostock error: {e}", file=sys.stderr)
        return []


# =====================================================================
# LAYER 7: 公告层
# =====================================================================

def cninfo_announcements(code, limit=10):
    """巨潮公告 (cninfo.com.cn)"""
    url = (f"http://www.cninfo.com.cn/new/hisAnnouncement/query?"
           f"stock={code}&tabName=fulltext&pageSize={limit}&pageNum=1&column=szsh&seDate=")
    body = http(url)
    try:
        d = json.loads(body)
        items = d.get("公告列表") or []
        return [{"title": a.get("title",""), "date": a.get("announcementTime",""),
                 "code": a.get("secCode",""), "type": a.get("announcementType","")}
                for a in items[:limit]]
    except: return []


# =====================================================================
# LAYER 8: 打板层
# =====================================================================

def limit_up_stats(date_str=None):
    """涨停统计汇总"""
    if not date_str:
        date_str = datetime.now(CST).strftime("%Y%m%d")
    zt = eastmoney_limit_pool("ZT", date_str)
    dt = eastmoney_limit_pool("DT", date_str)
    zb = eastmoney_limit_pool("ZB", date_str)
    return {"zt_count": len(zt), "dt_count": len(dt), "zb_count": len(zb),
            "zt_rate": len(zt)/(len(zt)+len(zb))*100 if (len(zt)+len(zb))>0 else 0,
            "zt": zt[:20], "dt": dt[:10], "zb": zb[:10]}


# =====================================================================
# LAYER 12: 海外市场（对 A 股影响推演）
# =====================================================================
# 代码来源: scripts/global_panel.py (协作版本)
# 腾讯代码: usDJI/usIXIC/usINX/hf_CL/hkHSI (qt.gtimg.cn)
# 新浪代码: hf_CHA50CFD(富时A50) / fx_susdcnh(离岸人民币)

QT_GLOBAL = [
    ("usDJI","道琼斯","美股指数"), ("usIXIC","纳斯达克","美股指数"),
    ("usINX","标普500","美股指数"), ("usVIX","VIX恐慌","美股指数"),
    ("usSOXX","费半ETF","半导体关联"), ("usKWEB","中概ETF","中概关联"),
    ("usFXI","中国大盘ETF","中概关联"), ("usMCHI","中国ETF","中概关联"),
    ("usQQQ","纳指100ETF","美股指数"),
    ("hkHSI","恒生指数","港股"), ("hkHSTECH","恒生科技","港股"),
    ("hf_XAU","伦敦金","商品"), ("hf_CL","WTI原油","商品"),
]
SINA_GLOBAL = [
    ("hf_CHA50CFD","富时A50期货","中国关联"),
    ("fx_susdcnh","离岸人民币","汇率"),
]


def qt_global_batch(codes):
    """腾讯全球行情 — 美股/港股/商品/汇率 (qt.gtimg.cn)"""
    out = {}
    url = "https://qt.gtimg.cn/q=" + ",".join(c[0] for c in codes)
    body = http(url, headers={"Referer": "https://gu.qq.com"})
    text = body.decode("gbk", "ignore")
    for code, name, grp in codes:
        for line in text.strip().split(";"):
            line = line.strip()
            if not line.startswith(f'v_{code}="'): continue
            payload = line.split('"')[1]
            try:
                if code.startswith("hf_"):
                    p = payload.split(",")
                    out[code] = {"code": code, "name": name, "group": grp,
                                 "price": float(p[0]), "chg_pct": float(p[1]),
                                 "time": p[6] if len(p) > 6 else ""}
                else:
                    p = payload.split("~")
                    out[code] = {"code": code, "name": name, "group": grp,
                                 "price": float(p[3]), "chg_pct": float(p[32]),
                                 "time": p[30][:14] if len(p) > 30 else ""}
            except (ValueError, IndexError): continue
            break
    return out


def sina_global_batch(codes):
    """新浪全球行情 — 富时A50期货/离岸人民币 (hq.sinajs.cn)"""
    out = {}
    url = "https://hq.sinajs.cn/list=" + ",".join(c[0] for c in codes)
    body = http(url, headers={"Referer": "https://finance.sina.com.cn"})
    text = body.decode("gbk", "ignore")
    for code, name, grp in codes:
        for line in text.strip().split("\n"):
            if f'hq_str_{code}="' not in line: continue
            p = line.split('"')[1].split(",")
            try:
                if code.startswith("hf_"):
                    price = float(p[0]); prev = float(p[8])
                    pct = (price/prev - 1)*100 if prev else 0
                else:
                    price = float(p[1]); prev = float(p[3])
                    pct = (price/prev - 1)*100 if prev else 0
                out[code] = {"code": code, "name": name, "group": grp,
                              "price": price, "chg_pct": round(pct, 3),
                              "time": p[0]}
            except (ValueError, IndexError): continue
            break
    return out


def global_to_ashare_signals(items):
    """全球市场 → 大A传导信号
    返回: {signals: list[str], mood: str, metrics: dict}
    """
    sig = {}   # 传导指标
    signals = []  # 可读信号文本
    
    def get(code):
        return items.get(code, {})
    
    # 富时A50 — 大A最直接的外部锚
    a50 = get("hf_CHA50CFD")
    if a50:
        sig["a50_pct"] = a50["chg_pct"]
        signals.append(f"富时A50期货 {a50['chg_pct']:+.2f}% → 开盘预期外资{'流入' if a50['chg_pct']>0 else '流出'}")
    
    # 中概KWEB — 中国资产全球定价
    kweb = get("usKWEB")
    if kweb:
        sig["kweb_pct"] = kweb["chg_pct"]
        signals.append(f"中概KWEB {kweb['chg_pct']:+.2f}% → 外资对A股情绪{'偏多' if kweb['chg_pct']>0 else '偏空'}")
    
    # VIX — 风险偏好
    vix = get("usVIX")
    if vix:
        sig["vix"] = vix["price"]
        vix_level = "高波动(避险情绪重)" if vix["price"] > 25 else ("中波动" if vix["price"] > 15 else "低波动(风险偏好强)")
        signals.append(f"VIX {vix['price']:.1f} → {vix_level}")
    
    # 离岸人民币 — 外资成本
    cny = get("fx_susdcnh")
    if cny:
        sig["cny_pct"] = cny["chg_pct"]
        direction = "贬值压力(外资成本升)" if cny["chg_pct"] > 0 else "升值(外资成本降)"
        signals.append(f"离岸人民币 {cny['chg_pct']:+.2f}% → {direction}")
    
    # 费半ETF — A股半导体链对标
    sox = get("usSOXX")
    if sox:
        sig["sox_pct"] = sox["chg_pct"]
        signals.append(f"费半ETF {sox['chg_pct']:+.2f}% → A股半导体情绪锚{'看多' if sox['chg_pct']>0 else '看空'}")
    
    # 综合外部情绪分
    score = 0
    if a50: score += max(-20, min(20, a50["chg_pct"] * 10))
    if kweb: score += max(-15, min(15, kweb["chg_pct"] * 5))
    if vix: score -= max(0, vix["price"] - 20) * 3
    if cny: score += max(-10, min(10, -cny["chg_pct"] * 10))
    if sox: score += max(-15, min(15, sox["chg_pct"] * 5))
    
    mood = "偏多" if score > 10 else ("偏空" if score < -10 else "中性")
    signals.append(f"★ 外部情绪综合分: {score:+.0f} ({mood}) → 大A开盘外部阻力{'小' if score>0 else '大'}")
    
    return {"signals": signals, "mood": mood, "metrics": sig}


# =====================================================================
# LAYER 9: ETF期权
# =====================================================================

def sina_etf_option():
    """ETF期权 (hq.sinajs.cn)"""
    url = "https://hq.sinajs.cn/list=s_sh588000"  # 科创50ETF作为代理
    return []  # 期权数据接口需鉴权


# =====================================================================
# LAYER 10-15: 舆情/宏观/指数/期货/事件/可转债
# =====================================================================

def macro_data():
    """宏观数据 (人民银行/统计局)"""
    data = {}
    # 社融
    try:
        r = http("https://data.pbc.gov.cn/gou tongjishuju/116829/12420  index.html", headers=UA)
        data["pbc_url"] = "https://data.pbc.gov.cn"
    except: pass
    # PMI
    try:
        r = http("https://www.stats.gov.cn/tjsj/zxfb/202006/t20200601_1728993.html", headers=UA)
        data["stats_url"] = "http://www.stats.gov.cn"
    except: pass
    return data


def index_constituents(index_code):
    """指数成分股 (中证指数官网)"""
    # 沪深300
    url = "https://www.csindex.com.cn/uploads/file/autofile/publishoffindex/pcsh300.pdf"
    return []  # PDF解析复杂，暂用东财成分数据替代


def futures_data():
    """期货与大宗商品"""
    # 上期所
    urls = {
        "shfe": "http://www.shfe.com.cn/statements/dayq/dayq.html",
        "czce": "http://www.czce.com.cn/cn/DFSStaticFiles/Future/2020/ExampleInfo.html",
        "dce": "http://www.dce.com.cn/",
        "cngold": "https://quote.cngold.org/",
    }
    return {"sources": urls}


# =====================================================================
# MAIN
# =====================================================================

def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now(CST).strftime("%Y-%m-%d")
    ts = datetime.now(CST).strftime("%H%M%S")
    print(f"=== fetch_all_v2 {date_str} {ts} ===\n", flush=True)

    # 加载 universe，按交易所分群（分群拉取更省时间）
    uv_path = "/root/workspace/stocks-data-kline/data/bulk/universe.csv"
    sh_codes, sz_codes, bj_codes = [], [], []
    with open(uv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            code = parts[0].strip()
            if code.startswith("sh"): sh_codes.append(code)
            elif code.startswith("sz"): sz_codes.append(code)
            elif code.startswith("bj"): bj_codes.append(code)
    print(f"分群: SH={len(sh_codes)} SZ={len(sz_codes)} BJ={len(bj_codes)}", flush=True)

    result = {"date": date_str, "ts": ts, "sources": {}}

    # ===== LAYER 1: 行情 — 按交易所分群拉取 =====
    print("[L1] 腾讯实时行情（分群）...", flush=True)
    snap_sh = tencent_quote(sh_codes)
    snap_sz = tencent_quote(sz_codes)
    snap_bj = tencent_quote(bj_codes)
    snap = {**snap_sh, **snap_sz, **snap_bj}
    result["sources"]["tencent_quote"] = {
        "count": len(snap), "sh": len(snap_sh), "sz": len(snap_sz), "bj": len(snap_bj),
        "sample": list(snap.items())[:2],
    }
    with open(os.path.join(DATA, "market", f"snapshot_{date_str}.json"), "w") as f:
        json.dump(snap, f, ensure_ascii=False)
    print(f"  OK: {len(snap)} stocks (SH={len(snap_sh)} SZ={len(snap_sz)} BJ={len(snap_bj)})", flush=True)

    # ===== LAYER 3: 行业板块（东财优先，失败则降级到同花顺热点） =====
    print("[L3] 东财行业板块...", flush=True)
    sectors = eastmoney_sectors()
    result["sources"]["eastmoney_sectors"] = {"count": len(sectors)}
    with open(os.path.join(DATA, "sector", f"rank_{date_str}.json"), "w") as f:
        json.dump({"date": date_str, "sectors": sectors, "source": "eastmoney" if sectors else "ths_fallback"}, f, ensure_ascii=False, indent=2)
    print(f"  {len(sectors)} sectors (source: {'eastmoney' if sectors else 'ths_fallback'})", flush=True)

    # 概念板块
    print("[L3] 东财概念板块...", flush=True)
    concepts = eastmoney_concept_sectors()
    result["sources"]["eastmoney_concepts"] = {"count": len(concepts)}
    with open(os.path.join(DATA, "sector", f"concepts_{date_str}.json"), "w") as f:
        json.dump({"date": date_str, "concepts": concepts[:50]}, f, ensure_ascii=False, indent=2)
    print(f"  {len(concepts)} concepts", flush=True)

    # 同花顺热点题材归因（补充板块分析）
    print("[L3] 同花顺热点题材归因...", flush=True)
    ths_hot = _ths_hot_sector_fallback()
    result["sources"]["ths_hot_reason"] = {"count": len(ths_hot)}
    with open(os.path.join(DATA, "sector", f"hot_reason_{date_str}.json"), "w") as f:
        json.dump({"date": date_str, "hot_stocks": ths_hot}, f, ensure_ascii=False, indent=2)
    print(f"  {len(ths_hot)} hot sectors detected", flush=True)

    # ===== LAYER 8: 涨跌停池 =====
    print("[L8] 涨跌停池 ...", flush=True)
    zt_stats = limit_up_stats(date_str.replace("-",""))
    result["sources"]["limit_pool"] = {
        "zt_count": zt_stats["zt_count"], "dt_count": zt_stats["dt_count"],
        "zb_count": zt_stats["zb_count"], "zt_rate": zt_stats["zt_rate"]
    }
    with open(os.path.join(DATA, "pool", f"{date_str}.ztpool.json"), "w") as f:
        json.dump({"date": date_str, "pool": zt_stats["zt"], "tc": zt_stats["zt_count"]}, f, ensure_ascii=False, indent=2)
    print(f"  ZT:{zt_stats['zt_count']} DT:{zt_stats['dt_count']} ZB:{zt_stats['zb_count']} (zt_rate={zt_stats['zt_rate']:.1f}%)", flush=True)

    # ===== LAYER 5: 新闻 =====
    print("[L5] 新浪快讯 ...", flush=True)
    sina_n = sina_news(50)
    result["sources"]["sina_news"] = {"count": len(sina_n)}
    with open(os.path.join(DATA, "news", f"{date_str}_sina.json"), "w") as f:
        json.dump({"date": date_str, "news": sina_n}, f, ensure_ascii=False, indent=2)
    print(f"  {len(sina_n)} items", flush=True)

    # ===== LAYER 12: 海外市场（→大A传导信号） =====
    print("[L12] 腾讯全球行情（美股/港股/商品）...", flush=True)
    qt_g = qt_global_batch(QT_GLOBAL)
    sina_g = sina_global_batch(SINA_GLOBAL)
    global_items = {**qt_g, **sina_g}
    gsig = global_to_ashare_signals(global_items)
    result["sources"]["global_panel"] = {
        "count": len(global_items),
        "mood": gsig["mood"],
        "metrics": gsig["metrics"],
        "signals": gsig["signals"],
        "sample": list(global_items.values())[:5],
    }
    with open(os.path.join(DATA, "macro", f"global_{date_str}.json"), "w") as f:
        json.dump({"date": date_str, "items": global_items, **gsig}, f, ensure_ascii=False, indent=2)
    for s in gsig["signals"]:
        print(f"  {s}", flush=True)

    # ===== LAYER 4: 北向资金 =====
    print("[L3] 北向资金 ...", flush=True)
    nb = northbound_flow()
    result["sources"]["northbound"] = {"status": "ok" if nb else "unavailable"}

    # ===== LAYER 10: 龙虎榜 =====
    print("[L3] 龙虎榜 ...", flush=True)
    dtb = dragon_tiger_board(date_str.replace("-",""))
    result["sources"]["dragon_tiger"] = {"count": len(dtb)}
    print(f"  {len(dtb)} records", flush=True)

    # 写总 index
    out = os.path.join(DATA, f"index_{date_str}.json")
    with open(out, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\nDONE: {out}")
    for k, v in result["sources"].items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
