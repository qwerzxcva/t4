#!/usr/bin/env python3
"""tunnel_sync.py — 把serveo隧道URL同步到 stocks-ai-view/data/tunnel.json (心跳版)
URL不变时只更新ts, 变了才换url. cron: */5
"""
import json, base64, re, urllib.request, datetime, os, pathlib

TOK = None
for line in open(os.path.expanduser("~/.git-credentials")):
    m = re.search(r"ghp_[A-Za-z0-9]+", line)
    if m: TOK = m.group(0); break
H = {"Authorization": f"token {TOK}", "Accept": "application/vnd.github+json"}
REPO = "qwerzxcva/stocks-ai-view"
LOG = "/tmp/serveo.log"
STATE = "/tmp/tunnel_state.json"

def api(method, url, payload=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None,
                                 headers=H, method=method)
    return json.load(urllib.request.urlopen(req, timeout=30))

def get_url():
    log = pathlib.Path(LOG)
    if not log.exists(): return None
    urls = re.findall(r"https://[a-z0-9]+-[0-9-]+\.serveousercontent\.com", log.read_text())
    if not urls: return None
    url = urls[-1]
    # 心跳探测: 隧道真通才算
    try:
        req = urllib.request.Request(url + "/api/pools", headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status == 200: return url
    except Exception:
        return None
    return url

def main():
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    url = get_url()
    old = {}
    if os.path.exists(STATE):
        old = json.load(open(STATE))
    prev_url = old.get("url")
    if url:
        data = {"url": url, "ts": now, "ok": True}
    else:
        data = {"url": prev_url or "", "ts": now, "ok": False}
    json.dump(data, open(STATE, "w"))
    # 只有内容变化才推(GitHub API配额)
    prev_blob = old.get("pushed_ts")
    if prev_url == data["url"] and old.get("ok") == data["ok"] and prev_blob:
        # URL与状态都没变: 每30分钟发一次心跳
        if (datetime.datetime.now() - datetime.datetime.fromisoformat(prev_blob)).total_seconds() < 1800:
            print("no change, skip"); return
    data["ts"] = now; data["pushed_ts"] = now
    json.dump(data, open(STATE, "w"))
    body = {"message": f"tunnel heartbeat {now} ok={data['ok']}",
            "content": base64.b64encode(json.dumps(data).encode()).decode()}
    try:
        sha = api("GET", f"https://api.github.com/repos/{REPO}/contents/data/tunnel.json").get("sha")
        if sha: body["sha"] = sha
    except urllib.error.HTTPError as e:
        if e.code != 404: raise
    r = api("PUT", f"https://api.github.com/repos/{REPO}/contents/data/tunnel.json", body)
    print("tunnel.json ->", r["commit"]["sha"][:8], data["url"] or "DOWN", "ok=", data["ok"])

if __name__ == "__main__":
    main()
