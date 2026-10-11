#!/usr/bin/env python3
# collect sina 7x24 + 财联社电报 + 新华社时政/财经, dedup, tag related watchlist, append to 消息面/
import json, re, urllib.request, urllib.error, os, sys, datetime

ROOT = sys.argv[1]
CN = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
DATE = CN.strftime("%Y-%m-%d")
NOW = CN.strftime("%H:%M")
NEWS = os.environ.get("STOCKS_NEWS_DIR") or os.path.join(ROOT, "消息面")
os.makedirs(NEWS, exist_ok=True)
SEEN = os.path.join(NEWS, ".seen_ids.txt")
seen = set()
if os.path.exists(SEEN):
    seen = {l.strip() for l in open(SEEN, encoding="utf-8") if l.strip()}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
      "Referer": "https://finance.sina.com.cn/7x24/"}

# keyword -> related watchlist tag (ASCII names from tools/watchlist.txt)
KEYMAP = [
    ("华为", "华为链:ZTE/000063,Tuowei/002261,Changshan/000158,DigitalChina/000034"),
    ("鸿蒙", "华为链:ZTE/000063"),
    ("中兴", "ZTE/000063"),
    ("黄金", "黄金:ChifengGold/600988,HunanGold/002155"),
    ("金价", "黄金:ChifengGold/600988,HunanGold/002155"),
    ("石油", "石油:CNOOC/600938,Zhongman/603619,Guanghui/600256"),
    ("原油", "石油:CNOOC/600938,Zhongman/603619,Guanghui/600256"),
    ("油气", "石油:CNOOC/600938,Zhongman/603619,Guanghui/600256"),
    ("汽车", "汽车/新能源:BYD/002594,AutoETF/516110,NEVETF/515030"),
    ("新能源", "新能源:BYD/002594,CATL/300750,NEVETF/515030,NewEnergyETF/516160"),
    ("比亚迪", "BYD/002594"),
    ("宁德", "CATL/300750"),
    ("半导体", "半导体:ChipETF/512760,SemiETF/512480,GigaDevice/603986"),
    ("芯片", "半导体:ChipETF/512760,SemiETF/512480"),
    ("光刻", "半导体:SemiETF/512480"),
    ("通信", "通信:TelecomETF/515880,ZTE/000063"),
    ("5G", "通信:TelecomETF/515880"),
    ("光模块", "光模块:Innolight/300308,Eoptolink/300502"),
    ("中际旭创", "Innolight/300308"),
    ("新易盛", "Eoptolink/300502"),
    ("电力", "电力:Mengdian/600863,Wanneng/000543,Huaneng/600011,LeshanPower/600644"),
    ("银行", "银行:BankETF/512800"),
    ("券商", "券商:BrokerETF/512880"),
    ("证券", "券商:BrokerETF/512880"),
    ("科创", "科创:STAR50ETF/588000"),
    ("创业板", "创业:ChiNextETF/159915"),
    ("白酒", "酒:LiquorETF/512690,Wuliangye/000858"),
    ("酒", "酒:LiquorETF/512690"),
    ("消费", "消费:Haier/600690,Gree/000651,Wuliangye/000858"),
    ("光伏", "光伏:SolarETF/515790,NewEnergyETF/516160"),
    ("太阳能", "光伏:SolarETF/515790"),
    ("军工", "军工:DefenseETF/512660"),
    ("国防", "军工:DefenseETF/512660"),
    ("传媒", "传媒:MediaETF/512980"),
    ("有色", "有色:MetalETF/512400"),
    ("金属", "有色:MetalETF/512400"),
    ("科技", "科技:TechETF/515000"),
    ("恒瑞", "Hengrui/600276"),
    ("药明", "WuXiAppTec/603259"),
    ("创新药", "医药:PharmaETF/512010,HealthETF/512170"),
    ("医药", "医药:PharmaETF/512010,HealthETF/512170"),
    ("CRO", "医药:WuXiAppTec/603259"),
    ("海尔", "Haier/600690"),
    ("格力", "Gree/000651"),
    ("五粮液", "Wuliangye/000858"),
    ("AI", "AI:算力/光模块/半导体链"),
    ("算力", "算力:光模块/数据中心"),
    ("机器人", "机器人:拓维/常山/中兴"),
    ("低空", "低空经济:通信/军工"),
    ("重组", "重组概念:个股事件驱动"),
    ("并购", "并购概念:个股事件驱动"),
]


def tag(title):
    hits = []
    for kw, t in KEYMAP:
        if kw in title:
            hits.append(t)
    seen_t = set()
    out = []
    for h in hits:
        if h not in seen_t:
            seen_t.add(h)
            out.append(h)
    return out


