#!/usr/bin/env python3
"""dual_push.py — 双账号 GitHub 提交引擎 (纯cron, 不依赖AI)
路由: 数据类型 -> 目标仓库. token池轮换, 403/rate-limit 自动切换.
用法: dual_push.py <kind> [file]  kind: pages|minute|news|social|fund|global|moneyflow
"""
import json, base64, re, os, sys, urllib.request, urllib.error, datetime, pathlib

HOME = os.path.expanduser("~")
TOKENS = []  # (label, token)
for line in open(f"{HOME}/.git-credentials"):
    m = re.search(r"ghp_[A-Za-z0-9]+", line)
    if m: TOKENS.append(("qwerzxcva", m.group(0)))
np = f"{HOME}/.hermes/dufuwuxin.token"
if os.path.exists(np): TOKENS.append(("dufuwuxin", open(np).read().strip()))

SDB = f"{HOME}/stockdb"
# kind -> [(local_path, repo, remote_path, private)]
ROUTES = {
    "pages":     [(f"{SDB}/data/pages_latest.json", "qwerzxcva/stocks-ai-view", "data/latest.json", False),
                  (f"{SDB}/data/pages_latest.json", "dufuwuxin/d4-pages-mirror", "data/latest.json", False)],
    "minute":    [("{SDB}/data/market_snapshot/{DATE}_full.csv", "dufuwuxin/d1-minute", "minute/{YYYYMMDD}.csv", True),
                  ("{SDB}/data/market_snapshot/{DATE}_full.csv", "qwerzxcva/t2", "minute/{YYYYMMDD}.csv", True)],
    "news":      [("{SDB}/data/news_map/{DATE}.json", "dufuwuxin/d2-social", "news/latest.json", True),
                  ("{SDB}/data/news_map/{DATE}.json", "qwerzxcva/t3", "news/latest.json", True)],
    "social":    [(f"{SDB}/data/social/latest.json", "dufuwuxin/d2-social", "social/latest.json", True),
                  (f"{SDB}/data/social/latest.json", "qwerzxcva/t7", "social/latest.json", True)],
    "fund":      [(f"{SDB}/data/fund/latest.json", "dufuwuxin/d3-global", "fund/latest.json", True),
                  (f"{SDB}/data/fund/latest.json", "qwerzxcva/t7", "fund/latest.json", True)],
    "moneyflow": [(f"{SDB}/data/moneyflow/latest.json", "dufuwuxin/d3-global", "moneyflow/latest.json", True),
                  (f"{SDB}/data/moneyflow/latest.json", "qwerzxcva/t7", "moneyflow/latest.json", True)],
    "global":    [(f"{SDB}/data/global/latest.json", "dufuwuxin/d3-global", "global/latest.json", True),
                  (f"{SDB}/data/global/latest.json", "qwerzxcva/t7", "global/latest.json", True)],
    "hotfeeds":  [(f"{SDB}/data/hotfeeds/latest.json", "dufuwuxin/d2-social", "hotfeeds/latest.json", True),
                  (f"{SDB}/data/hotfeeds/latest.json", "qwerzxcva/t7", "hotfeeds/latest.json", True)],
}

def api(tok, method, url, payload=None, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None,
        headers={"Authorization": f"token {tok}", "Accept": "application/vnd.github+json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_msg": e.read().decode()[:150]}

def push_file(repo, remote_path, content_bytes, label="dual_push"):
    """token池轮换推送, 返回 (ok, sha, used)"""
    today = datetime.date.today().strftime("%Y%m%d")
    remote_path = remote_path.replace("{YYYYMMDD}", today)
    last_err = None
    for who, tok in TOKENS:
        get = api(tok, "GET", f"https://api.github.com/repos/{repo}/contents/{remote_path}")
        body = {"message": f"[{label}] {who} {datetime.datetime.now().strftime('%H:%M:%S')}",
                "content": base64.b64encode(content_bytes).decode()}
        if "sha" in get: body["sha"] = get["sha"]
        if get.get("_err") == 404 and not get.get("sha"):
            pass  # 新文件
        r = api(tok, "PUT", f"https://api.github.com/repos/{repo}/contents/{remote_path}", body, timeout=60)
        if "_err" not in r:
            return True, r["commit"]["sha"][:8], who
        last_err = (who, r.get("_err"), r.get("_msg", "")[:80])
        print(f"  [{who}] FAIL {r.get('_err')}: {r.get('_msg','')[:80]}", file=sys.stderr)
        if r.get("_err") == 403:  # 限速 -> 下一个token
            continue
        if r.get("_err") == 409:  # sha冲突 -> 刷新一次
            get = api(tok, "GET", f"https://api.github.com/repos/{repo}/contents/{remote_path}")
            if "sha" in get:
                body["sha"] = get["sha"]
                r = api(tok, "PUT", f"https://api.github.com/repos/{repo}/contents/{remote_path}", body, timeout=60)
                if "_err" not in r:
                    return True, r["commit"]["sha"][:8], who
    print(f"PUSH FAIL {repo}/{remote_path}: {last_err}", file=sys.stderr)
    return False, None, None

def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "pages"
    routes = ROUTES.get(kind)
    if not routes: print("unknown kind", kind); sys.exit(1)
    now = datetime.datetime.now()
    for local, repo, remote, priv in routes:
        today = datetime.date.today().strftime("%Y%m%d")
        p = pathlib.Path(local.replace("{SDB}", SDB).replace("{YYYYMMDD}", today).replace("{DATE}", datetime.date.today().isoformat()))
        if not p.exists():
            print(f"skip {kind}: {local} missing", file=sys.stderr); continue
        # 30秒内内容没变就跳过(省配额)
        state = f"/tmp/dualpush_{kind}_{repo.replace('/','_')}.json"
        sig = f"{p.stat().st_size}_{int(p.stat().st_mtime)}"  # noqa
        old = json.load(open(state))["sig"] if os.path.exists(state) else None
        if old == sig and now.minute % 30 != 0:  # 每30分钟强制推一次心跳
            continue
        ok, sha, who = push_file(repo, remote, p.read_bytes(), kind)
        json.dump({"sig": sig, "sha": sha, "who": who, "ts": now.isoformat()}, open(state, "w"))
        print(f"{kind} -> {repo}/{remote} [{'OK ' + who if ok else 'FAIL'}] {sha}")

if __name__ == "__main__":
    main()
