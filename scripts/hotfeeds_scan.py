# 热榜聚合器 v1: 已通源=同花顺快讯/抖音/B站/头条/新浪7x24/财联社(在news_map里) → data/hotfeeds/latest.json
# 页面"24h热点资讯流"和"热点主题"将从此文件+news_map合并
import urllib.request, ssl, json, gzip, datetime, os
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
BASE = "/home/agentuser/stockdb"

def get(url, hdrs=None):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Encoding": "gzip",
         "Accept-Language": "zh-CN,zh;q=0.9"}
    if hdrs: h.update(hdrs)
    req = urllib.request.Request(url, headers=h)
    b = urllib.request.urlopen(req, timeout=15, context=ctx).read()
    if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
    return b.decode("utf-8", "ignore")

def fetch_all():
    feeds = []
    # 1) 同花顺快讯
    try:
        d = json.loads(get("https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize=20"))
        for x in d.get("data", {}).get("list", []):
            feeds.append({"src": "同花顺", "title": x.get("title", ""), "digest": (x.get("digest") or "")[:60],
                          "ts": x.get("ctime", ""), "hot": None})
    except Exception as e:
        print("ths:", str(e)[:40])
    # 2) 抖音热榜
    try:
        d = json.loads(get("https://www.douyin.com/aweme/v1/web/hot/search/list/?source=6",
                           {"Referer": "https://www.douyin.com/"}))
        for w in d.get("data", {}).get("word_list", [])[:15]:
            feeds.append({"src": "抖音", "title": w.get("word", ""),
                          "hot": w.get("hot_value"), "ts": "", "digest": ""})
    except Exception as e:
        print("dy:", str(e)[:40])
    # 3) B站热门
    try:
        d = json.loads(get("https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
                           {"Referer": "https://www.bilibili.com/"}))
        for x in d.get("data", {}).get("list", [])[:15]:
            feeds.append({"src": "B站", "title": (x.get("title", "") or "").replace('<em class="keyword">','').replace("</em>",""),
                          "hot": x.get("stat", {}).get("view"), "ts": "", "digest": ""})
    except Exception as e:
        print("bili:", str(e)[:40])
    # 4) 头条热榜
    try:
        d = json.loads(get("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"))
        for x in d.get("data", [])[:15]:
            feeds.append({"src": "头条", "title": x.get("Title", ""), "hot": x.get("HotValue"),
                          "ts": "", "digest": ""})
    except Exception as e:
        print("tt:", str(e)[:40])
    return feeds

def main():
    feeds = fetch_all()
    out = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "feeds": feeds, "by_src": {}}
    for f in feeds:
        out["by_src"].setdefault(f["src"], 0)
        out["by_src"][f["src"]] += 1
    os.makedirs(f"{BASE}/data/hotfeeds", exist_ok=True)
    json.dump(out, open(f"{BASE}/data/hotfeeds/latest.json", "w"), ensure_ascii=False, indent=1)
    print("feeds:", out["by_src"])

if __name__ == "__main__":
    main()