new_items = []

def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def http_text(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")

# ── source 1: 新浪 7x24 财经 ─────────────────────────────
try:
    data = http_json("https://app.cj.sina.com.cn/api/news/pc?page=1&num=300")
    feed = data.get("result", {}).get("data", {}).get("feed", {}).get("list", [])
    for x in feed:
        nid = str(x.get("id", ""))
        key = f"sina:{nid}"
        if not nid or key in seen:
            continue
        seen.add(key)
        title = (x.get("rich_text", "") or "").replace("\n", " ").strip()
        ctime = x.get("create_time", "") or ""
        tags = tag(title)
        rel = ", ".join(tags) if tags else "—"
        new_items.append((ctime, f"- [{ctime}] {title}  \n  - 涉及: {rel}  \n  - 来源: 新浪7x24 | id={nid}"))
except Exception as e:
    print(f"ERR sina: {e}", file=sys.stderr)

# ── source 2: 东财 7x24 (财经 type=8 / 政策 type=11 / 产经 type=12) ──────────
for ntype, label in [(8, "em-财经"), (11, "em-政策"), (12, "em-产经")]:
    try:
        url = (f"https://np-listapi.eastmoney.com/comm/wap/getListInfo"
               f"?type={ntype}&pageSize=50&pageNo=1&client=web&source=XHH")
        data = http_json(url)
        items = ((data.get("data") or {}).get("list")) or []
        for x in items:
            nid = str(x.get("id", ""))
            key = f"{label}:{nid}"
            if not nid or key in seen:
                continue
            seen.add(key)
            title = (x.get("title", "") or "").replace("\n", " ").strip()
            if not title:
                continue
            ctime = str(x.get("showtime") or x.get("ctime", ""))[:19]
            tags = tag(title)
            rel = ", ".join(tags) if tags else "—"
            new_items.append((ctime, f"- [{ctime}] 【{label}】{title}  \n  - 涉及: {rel}  \n  - 来源: 东财{label} | id={nid}"))
    except Exception as e:
        print(f"ERR em-{label}: {e}", file=sys.stderr)

# ── source 3: 财联社电报 (财经快讯) ─────────────────────────
try:
    data = http_json("https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&sv=8.4.6&sign=9dc7288db1f2472d0d42e1ea54f7411f")
    for x in (data.get("data", {}).get("roll_data", []))[:100]:
        nid = str(x.get("id", ""))
        key = f"cls:{nid}"
        if not nid or key in seen:
            continue
        seen.add(key)
        title = (x.get("title") or x.get("content") or "").strip()
        if not title:
            continue
        ts = str(x.get("ctime", "") or "")[:19]
        tags = tag(title)
        rel = ", ".join(tags) if tags else "—"
        new_items.append((ts, f"- [{ts}] 【财联社】{title}  \n  - 涉及: {rel}  \n  - 来源: 财联社电报 | id={nid}"))
except Exception as e:
    print(f"ERR cls: {e}", file=sys.stderr)

# ── source 3: 中国政府网 (政事/政策) ───────────────────────────
try:
    html = http_text("https://www.gov.cn/zhengce/zuixin.htm", timeout=15)
    # 提取政策条目: <li class="list01"><a ...>标题</a></li> 或时间戳格式
    for m in re.finditer(r'<li\s+class="list01"[^>]*>.*?<a[^>]*>([^<]+)</a>.*?<span>([\d/]+)</span>', html, re.S):
        title, ts = m.group(1).strip(), m.group(2)
        key = f"gov:{title[:40]}"
        if key in seen:
            continue
        seen.add(key)
        tags = tag(title)
        rel = ", ".join(tags) if tags else "—"
        new_items.append((ts, f"- [{ts}] 【政府网】{title}  \n  - 涉及: {rel}  \n  - 来源: 中国政府网 | id={key}"))
except Exception as e:
    print(f"ERR gov: {e}", file=sys.stderr)

new_items.sort(key=lambda a: a[0])
daily = os.path.join(NEWS, f"{DATE}.md")
if not os.path.exists(daily):
    open(daily, "w", encoding="utf-8").write(f"# news 7x24 {DATE}\n")

with open(SEEN, "w", encoding="utf-8") as f:
    f.write("\n".join(list(seen)[-8000:]) + "\n")

if new_items:
    with open(daily, "a", encoding="utf-8") as f:
        f.write(f"\n## {NOW} (新增 {len(new_items)})\n")
        for _, line in new_items:
            f.write(line + "\n")
    print(len(new_items))
else:
    print(0)
