#!/usr/bin/env python3
"""
百度贴吧股票吧数据采集脚本（无需登录，仅抓取公开内容）

吧名规律: "{股票代码}吧"，如 600519吧、000858吧
用法:
  python3 tieba_stock_sentiment.py 600519,000858,300750 [输出目录]

输出:
  stocks-data-live/data/research/tieba_{date}.json
  包含每个吧近 24h 新帖数和热门帖标题。

零依赖，仅使用 Python 标准库。
"""
import json
import sys
import time
import re
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量配置
# ---------------------------------------------------------------------------
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
RATE_LIMIT = 0.5          # 每个吧请求间隔（秒）
TIEBA_API = "https://tieba.baidu.com/mo/q/m/c/threadlist"
TIEBA_WEB = "https://tieba.baidu.com/f"
DEFAULT_OUTPUT_DIR = "stocks-data-live/data/research"

# 股票代码 → 名称（用于备注和备选吧名）
STOCK_NAMES: dict[str, str] = {
    "600519": "贵州茅台",
    "000858": "五粮液",
    "300750": "宁德时代",
    "600036": "招商银行",
    "000001": "平安银行",
    "601318": "中国平安",
    "002594": "比亚迪",
    "600030": "中信证券",
    "000333": "美的集团",
    "601888": "中国中免",
    "000002": "万科A",
    "600000": "浦发银行",
}

# ---------------------------------------------------------------------------
# HTTP 工具函数
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int = 10) -> bytes | None:
    """发起 GET 请求，带浏览器 UA。失败返回 None，不抛异常。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            if attempt < 1:
                time.sleep(0.3)
    return None


def fetch_tieba_api(bar_name: str) -> list[dict]:
    """
    尝试贴吧 mobile JSON API，返回帖子列表。
    每条: {tid, title, reply_count, view_count, create_time, author}
    """
    params = urllib.parse.urlencode({
        "type": "thread_list",
        "kw": bar_name,
        "pn": 0,
    })
    url = f"{TIEBA_API}?{params}"
    body = http_get(url)
    if not body:
        return []
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []

    results: list[dict] = []
    for item in data.get("thread_list", []):
        author_info = item.get("author") or {}
        results.append({
            "tid": str(item.get("id", "")),
            "title": item.get("title") or "",
            "reply_count": int(item.get("reply_num", 0) or 0),
            "view_count": int(item.get("view_num", 0) or 0),
            "create_time": item.get("create_time", 0),
            "author": author_info.get("name", "") if isinstance(author_info, dict) else "",
        })
    return results


def fetch_tieba_web(bar_name: str) -> list[dict]:
    """
    备用方案：从贴吧网页版提取帖子标题（API 不可用时降级使用）。
    匹配 class 含 j_th_tit 的链接，提取标题文本。
    """
    url = f"{TIEBA_WEB}/f?kw={urllib.parse.quote(bar_name)}&ie=utf-8"
    body = http_get(url)
    if not body:
        return []
    html = body.decode("utf-8", errors="replace")
    results: list[dict] = []
    for m in re.finditer(
        r'<a[^>]*class="[^"]*j_th_tit[^"]*"[^>]*>(.*?)</a>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if title:
            results.append({
                "tid": "",
                "title": title,
                "reply_count": 0,
                "view_count": 0,
                "create_time": 0,
                "author": "",
            })
    return results[:30]


# ---------------------------------------------------------------------------
# 单个吧采集
# ---------------------------------------------------------------------------

def collect_bar_sentiment(stock_code: str) -> dict:
    """
    采集指定股票对应的贴吧数据，返回结构化结果 dict。
    任何异常由调用方 try/except 捕获。
    """
    stock_name = STOCK_NAMES.get(stock_code, stock_code)
    bar_name = f"{stock_code}吧"

    threads = fetch_tieba_api(bar_name)
    if not threads:
        threads = fetch_tieba_web(bar_name)

    # 按 24h 窗口过滤（create_time==0 表示无时间戳，保留）
    cutoff = datetime.now().timestamp() - 86400
    recent = [t for t in threads if t["create_time"] == 0 or t["create_time"] >= cutoff]

    # 按回复数降序取 top 10 作为热门帖
    hot_threads = sorted(recent, key=lambda t: t["reply_count"], reverse=True)[:10]

    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "bar_name": bar_name,
        "total_threads_24h": len(recent),
        "hot_threads": [
            {
                "tid": t["tid"],
                "title": t["title"],
                "reply_count": t["reply_count"],
                "view_count": t["view_count"],
                "author": t["author"],
            }
            for t in hot_threads
        ],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ok" if recent else "empty",
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python3 tieba_stock_sentiment.py <股票代码,逗号分隔> [输出目录]")
        print("示例: python3 tieba_stock_sentiment.py 600519,000858,300750")
        sys.exit(1)

    codes = [c.strip() for c in sys.argv[1].split(",") if c.strip()]
    output_dir = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(DEFAULT_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    bars_result: dict[str, dict] = {}
    for i, code in enumerate(codes):
        try:
            bars_result[code] = collect_bar_sentiment(code)
        except Exception as exc:
            bars_result[code] = {
                "stock_code": code,
                "stock_name": STOCK_NAMES.get(code, code),
                "bar_name": f"{code}吧",
                "status": "failed",
                "error": str(exc),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
        # 限流：最后一个吧不 sleep
        if i < len(codes) - 1:
            time.sleep(RATE_LIMIT)

    output = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": "tieba",
        "window_hours": 24,
        "bars": bars_result,
    }

    out_file = output_dir / f"tieba_{datetime.now():%Y-%m-%d}.json"
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)

    print(f"[tieba] 结果已写入 {out_file}")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
