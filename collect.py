#!/usr/bin/env python3
"""自有电台库：从 Radio Browser 聚合全球电台，逐台验活并分类，只留能用的。

判定（与电视库不同：收音机有自家代理兜底，http/无 CORS 不是死刑）：
  - 普通流可达            → 收录；https 且带 CORS 标 mode=direct，否则 mode=proxy
  - HLS(.m3u8) 可达       → 收录；hls=1 表示 https+带 CORS 可直连播，hls=0 表示不可直连
  - 播放列表文件(.pls/.m3u) → 解析第一条流再验（限一层），以解析出的地址收录
  - 不可达 / 落地页(html) / 状态异常 → 剔除
产物字段沿用收音机现有 data/*.json 格式（stationuuid/name/url_resolved/urls/
favicon/tags/country/votes，HLS 台带 hls），另加 mode，前端可忽略。
老式 Shoutcast 返回非标准状态行 ICY 200，urllib 会误判，socket 兜底判活。
"""
import concurrent.futures as cf
import json
import os
import re
import socket
import ssl
import sys
import urllib.request
import urllib.error
from urllib.parse import urlparse

RB_HOSTS = [
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
]
UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
APP_ORIGIN = "https://app.benfen.net"
TIMEOUT = 8
WORKERS = 40
LIMIT = int(os.environ.get("LIMIT", "0"))

COUNTRIES = ["CN", "HK", "TW", "US", "GB", "JP", "KR", "DE",
             "FR", "CA", "AU", "ES", "IT", "NL", "IN", "BR", "RU"]


