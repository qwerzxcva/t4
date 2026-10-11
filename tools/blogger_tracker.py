#!/usr/bin/env python3
"""
博主/用户 加仓减仓 追踪与权重系统 (blogger tracker v1)

功能:
  1. record(): 记录某博主对某标的的「加仓/减仓/止损/持有」操作 (人工录入, 暂缺自动化抓取)
  2. grade():  按时间窗 (默认 3 日) 用新浪基金净值涨跌判定该操作是否「正确」
  3. weight:   按命中率滚动加权 —— 越准的博主权重越高, 用于后续聚合预测

权重公式 (v1, 带注释):
  win_rate  = 最近 N 条已评级操作里 direction 正确的比例 (N=20, 不足 5 条不加权)
  weight    = clamp(0.5 + 1.0 * win_rate, 0.2, 5.0)
              # 0.5 为基础分 (新人起点), win_rate=1 → 1.5, 但连续命中额外指数放大:
  streak    = 最近连续正确次数; 若 streak>=3: weight *= (1.0 + 0.15*(streak-2))
              # 连胜加成: 3连 +15%, 4连 +30%... 上限截断 5.0
  评级口径:
    加仓(pos)  : 标的 NAV 在 3 个交易日内 上涨 > +0.3%  → 正确
    减仓(short): 标的 NAV 在 3 个交易日内 下跌 < -0.3%  → 正确
    止损(stop) : 视同 short
    持有(hold) : 不评级, 不计入 win_rate
    阈值 ±0.3% 过滤净值噪声 (基金 NAV 日内波动通常 <0.5%)

存储:
  stocks-data-live/data/bloggers/calls.jsonl   追加式操作日志 (一行一条)
  stocks-data-live/data/bloggers/grades.json   评级 + 权重快照 (每次 grade 覆写)

用法:
  python3 blogger_tracker.py record --blogger 韭菜研报 --asset f_512480 --side 加仓 --note "半导体反弹第二波"
  python3 blogger_tracker.py grade [--window 3] [--out ...]
  python3 blogger_tracker.py report
"""
import argparse, datetime, json, os, re, sys, time, urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
ROOT = Path(os.environ.get("BLOGGERS_DIR") or
            Path(__file__).resolve().parent.parent.parent / "stocks-data-live" / "data" / "bloggers")
CALLS = ROOT / "calls.jsonl"
GRADES = ROOT / "grades.json"
EVAL_DAYS = 3          # 评级窗口 (交易日)
THRESH = 0.003         # ±0.3% 净值噪声阈值
STREAK_BONUS = 0.15    # 连胜每多 1 次 +15%
W_MIN, W_MAX = 0.2, 5.0


def http_nav(code):
    """code: f_512480 或 512480 → 当前 NAV (float)"""
    c = code if code.startswith("f_") else "f_" + code
    url = f"https://hq.sinajs.cn/list={c}"
    body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10).read().decode("gbk", "replace")
    m = re.search(r'"([^"]+)"', body)
    if not m:
        return None
    p = m.group(1).split(",")
    return float(p[1]) if len(p) > 1 and p[1] else None


