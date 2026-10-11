#!/usr/bin/env python3
"""
data_growth_report: 各数据仓体量报告 + 50GB 目标进度 (GitHub Actions 每周生成)
读: 各仓 .git 内对象统计 (通过 GitHub API content 大小不可行, 改用本地工作区 du 等价物:
    直接统计各仓 tracked 文件的总字节数)
输出: hub/analysis/data_growth_YYYYMMDD.json + .md
"""
import datetime, json, os, subprocess, sys
from pathlib import Path

WS = Path(os.environ.get("STOCKS_WS", "/root/workspace"))
REPOS = {
    "kline": "stocks-data-kline",
    "live": "stocks-data-live",
    "news": "stocks-data-news",
    "tools": "stocks-tools",
    "hub": "stocks",
}
GOAL_TOTAL_GB = 50.0


def repo_size_mb(path):
    """tracked 文件总大小 (近似, 不含 .git)"""
    total = 0
    for f in path.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            total += f.stat().st_size
    return round(total / 1048576, 2)


def main():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    report = {"ts": now.isoformat(), "repos": {}, "goal_gb": GOAL_TOTAL_GB}
    for key, name in REPOS.items():
        p = WS / name
        if p.exists():
            report["repos"][name] = {"size_mb": repo_size_mb(p)}
    total_mb = sum(r["size_mb"] for r in report["repos"].values())
    total_gb = round(total_mb / 1024, 3)
    report["total_mb"] = round(total_mb, 1)
    report["total_gb"] = total_gb
    report["pct_of_goal"] = round(total_gb / GOAL_TOTAL_GB * 100, 2)
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("analysis")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%d")
    jp = out_dir / f"data_growth_{ts}.json"
    jp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    md = out_dir / f"data_growth_{ts}.md"
    lines = [f"# 数据体量报告 {now.strftime('%Y-%m-%d')}", "",
             f"- 总量: **{total_gb} GB** / 目标 {GOAL_TOTAL_GB} GB ({report['pct_of_goal']}%)",
             "", "| 仓库 | 大小 (MB) |", "|------|-----------|"]
    for name, r in report["repos"].items():
        lines.append(f"| {name} | {r['size_mb']} |")
    lines += ["", "> 增长驱动: kline 分钟线每日增量 ~0.5-2GB/月 (5344 只 × m5/m60 补采), live 快照 ~200MB/月, news 归档 ~100MB/月",
              "> 当前瓶颈: 全市场日K 仅完成 244/5344 (WAF 限流); 分钟线补采未启动; 50GB 需分钟线 1 年归档 + 日K 全量"]
    md.write_text("\n".join(lines))
    print(f"[growth] total={total_gb}GB ({report['pct_of_goal']}%) → {jp}, {md}")


if __name__ == "__main__":
    main()
