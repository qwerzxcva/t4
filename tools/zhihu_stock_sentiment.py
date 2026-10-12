#!/usr/bin/env python3
"""
知乎股票情绪数据采集脚本（无需登录，仅抓取公开内容）

用法:
  python3 zhihu_stock_sentiment.py 600519,000858,300750 [输出目录]

输出:
  stocks-data-live/data/research/zhihu_{date}.json
  包含每只股票近 24h 的正面/负面/中性回答数及比例。

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
RATE_LIMIT = 0.5          # 每只股票请求间隔（秒）
ZHIHU_SEARCH_API = "https://api.zhihu.com/search"
ZHIHU_SEARCH_WEB = "https://www.zhihu.com/search"
DEFAULT_OUTPUT_DIR = "stocks-data-live/data/research"

# 股票代码 → 名称（用于更自然的搜索关键词）
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

# 关键词情绪判定（简单词频统计）
POSITIVE_KEYWORDS = [
    "看好", "上涨", "买入", "推荐", "牛市", "利好", "加仓",
    "持有", "突破", "新高", "强势", "反弹", "机会",
    "值得", "优质", "核心", "龙头", "盈利", "增长",
    "乐观", "潜力", "布局", "信心",
]
NEGATIVE_KEYWORDS = [
    "看空", "下跌", "卖出", "割肉", "熊市", "利空", "减仓",
    "亏损", "崩盘", "跌停", "弱势", "套牢", "陷阱",
    "高估", "泡沫", "危险", "风险", "谨慎", "悲观",
    "回撤", "止损", "出货",
]

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


def fetch_zhihu_search(query: str) -> list[dict]:
    """
    尝试知乎搜索 API，返回答案列表。
    每条: {answer_id, content, created_time}
    """
    params = urllib.parse.urlencode({"q": query, "type": "answer"})
    url = f"{ZHIHU_SEARCH_API}?{params}"
    body = http_get(url)
    if not body:
        return []
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    results = []
    for item in data.get("data", []):
        if item.get("type") != "answer":
            continue
        results.append({
            "answer_id": str(item.get("id", "")),
            "content": item.get("content") or "",
            "created_time": item.get("created_time", 0),
        })
    return results


def fetch_zhihu_web(query: str) -> list[dict]:
    """
    备用方案：从知乎网页搜索结果页提取摘要文本（无 API 权限时降级使用）。
    可靠性低于 API，仅作为兜底。
    """
    url = f"{ZHIHU_SEARCH_WEB}?type=content&q={urllib.parse.quote(query)}"
    body = http_get(url)
    if not body:
        return []
    html = body.decode("utf-8", errors="replace")
    results: list[dict] = []
    # 匹配知乎搜索结果中的 RichText 片段
    for m in re.finditer(
        r'<div[^>]*class="[^"]*RichText[^"]*"[^>]*>(.*?)</div>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if len(text) > 10:
            results.append({
                "answer_id": "",
                "content": text,
                "created_time": 0,
            })
    return results[:20]


# ---------------------------------------------------------------------------
# 情绪分析
# ---------------------------------------------------------------------------

def classify_sentiment(text: str) -> str:
    """基于关键词命中数判定单条文本的情绪倾向。返回 positive / negative / neutral。"""
    pos_hits = sum(1 for kw in POSITIVE_KEYWORDS if kw in text)
    neg_hits = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text)
    if pos_hits > neg_hits:
        return "positive"
    if neg_hits > pos_hits:
        return "negative"
    return "neutral"


# ---------------------------------------------------------------------------
# 单只股票采集
# ---------------------------------------------------------------------------

def collect_stock_sentiment(stock_code: str) -> dict:
    """
    采集并分析指定股票的知乎情绪数据，返回结构化结果 dict。
    任何异常由调用方 try/except 捕获，本函数内部不捕获（保持干净）。
    """
    stock_name = STOCK_NAMES.get(stock_code, stock_code)
    # 搜索关键词："{代码} 涨" 比纯代码更容易命中讨论帖
    query = f"{stock_code} 涨"

    answers = fetch_zhihu_search(query)
    if not answers:
        answers = fetch_zhihu_web(query)

    # 按 24h 窗口过滤（created_time==0 视为无时间戳，保留）
    cutoff = datetime.now().timestamp() - 86400
    recent = [a for a in answers if a["created_time"] == 0 or a["created_time"] >= cutoff]

    positive = negative = neutral = 0
    sample_pos: list[str] = []
    sample_neg: list[str] = []
    for ans in recent:
        cls = classify_sentiment(ans["content"])
        if cls == "positive":
            positive += 1
            if len(sample_pos) < 3:
                sample_pos.append(ans["content"][:200])
        elif cls == "negative":
            negative += 1
            if len(sample_neg) < 3:
                sample_neg.append(ans["content"][:200])
        else:
            neutral += 1

    total = len(recent)
    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "query": query,
        "total_answers": total,
        "positive_count": positive,
        "negative_count": negative,
        "neutral_count": neutral,
        "positive_ratio": round(positive / total, 4) if total else 0.0,
        "negative_ratio": round(negative / total, 4) if total else 0.0,
        "sample_positive": sample_pos,
        "sample_negative": sample_neg,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ok" if total else "empty",
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python3 zhihu_stock_sentiment.py <股票代码,逗号分隔> [输出目录]")
        print("示例: python3 zhihu_stock_sentiment.py 600519,000858,300750")
        sys.exit(1)

    codes = [c.strip() for c in sys.argv[1].split(",") if c.strip()]
    output_dir = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(DEFAULT_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    stocks_result: dict[str, dict] = {}
    for i, code in enumerate(codes):
        try:
            stocks_result[code] = collect_stock_sentiment(code)
        except Exception as exc:
            stocks_result[code] = {
                "stock_code": code,
                "stock_name": STOCK_NAMES.get(code, code),
                "status": "failed",
                "error": str(exc),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
        # 限流：最后一只不 sleep
        if i < len(codes) - 1:
            time.sleep(RATE_LIMIT)

    output = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source": "zhihu",
        "window_hours": 24,
        "stocks": stocks_result,
    }

    out_file = output_dir / f"zhihu_{datetime.now():%Y-%m-%d}.json"
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)

    print(f"[zhihu] 结果已写入 {out_file}")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
