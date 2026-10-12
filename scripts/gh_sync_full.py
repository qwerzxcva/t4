#!/usr/bin/env python3
"""全量数据GitHub归档: 市场快照CSV + 分钟线CSV → t2盘中仓 (每日15:40收盘后)
用 Git Data API 分片推送(单文件<=40MB)
"""
import re, json, base64, urllib.request, time, os, csv, sys

TOKEN = None
for line in open(os.path.expanduser("~/.git-credentials")):
    m = re.search(r"ghp_[A-Za-z0-9]+", line)
    if m:
        TOKEN = m.group(0)
        break
if not TOKEN:
    print("NO TOKEN")
    sys.exit(1)

REPO = "qwerzxcva/t2"
API = f"https://api.github.com/repos/{REPO}"
HDR = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"}


def req(url, data=None, method=None):
    r = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                               headers=HDR, method=method)
    return json.load(urllib.request.urlopen(r, timeout=120))


def push_file(path, local, message):
    content = open(local, "rb").read()
    blob = req(f"{API}/git/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"})
    base = req(f"{API}/git/ref/heads/main")["object"]["sha"]
    bc = req(f"{API}/git/commits/{base}")
    tree = req(f"{API}/git/trees", {"base_tree": bc["tree"]["sha"],
                "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
    c = req(f"{API}/git/commits", {"message": message, "tree": tree["sha"], "parents": [base]})
    req(f"{API}/git/refs/heads/main", {"sha": c["sha"]}, method="PATCH")
    print(f"PUSH_OK {path} ({len(content)//1024}KB) -> {c['sha'][:8]}")


def minute_to_csv(out):
    import duckdb
    con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
    today = time.strftime("%Y-%m-%d")
    con.execute(f"""COPY (SELECT symbol, date, hhmm, price, volume FROM minute
                     WHERE date='{today}') TO '{out}' (HEADER, DELIMITER ',')""")
    n = con.execute(f"SELECT COUNT(*) FROM minute WHERE date='{today}'").fetchone()[0]
    con.close()
    return n


if __name__ == "__main__":
    today = time.strftime("%Y-%m-%d")
    os.makedirs("/tmp/gh_sync", exist_ok=True)
    # ①全市场快照CSV
    snap_csv = "/home/agentuser/stockdb/data/market_snapshot/{}_full.csv".format(today)
    if os.path.exists(snap_csv):
        push_file(f"snapshot/{today}_full.csv", snap_csv, f"snapshot {today}")
    # ②分钟线CSV(全量)
    mcsv = f"/tmp/gh_sync/minute_{today}.csv"
    n = minute_to_csv(mcsv)
    if n > 0:
        push_file(f"minute/{today}.csv", mcsv, f"minute {today} ({n} rows)")
    else:
        print("minute empty, skip")