def rb_fetch(path):
    for h in RB_HOSTS:
        try:
            req = urllib.request.Request(h + path, headers={"User-Agent": "benfen-radio-lib/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            continue
    raise RuntimeError(f"Radio Browser 全部镜像不可用: {path}")


def aggregate():
    seen, out = set(), []

    def add(items):
        for s in items:
            u = s.get("url_resolved") or ""
            uuid = s.get("stationuuid") or ""
            if not u or not uuid or uuid in seen or u in seen:
                continue
            seen.add(uuid)
            seen.add(u)
            out.append(s)

    add(rb_fetch("/json/stations/topvote/600"))
    add(rb_fetch("/json/stations/topclick/600"))
    for cc in COUNTRIES:
        try:
            add(rb_fetch(f"/json/stations/search?countrycode={cc}"
                         "&hidebroken=true&order=votes&reverse=true&limit=400"))
        except Exception as e:
            print(f"[{cc}] 聚合失败: {e}", file=sys.stderr)
    print(f"聚合去重后 {len(out)} 台", file=sys.stderr)
    return out


def cors_ok(headers):
    acao = ""
    for k, v in headers.items():
        if k.lower() == "access-control-allow-origin":
            acao = v.strip()
    return acao in ("*", APP_ORIGIN)


def probe(url):
    """GET 少量字节判活。返回 dict(status, ctype, cors, body_head) 或 None。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Origin": APP_ORIGIN,
        "Referer": APP_ORIGIN + "/radio/", "Range": "bytes=0-2047",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(2048)
            return {"status": r.status, "ctype": (r.headers.get("Content-Type") or "").lower(),
                    "cors": cors_ok(r.headers), "body": body}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2048)
        except Exception:
            body = b""
        return {"status": e.code, "ctype": (e.headers.get("Content-Type") or "").lower() if e.headers else "",
                "cors": cors_ok(e.headers) if e.headers else False, "body": body}
    except Exception:
        return None


def socket_alive(url):
    """Shoutcast ICY 兜底：原始 socket 发请求，看首行是否含 200/ICY。"""
    try:
        p = urlparse(url)
        host = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        raw = socket.create_connection((host, port), timeout=TIMEOUT)
        if p.scheme == "https":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        raw.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: {UA}\r\n\r\n".encode())
        head = raw.recv(256).decode("latin-1", "ignore")
        raw.close()
        return "200" in head.splitlines()[0] if head else False
    except Exception:
        return False




def extract_stream(body):
    text = body.decode("utf-8", "ignore")
    m = re.findall(r"https?://[^\s\"'<>]+", text)
    return m[0] if m else None


def verify(s, depth=0):
    """返回收录记录 dict 或 None。depth 是播放列表解析层数，最多 1 层。"""
    url = s.get("url_resolved") or ""
    if not url.startswith(("http://", "https://")):
        return None
    pr = probe(url)
    if pr is None:
        # urllib 判不了的老式流，socket 兜底
        if not socket_alive(url):
            return None
        pr = {"status": 200, "ctype": "", "cors": False, "body": b""}
    if pr["status"] not in (200, 206):
        return None
    ctype = pr["ctype"]
    body_head = pr["body"][:600].decode("utf-8", "ignore") if pr["body"] else ""
    # 先认 HLS：m3u8 的 Content-Type 同属 mpegurl 家族，不能被下面的列表解析误伤
    is_hls = (url.split("?")[0].lower().endswith(".m3u8")
              or "#EXT-X-" in body_head)
    if is_hls:
        if "text/html" in ctype:
            return None
        rec = _record(s, url)
        rec["hls"] = 1 if (url.startswith("https://") and pr["cors"]) else 0
        rec["mode"] = "hls"
        return rec if rec["name"] else None
    # 播放列表文件（.pls 等）：解析一层
    if any(t in ctype for t in ("x-scpls", "pls+xml")) or url.lower().endswith(".pls"):
        if depth >= 1:
            return None
        inner = extract_stream(pr["body"])
        if not inner or inner == url:
            return None
        s = dict(s)
        s["url_resolved"] = inner
        return verify(s, depth + 1)
    if "text/html" in ctype:
        return None
    rec = _record(s, url)
    rec["mode"] = "direct" if (url.startswith("https://") and pr["cors"]) else "proxy"
    if not rec["name"]:
        return None
    return rec


def _record(s, url):
    return {
        "stationuuid": s.get("stationuuid", ""),
        "name": (s.get("name") or "").strip(),
        "url_resolved": url,
        "urls": [url],
        "favicon": s.get("favicon", ""),
        "tags": s.get("tags", ""),
        "country": s.get("country", ""),
        "countrycode": s.get("countrycode", ""),
        "votes": s.get("votes", 0),
        "bitrate": s.get("bitrate", 0),
        "codec": s.get("codec", ""),
    }


def main():
    stations = aggregate()
    if LIMIT:
        stations = stations[:LIMIT]
    print(f"共 {len(stations)} 台待验证，并发 {WORKERS}", file=sys.stderr)
    kept, modes = [], {}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for rec in ex.map(verify, stations):
            if rec:
                kept.append(rec)
                modes[rec["mode"]] = modes.get(rec["mode"], 0) + 1
    kept.sort(key=lambda r: -r["votes"])
    print(f"收录 {len(kept)}/{len(stations)}；模式分布 {modes}", file=sys.stderr)

    os.makedirs("out", exist_ok=True)
    with open("out/stations.json", "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, separators=(",", ":"))
    by_country = {}
    for r in kept:
        by_country[r["countrycode"] or "??"] = by_country.get(r["countrycode"] or "??", 0) + 1
    stats = {"checked": len(stations), "kept": len(kept),
             "modes": modes, "by_country": by_country}
    with open("out/stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    import datetime
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>"
                   for k, v in sorted(by_country.items(), key=lambda x: -x[1])[:15])
    with open("out/index.html", "w", encoding="utf-8") as f:
        f.write(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>自有电台库 · 全部验活</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.7;color:#222}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 12px}}code{{background:#f2f2f2;padding:2px 6px}}</style>
</head><body>
<h1>自有电台库</h1>
<p>从 Radio Browser 聚合全球电台，逐台服务端验活，只收录能连通的；每台标注播放方式（直连 / 代理 / HLS）。</p>
<p>数据地址：<code>https://benfennet.github.io/radio-verified/stations.json</code></p>
<p>最近验证：{now.strftime('%Y-%m-%d %H:%M')}（北京时间）· 共验 {len(stations)} 台，收录 {len(kept)} 台（直连 {modes.get('direct',0)} / 代理 {modes.get('proxy',0)} / HLS {modes.get('hls',0)}）。</p>
<table><tr><th>国家/地区</th><th>台数</th></tr>{rows}</table>
<p>每天自动重新验证两次。</p>
</body></html>""")
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