def record(blogger, asset, side, note=""):
    ts = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    nav = http_nav(asset)
    rec = {"ts": ts.isoformat(), "blogger": blogger, "asset": asset, "side": side,
           "note": note, "nav_at_call": nav}
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(CALLS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[record] {blogger} {side} {asset} nav={nav} @ {ts:%m-%d %H:%M}", flush=True)
    return rec


def _side_ok(side, nav_chg):
    """nav_chg: 自调用以来的净值变化率 (小数)。加仓/减仓是否做对。"""
    if side in ("加仓", "pos"):
        return nav_chg > THRESH
    if side in ("减仓", "止损", "short", "stop"):
        return nav_chg < -THRESH
    return None  # hold 等不评级


def grade(window_days=EVAL_DAYS):
    """逐条评级未评级的 calls, 更新 weights; 返回 grades dict"""
    ROOT.mkdir(parents=True, exist_ok=True)
    calls = []
    if CALLS.exists():
        for line in CALLS.read_text(encoding="utf-8").splitlines():
            try:
                calls.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    grades = json.loads(GRADES.read_text()) if GRADES.exists() else {"bloggers": {}, "calls": []}
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))

    for c in calls:
        key = f"{c['ts']}|{c['blogger']}|{c['asset']}|{c['side']}"
        if any(g["key"] == key for g in grades["calls"]):
            continue
        age = (now - datetime.datetime.fromisoformat(c["ts"])).total_seconds() / 86400
        if age < window_days:
            continue  # 窗口未到
        cur = http_nav(c["asset"])
        if not cur or not c.get("nav_at_call"):
            continue
        nav_chg = (cur / c["nav_at_call"]) - 1
        ok = _side_ok(c["side"], nav_chg)
        entry = {"key": key, "ok": ok, "nav_chg": round(nav_chg, 5), "graded_ts": now.isoformat()}
        grades["calls"].append(entry)
        if ok is None:
            continue
        b = grades["bloggers"].setdefault(c["blogger"], {"name": c["blogger"], "weight": 1.0,
                                                          "streak": 0, "calls": 0, "hits": 0})
        b["calls"] += 1
        if ok:
            b["hits"] += 1
            b["streak"] = b.get("streak", 0) + 1
        else:
            b["streak"] = 0
        # 权重公式 v1 (注释见文件头)
        if b["calls"] >= 5:
            wr = b["hits"] / b["calls"]
            w = 0.5 + 1.0 * wr
            if b["streak"] >= 3:
                w *= 1.0 + STREAK_BONUS * (b["streak"] - 2)
            b["weight"] = round(max(W_MIN, min(W_MAX, w)), 3)
        else:
            b["weight"] = 1.0  # 样本不足不加权
    grades["ts"] = now.isoformat()
    GRADES.write_text(json.dumps(grades, ensure_ascii=False, indent=2), encoding="utf-8")
    return grades


def report(out=None):
    g = grade()
    rows = sorted(g.get("bloggers", {}).values(), key=lambda x: x.get("weight", 0), reverse=True)
    md = ["# 博主/用户 加仓减仓 追踪报告", "",
          f"生成: {g.get('ts','')} · 评级窗口 {EVAL_DAYS} 日 · 阈值 ±{THRESH*100:.1f}% · 权重公式: 0.5+win_rate, 连胜每+1次 ×1.15, 截断 [{W_MIN},{W_MAX}]",
          "", "| 博主 | 权重 | 连胜 | 已评/命中 | 命中率 |", "|------|------|------|-----------|--------|"]
    for b in rows:
        wr = (b["hits"] / b["calls"]) if b.get("calls") else 0
        md.append(f"| {b['name']} | **{b.get('weight',1.0)}** | {b.get('streak',0)} "
                  f"| {b.get('calls',0)}/{b.get('hits',0)} | {wr:.0%} |")
    md += ["", "## 近期已评级操作", "",
           "| 时间 | 博主 | 标的 | 操作 | 净值变化 | 判定 |",
           "|------|------|------|------|---------|------|"]
    for e in g.get("calls", [])[-30:]:
        mark = "✅" if e["ok"] else ("—" if e["ok"] is None else "❌")
        parts = e["key"].split("|")
        md.append(f"| {parts[0][:16]} | {parts[1]} | {parts[2]} | {parts[3]} | {e['nav_chg']:+.2%} | {mark} |")
    text = "\n".join(md)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"[report] {out}", flush=True)
    else:
        print(text)
    return text


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("record")
    r.add_argument("--blogger", required=True)
    r.add_argument("--asset", required=True, help="f_512480 或 512480")
    r.add_argument("--side", required=True, help="加仓/减仓/止损/持有")
    r.add_argument("--note", default="")
    g = sub.add_parser("grade")
    g.add_argument("--window", type=float, default=EVAL_DAYS)
    rp = sub.add_parser("report")
    rp.add_argument("--out", default=None)
    args = p.parse_args()

    if args.cmd == "record":
        record(args.blogger, args.asset, args.side, args.note)
    elif args.cmd == "grade":
        g = grade(args.window)
        print(f"[grade] {len(g['calls'])} calls, {len(g['bloggers'])} bloggers", flush=True)
    elif args.cmd == "report":
        report(args.out)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
