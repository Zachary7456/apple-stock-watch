#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iPhone Duo 门店库存巡检（云端版 · 纯标准库，可跑在 GitHub Actions）

苹果中国站的 /shop/retail/pickup-message 接口无需 cookie、无需浏览器，
普通 HTTP 请求就能拿到 6 家北京门店的实时取货状态。

用法:
  python3 watch.py                # 跑一次
  PUSH=ntfy NTFY_TOPIC=xxx python3 watch.py

推送通道（环境变量）:
  PUSH=ntfy       NTFY_TOPIC
  PUSH=bark       BARK_URL
  PUSH=serverchan SC_KEY
  PUSH=telegram   TG_TOKEN + TG_CHAT
  PUSH=webhook    WEBHOOK_URL        # POST {"text": "..."} 到任意 URL
  不设置 PUSH 则只打印到日志
"""
import os
import sys
import json
import time
import subprocess
import urllib.parse
import urllib.request

TARGETS = {
    "MK2M4CH/A": "iPhone Duo 256GB 星光白色",
    # "MK2P4CH/A": "iPhone Duo 512GB 星光白色",
    # "MK2N4CH/A": "iPhone Duo 256GB 夜空色",
}

STORES = {
    "R320": "三里屯",
    "R388": "西单大悦城",
    "R645": "朝阳大悦城",
    "R448": "王府井",
    "R479": "华贸购物中心",
    "R792": "北京荟聚",
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BASE = "https://www.apple.com.cn/shop/retail/pickup-message?parts.0={part}&little=true&store={store}"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


def _curl_query(url):
    """兜底：用 curl + HTTP/2 再试一次（GitHub runner 自带 curl）"""
    try:
        p = subprocess.run(
            ["curl", "-s", "--http2", "-m", "25", "-A", UA,
             "-H", "Accept: application/json", "-H", "Accept-Language: zh-CN,zh;q=0.9", url],
            capture_output=True, text=True, timeout=30)
        return json.loads(p.stdout)
    except Exception:
        return None


def query(part, store, retries=3):
    """返回 'available' / 'unavailable' / 'unknown'"""
    url = BASE.format(part=part, store=store)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                j = json.loads(r.read().decode("utf-8"))
            st = j["body"]["stores"][0]
            pa = st["partsAvailability"][part]
            return pa.get("pickupDisplay") or "unknown"
        except Exception as e:
            if i == retries - 1:
                j2 = _curl_query(url)          # curl 兜底
                if j2:
                    try:
                        return j2["body"]["stores"][0]["partsAvailability"][part].get(
                            "pickupDisplay") or "unknown"
                    except Exception:
                        pass
                print(f"  ! {store}/{part} 查询失败: {type(e).__name__} {e}")
                return "unknown"
            time.sleep(3 * (i + 1))
    return "unknown"


def _send_one(kind, title, body):
    def _open(url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, headers=headers or {})
        urllib.request.urlopen(req, timeout=25).read()

    if kind == "ntfy":
        _open(f"https://ntfy.sh/{os.environ['NTFY_TOPIC']}",
              data=body.encode("utf-8"),
              headers={"Title": title.encode("utf-8").decode("latin-1"),
                       "Priority": "5", "Tags": "apple"})
    elif kind == "bark":
        # safe='' 必须保留：否则正文里的 https:// 斜杠不转义，Bark 会按路径切分并返回 404
        _open(f"{os.environ['BARK_URL'].rstrip('/')}/"
              f"{urllib.parse.quote(title, safe='')}/"
              f"{urllib.parse.quote(body, safe='')}?group=duo&level=timeSensitive")
    elif kind == "serverchan":
        _open(f"https://sctapi.ftqq.com/{os.environ['SC_KEY']}.send?"
              f"title={urllib.parse.quote(title)}&desp={urllib.parse.quote(body)}")
    elif kind == "telegram":
        _open(f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage?"
              f"chat_id={os.environ['TG_CHAT']}&text="
              f"{urllib.parse.quote(title + chr(10) + body)}")
    elif kind == "webhook":
        _open(os.environ["WEBHOOK_URL"],
              data=json.dumps({"text": f"{title}\n{body}"}).encode(),
              headers={"Content-Type": "application/json"})
    else:
        raise ValueError(f"未知通道 {kind}")


def push(title, body):
    """PUSH 支持逗号分隔多通道，例如 PUSH=bark,ntfy（任意一个成功即算送达）"""
    kinds = [k.strip().lower() for k in os.environ.get("PUSH", "").split(",") if k.strip()]
    if not kinds:
        print("[未配置推送]", title, "|", body)
        return
    ok = []
    for kind in kinds:
        for attempt in (1, 2):
            try:
                _send_one(kind, title, body)
                ok.append(kind)
                print(f"推送成功: {kind}" + ("" if attempt == 1 else "（第2次重试成功）"))
                break
            except Exception as e:
                if attempt == 2:
                    print(f"推送失败 {kind}: {type(e).__name__} {e}")
                else:
                    time.sleep(3)
    return ok


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    if os.environ.get("FORCE_TEST"):
        # 用与真实告警完全相同的标题和正文格式（含 URL），确保能测出编码类问题
        push("📦 iPhone Duo 有货了！",
             "【测试消息】三里屯：iPhone Duo 256GB 星光白色\n\n"
             "https://www.apple.com.cn/shop/buy-iphone/iphone-duo")
        print("测试推送已从云端发出")
        return 0
    state = load_state()
    hits, lines, unknown = [], [], 0
    for part, label in TARGETS.items():
        for sid, sname in STORES.items():
            st = query(part, sid)
            icon = {"available": "✅", "unavailable": "❌"}.get(st, "⚠️")
            lines.append(f"{sname}{icon}")
            if st == "unknown":
                unknown += 1
            elif st == "available":
                hits.append((part, label, sname, sid))
    print(" ".join(lines))

    total = len(TARGETS) * len(STORES)
    if unknown == total:
        print("全部查询失败（网络或接口异常）")
        push("⚠️ 云端巡检异常", "本次全部查询失败，可能是网络或苹果接口变动，请检查 Actions 日志。")
        return 1

    if not hits:
        print("无货")
        return 0

    now = time.time()
    fresh = [h for h in hits if now - state.get(f"{h[0]}|{h[3]}", 0) > 3600]
    body = "\n".join(f"{h[2]}：{h[1]}" for h in hits)
    print("有货！\n" + body)
    if fresh:
        push("📦 iPhone Duo 有货了！", body + "\n\nhttps://www.apple.com.cn/shop/buy-iphone/iphone-duo")
        for h in fresh:
            state[f"{h[0]}|{h[3]}"] = now
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    else:
        print("（1 小时内已提醒过，跳过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
