#!/usr/bin/env python3
"""
腾讯 Level 2 数据接口 (l2)
需 uin + skey cookie 才能获取五档/逐笔/分价数据

配置: 在 data/l2_config.json 里写:
  {"uin": "你的QQ号", "skey": "你的skey"}

用法:
  python3 fetch_l2.py --help
  python3 fetch_l2.py --codes sz000001,sh600519 --fields depth,bid_ask
  python3 fetch_l2.py --daily  # 拉今日全部成交明细（慢，约5分钟/只）
"""
import os, sys, json, time, urllib.request, urllib.parse, re
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "..", "..", "stocks-data-live", "data")
CONFIG_FILE = os.path.join(DATA_DIR, "l2_config.json")

UA_BASE = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
           "Referer": "https://gu.qq.com/"}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            return json.load(open(CONFIG_FILE))
        except Exception:
            pass
    return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[l2] 配置已保存 -> {CONFIG_FILE}")


def get_cookies(config):
    """构造腾讯自选股 Level 2 所需 cookie."""
    uin = config.get("uin", "").strip()
    skey = config.get("skey", "").strip()
    if not uin or not skey:
        return {}, None
    cookie_str = f"uin=o{uin}; skey={skey}"
    hdrs = dict(UA_BASE)
    hdrs["Cookie"] = cookie_str
    return hdrs, cookie_str


def http(url, headers=None, timeout=10):
    h = headers or UA_BASE
    for i in range(3):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout).read()
        except Exception as e:
            if i < 2: time.sleep(0.3)
    return b""


# =====================================================================
# Level 2 接口
# =====================================================================

def fetch_depth(codes, batch=20):
    """五档盘口 (qt.gtimg.cn L2)"""
    out = {}
    for i in range(0, len(codes), batch):
        batch_codes = codes[i:i+batch]
        # 格式: q=s_sz000001,s_sh600519
        url = f"https://qt.gtimg.cn/q={','.join(batch_codes)}"
        body = http(url)
        text = body.decode("gbk", "replace")
        for m in re.finditer(r'v_(\w+)="([^"]*)"', text):
            code = m.group(1)
            payload = m.group(2).split("~")
            if len(payload) < 50:
                continue  # 免费接口，字段不够
            try:
                out[code] = {
                    "code": code,
                    "price": float(payload[3]),
                    "prev_close": float(payload[4]),
                    "open": float(payload[5]),
                    "high": float(payload[33]) if len(payload) > 33 else 0,
                    "low": float(payload[34]) if len(payload) > 34 else 0,
                    "bid1_price": float(payload[9]) if len(payload) > 9 else 0,
                    "bid1_vol": int(float(payload[10])) if len(payload) > 10 else 0,
                    "bid2_price": float(payload[11]) if len(payload) > 11 else 0,
                    "bid2_vol": int(float(payload[12])) if len(payload) > 12 else 0,
                    "bid3_price": float(payload[13]) if len(payload) > 13 else 0,
                    "bid3_vol": int(float(payload[14])) if len(payload) > 14 else 0,
                    "ask1_price": float(payload[19]) if len(payload) > 19 else 0,
                    "ask1_vol": int(float(payload[20])) if len(payload) > 20 else 0,
                    "ask2_price": float(payload[21]) if len(payload) > 21 else 0,
                    "ask2_vol": int(float(payload[22])) if len(payload) > 22 else 0,
                    "ask3_price": float(payload[23]) if len(payload) > 23 else 0,
                    "ask3_vol": int(float(payload[24])) if len(payload) > 24 else 0,
                    "time": payload[30][:14] if len(payload) > 30 else "",
                }
            except (ValueError, IndexError):
                continue
        time.sleep(0.1)
    return out


def fetch_tick(codes, date_str=None):
    """逐笔成交 (qt.gtimg.cn t_ 前缀, 需 L2 权限)"""
    if not date_str:
        date_str = datetime.now(CST).strftime("%Y%m%d")
    out = {}
    for code in codes:
        # 格式: t_sz000001
        raw_code = code.replace("sh", "sh").replace("sz", "sz")
        url = f"https://qt.gtimg.cn/q=t_{raw_code}"
        headers, _ = get_cookies(load_config())
        body = http(url, headers=headers)
        text = body.decode("gbk", "replace")
        if 'none_match' in text:
            print(f"  [L2警告] {code} 需登录态，跳过", file=sys.stderr)
            continue
        out[code] = text
        time.sleep(0.05)
    return out


def check_auth():
    """检查当前 cookie 是否有 L2 权限."""
    cfg = load_config()
    if not cfg.get("uin"):
        return False, "未配置 UIN，请提供腾讯账号 UIN"
    headers, cookie_str = get_cookies(cfg)
    body = http("https://qt.gtimg.cn/q=s_sz000001", headers=headers)
    text = body.decode("gbk", "replace")
    has_l2 = len(text.split("~")) > 50 if '"' in text else False
    return has_l2, ("已认证 L2" if has_l2 else "未认证 L2，请检查 cookie 是否过期")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="腾讯 Level 2 行情")
    ap.add_argument("--codes", default="", help="股票代码列表，逗号分隔 (sz000001,sh600519)")
    ap.add_argument("--field", choices=["depth", "tick", "status"], default="depth")
    ap.add_argument("--save", action="store_true", help="保存到 data/l2_{date}_*.json")
    ap.add_argument("--verify", action="store_true", help="只检查认证状态")
    args = ap.parse_args()

    if args.verify:
        ok, msg = check_auth()
        print(f"Level 2 认证: {'✅' if ok else '❌'} {msg}")
        sys.exit(0 if ok else 1)

    if not args.codes:
        print("用法: python3 fetch_l2.py --codes sz000001,sh600519 [--field depth|tick] [--save]", file=sys.stderr)
        sys.exit(1)

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    date_str = datetime.now(CST).strftime("%Y-%m-%d")
    print(f"[l2] {args.field} | {len(codes)} codes | {date_str}", flush=True)

    if args.field == "depth":
        data = fetch_depth(codes)
        print(f"  返回: {len(data)} 只股票")
        if data:
            sample = list(data.values())[0]
            print(f"  示例 {sample['code']}: price={sample['price']} bid1={sample['bid1_price']} ask1={sample['ask1_price']}")
    elif args.field == "tick":
        data = fetch_tick(codes, date_str)
        print(f"  返回: {len(data)} 只股票（原始文本）")

    # Save
    if args.save:
        out = os.path.join(DATA_DIR, args.field, f"{date_str}.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            json.dump({"date": date_str, "field": args.field, "data": data}, f, ensure_ascii=False, indent=2)
        print(f"  保存: {out}")


if __name__ == "__main__":
    main()
