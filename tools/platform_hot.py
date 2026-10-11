"""
平台热榜采集器 (网页版免登录公开端点)
- 抖音热榜:   douyin.com aweme web hot board (纯 HTTP)
- 小红书首页: xiaohongshu.com/explore 内嵌 JSON (纯 HTTP)
- 微博热搜:   s.weibo.com/top/summary (需过 Sina Visitor System JS 挑战 → 本脚本自动带浏览器特征重试, 失败降级跳过)
输出: 追加到 news_collect 兼容的 消息面/news 目录; 也作为 ai_screener 的补充新闻源
用法: python3 platform_hot.py --out /tmp/platform_news.json
"""
import argparse, datetime, json, re, sys, time, urllib.request

UA_PC = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def douyin_hot():
    url = ("https://www.douyin.com/aweme/v1/web/hot/search/list/"
           "?device_platform=webapp&aid=6383&channel=channel_pc_web"
           "&source=9&hot_board_content=hot_board_default&hot_board_duration=7200")
    body = urllib.request.urlopen(urllib.request.Request(url, headers={**UA_PC, "Referer": "https://www.douyin.com/hot"}), timeout=15).read()
    d = json.loads(body.decode("utf-8", "replace"))
    out = []
    for w in (d.get("data") or {}).get("word_list") or []:
        word = str(w.get("word", ""))
        out.append({"ts": "", "title": f"[抖音热榜] {word}", "theme": [],
                    "sentiment": "neu", "src": "douyin-hot", "hot_value": w.get("hot_value"),
                    "note_count": w.get("note_count")})
    return out


def xhs_explore():
    """小红书 explore 页内嵌 JSON (window.__INITIAL_STATE__) → 笔记标题列表"""
    url = "https://www.xiaohongshu.com/explore"
    body = urllib.request.urlopen(urllib.request.Request(url, headers={**UA_PC, "Referer": "https://www.xiaohongshu.com/"}),
                                   timeout=15).read().decode("utf-8", "replace")
    m = re.search(r"window\.__INITIAL_STATE__=(.*?)</script>", body, re.S)
    out = []
    if m:
        raw = m.group(1).replace("undefined", "null")
        try:
            state = json.loads(raw)
            notes = []
            def walk(o):
                if isinstance(o, dict):
                    t = o.get("title") or o.get("titleText")
                    if t and o.get("type") in ("note", "normal", None):
                        notes.append(t)
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(state)
            for t in notes[:30]:
                out.append({"ts": "", "title": f"[小红书] {t[:80]}", "theme": [],
                            "sentiment": "neu", "src": "xhs-explore"})
        except json.JSONDecodeError:
            pass
    return out


def weibo_hot():
    """微博热搜: s.weibo.com 需过 visitor 挑战; 纯 HTTP 失败时静默降级 (浏览器自动化场景可用)"""
    url = "https://s.weibo.com/top/summary?cate=fin"
    try:
        body = urllib.request.urlopen(urllib.request.Request(url, headers={**UA_PC, "Referer": "https://s.weibo.com/"}),
                                      timeout=15).read().decode("utf-8", "replace")
        if "Sina Visitor System" in body:
            print("[weibo] visitor challenge, skip (needs browser)", file=sys.stderr)
            return []
        out = []
        for m in re.finditer(r'<td class="td-02">(.*?)</td>\s*<td class="td-01">(.*?)</td>', body, re.S):
            rank, word = re.sub(r"<[^>]+>", "", m.group(1)).strip(), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            out.append({"ts": "", "title": f"[微博热搜#{rank}] {word}", "theme": [],
                        "sentiment": "neu", "src": "weibo-hot", "hot_value": None})
        return out
    except Exception as e:
        print(f"[weibo] err: {e}", file=sys.stderr)
        return []


def xueqiu_hot():
    """雪球热门讨论: 阿里云 WAF JS 挑战; 纯 HTTP 带 PC UA 重试, 失败静默降级"""
    for base in ("https://xueqiu.com", "https://stock.xueqiu.com"):
        try:
            url = f"{base}/v5/statuses/hot/listV2.json?count=10&since_id=-1&max_id=-1" if "stock" in base else \
                  f"{base}/statuses/hot/listV2.json?count=10"
            body = urllib.request.urlopen(urllib.request.Request(url, headers={**UA_PC, "Referer": "https://xueqiu.com/"}),
                                          timeout=12).read().decode("utf-8", "replace")
            d = json.loads(body)
            out = []
            for it in (d.get("list") or d.get("statuses") or [])[:10]:
                st = it.get("original_status") or it
                text = re.sub(r"<[^>]+>", "", str(st.get("text", ""))).strip()
                if text:
                    out.append({"ts": "", "title": f"[雪球热议] {text[:80]}", "theme": [],
                                 "sentiment": "neu", "src": "xueqiu-hot"})
            if out:
                return out
        except Exception as e:
            print(f"[xueqiu] {base} err: {e}", file=sys.stderr)
    return []


def qq_news_author(author_id="5233565"):
    """腾讯新闻作者文章流: i.news.qq.com 公开接口; 失败静默降级"""
    out = []
    for ep in (f"https://i.news.qq.com/web/omn/author/articles?author_id={author_id}&page=1&size=20",
               f"https://i.news.qq.com/web_backend/getAuthorArticleList?authorId={author_id}&page=1&size=20"):
        try:
            body = urllib.request.urlopen(urllib.request.Request(ep, headers={**UA_PC, "Referer": "https://news.qq.com/"}),
                                          timeout=12).read().decode("utf-8", "replace")
            d = json.loads(body)
            items = (d.get("data") or {}).get("list") or d.get("list") or (d.get("data") or {}).get("articleList") or []
            for it in items[:20]:
                t = str(it.get("title", "")).strip()
                if t:
                    out.append({"ts": str(it.get("publish_time") or it.get("time", ""))[:19],
                                 "title": f"[腾讯新闻·作者{author_id}] {t[:80]}", "theme": [],
                                 "sentiment": "neu", "src": "qq-news-author"})
            if out:
                break
        except Exception as e:
            print(f"[qq-news] {ep.split('?')[0].split('/')[-1]} err: {e}", file=sys.stderr)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    items = []
    items += douyin_hot()
    time.sleep(0.5)
    items += xhs_explore()
    time.sleep(0.5)
    items += weibo_hot()
    time.sleep(0.5)
    items += xueqiu_hot()
    time.sleep(0.5)
    items += qq_news_author()
    report = {"ts": now.isoformat(), "date": now.strftime("%Y-%m-%d"),
              "items": items, "meta": {"sources": ["douyin-hot", "xhs-explore", "weibo-hot", "xueqiu-hot", "qq-news-author"],
                                        "zero_cred": True}}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"written {args.out} ({len(items)} items)", flush=True)
    else:
        print(text)


if __name__ == "__main__":
    main()
