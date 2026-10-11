#!/usr/bin/env python3
"""
tracker: 跟踪知名基金/ETF 日净值序列，输出「今日加减仓信号」(净值涨跌 + 相对同期指表现)
数据源: 新浪 hq.sinajs.cn (基金代码前缀 f_) —— 免费、无凭据、可复现
覆盖范围:
  - 头部宽基: 510300(沪深300ETF)/510050(SSE50ETF)
  - 热门行业: 512480(半导体)/512760(芯片)/512170(Health)/512880(券商)/513050(恒生科技)
  - 知名混合/定投: 110011(易方达优质精选)/501015(财通多策略 LOF)/161725(招商白酒)
未来可扩展:
  - 微博/抖音财经博主的「今日加仓 X 只」贴文: 需要登录态 cookie + 反爬绕过
  - 蚂蚁财富/京东金融/理财通: 需登录态或私有 API (目前不可直接抓)
  - 小红书基金经理日记: 需 app 抓包
  → 建议在容器内跑浏览器自动化 (playwright/selenium + 注入用户 cookie) 后再对接
用法: python3 tracker.py [--out /path/latest.json] [--top 10]
"""
import argparse, datetime, json, os, re, sys, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Referer": "https://finance.sina.com.cn/"}
# 跟踪池: code_in_sina=f_XXXXXX -> (name, group, tag)
WATCHLIST = {
    # 宽基 & 指数
    "f_510300": ("沪深300ETF", "宽基", "index"),
    "f_510050": ("上证50ETF", "宽基", "index"),
    "f_513050": ("恒生科技ETF", "宽基", "hk"),
    # 行业 ETF
    "f_512480": ("半导体ETF", "行业", "semi"),
    "f_512760": ("芯片ETF", "行业", "semi"),
    "f_512170": ("医疗健康ETF", "行业", "health"),
    "f_512880": ("券商ETF", "行业", "broker"),
    "f_512660": ("军工ETF", "行业", "military"),
    "f_512800": ("银行ETF", "行业", "bank"),
    "f_512690": ("酒ETF", "行业", "liquor"),
    "f_515790": ("光伏ETF", "行业", "solar"),
    "f_515030": ("新能源车ETF", "行业", "nev"),
    # 头部主动基金 (QDII/LOF/混合)
    "f_110011": ("易方达优质精选", "主动", "fund"),
    "f_501015": ("财通多策略升级 LOF", "主动", "fund"),
    "f_161725": ("招商中证白酒", "主动", "fund"),
}


def fetch():
    codes = list(WATCHLIST.keys())
    chunk = 40
    out = {}
    for i in range(0, len(codes), chunk):
        batch = codes[i:i+chunk]
        url = "https://hq.sinajs.cn/list=" + ",".join(batch)
        try:
            body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10).read().decode("gbk", "replace")
            for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', body):
                code, payload = m.group(1), m.group(2)
                p = payload.split(",")
                if len(p) < 5:
                    continue
                try:
                    # 新浪基金字段: [0]名 [1]净值/现价 [3]昨收 [4]日期
                    nav = float(p[1] or 0)
                    prev = float(p[3] or 0)
                    chg = round((nav / prev - 1) * 100, 3) if prev else 0
                    date = p[4] if len(p) > 4 else ""
                    out[code] = {"code": code, "nav": nav, "prev": round(prev, 4), "chg": chg, "date": date}
                except (ValueError, IndexError):
                    pass
            time.sleep(0.1)
        except Exception as e:
            print(f"[tracker {i}] err: {e}", file=sys.stderr)
    return out


def build_report(raw, meta_file):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    ts = now.strftime("%Y%m%d_%H%M")
    # 加载历史
    hist = []
    if os.path.exists(meta_file):
        try:
            hist = json.load(open(meta_file, encoding="utf-8"))
        except Exception:
            hist = []
    # 逐标的: 计算相对同期指涨跌 (相对强度) + 历史趋势
    records = []
    for code, info in raw.items():
        name, group, tag = WATCHLIST[code]
        # 相对强度: 与沪深300 (f_510300) 同日涨跌幅的差值
        bench = raw.get("f_510300", {}).get("chg", 0) or 0
        rel = round(info["chg"] - bench, 3)
        # 相对 5 日/20 日动量 (简单用历史序列近似)
        hist_for = [r for r in hist if r.get("code") == code]
        mov_5d = round(sum(r["chg"] for r in hist_for[-5:]) / max(1, min(5, len(hist_for))), 3)
        mov_20d = round(sum(r["chg"] for r in hist_for[-20:]) / max(1, min(20, len(hist_for))), 3)
        records.append({
            **info, "name": name, "group": group, "tag": tag,
            "rel_to_hs300": rel, "mov_5d": mov_5d, "mov_20d": mov_20d,
            "hist_count": len(hist_for),
        })
    records.sort(key=lambda x: abs(x["rel_to_hs300"]), reverse=True)
    report = {
        "ts": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "records": records,
        "bench": {"code": "f_510300", "name": "沪深300ETF", "nav": raw.get("f_510300", {}).get("nav"),
                  "chg": raw.get("f_510300", {}).get("chg"), "date": raw.get("f_510300", {}).get("date")},
        "meta": {"source": "sina-fund-hq", "algo": "tracker-v1", "zero_cred": True,
                 "expansion": "微博/抖音/小红书/蚂蚁财富/京东金融/理财通需要登录态 cookie, 见代码注释"},
    }
    # 保留历史窗口 (每标的最多 30 条)
    # 逐标的全量记录: 存 (代码, ts, nav, chg, rel) 供后续多日动量回看
    for r in records:
        hist.append({"code": r["code"], "ts": now.isoformat(), "nav": r["nav"],
                     "chg": r["chg"], "rel": r["rel_to_hs300"]})
    hist = hist[-200:]
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--meta-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "stocks-data-live", "data", "tracker"))
    args = parser.parse_args()
    meta_file = os.path.join(args.meta_dir, "tracker_hist.json")
    os.makedirs(args.meta_dir, exist_ok=True)
    print(f"[tracker] fetching {len(WATCHLIST)} instruments", flush=True)
    raw = fetch()
    print(f"[tracker] fetched {len(raw)} items", flush=True)
    report = build_report(raw, meta_file)
    top = report["records"][:args.top]
    print(f"[tracker] bench={report['bench']['chg']:+.3f}% | top up: {top[0]['name']} {top[0]['rel_to_hs300']:+.3f}%", flush=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[output] {args.out} ({os.path.getsize(args.out)} bytes)", flush=True)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
