"""local-web-extract sidecar (tier 1): curl_cffi download + trafilatura / Readability / pypdf.

POST /extract {"url": "..."} -> {"ok", "status", "final_url", "title", "text", "method", "chars", "error",
                                 "error_kind", "challenge"}
GET  /health -> {"ok": true, ...}

Listens on 0.0.0.0:8080 *inside the container*; the host-side run.sh publishes it on 127.0.0.1 only.
Verification walls are only detected (-> error_kind "challenge"); nothing here tries to pass them.
"""
import io
import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DL_TIMEOUT = 20          # seconds per download
MAX_BYTES = 25_000_000   # response body cap
MAX_REDIRECTS = 5
MIN_GOOD = 800           # trafilatura shorter than this -> also try Readability on the same HTML
MAX_PDF_PAGES = 400
_SEM = threading.BoundedSemaphore(int(os.environ.get("LOCAL_WEB_EXTRACT_CONCURRENCY", "4")))


class _Fail(Exception):
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind, self.msg = kind, msg


def check_public(url):
    """None if the URL may be fetched, else (error_kind, message). Refuses private/loopback/link-local targets."""
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return ("other", "unsupported URL scheme")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return ("dns", "DNS resolution failed for %s: %s" % (p.hostname, e))
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return ("blocked_ip", "Blocked: URL targets a private or internal network address")
    return None


def fetch(url):
    """Follow redirects by hand so every hop is checked. Returns (response, final_url) or raises _Fail."""
    from curl_cffi import requests as cr
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        bad = check_public(cur)
        if bad:
            raise _Fail(*bad)
        try:
            r = cr.get(cur, impersonate="chrome", timeout=DL_TIMEOUT, allow_redirects=False)
        except Exception as e:
            name = type(e).__name__
            kind = "timeout" if "imeout" in name or "timed out" in str(e).lower() else "other"
            raise _Fail(kind, "%s: %s" % (name, str(e)[:160]))
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            cur = urllib.parse.urljoin(cur, r.headers["location"])
            continue
        return r, cur
    raise _Fail("other", "too many redirects")


def detect_challenge(status, headers, body):
    """Name of a human-verification wall (DataDome / Cloudflare) from markers, else None.

    A bare 403 is NOT treated as a challenge (plenty of sites 403 plain bots without any wall).
    Detection only: callers must give up on the page, never try to pass it.
    """
    try:
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        raw = body[:120_000] if isinstance(body, (bytes, bytearray)) else str(body)[:120_000].encode("utf-8", "replace")
        low = raw.decode("utf-8", "replace").lower()
        small = len(body) < 30_000
        blocked = status in (401, 403, 429, 503)
        if "challenge" in h.get("cf-mitigated", "").lower():
            return "Cloudflare"
        if blocked or small:
            if ("just a moment" in low and ("challenges.cloudflare.com" in low or "cf-chl" in low
                                            or "_cf_chl_opt" in low or "<title>just a moment" in low)) \
                    or ("challenges.cloudflare.com/cdn-cgi/challenge-platform" in low and small) \
                    or "cf-chl-bypass" in low or "_cf_chl_opt" in low \
                    or ("attention required" in low and "cloudflare" in low and "you have been blocked" in low):
                return "Cloudflare"
            if "captcha-delivery.com" in low or ("datadome" in h.get("server", "").lower() and blocked):
                return "DataDome"
    except Exception:
        pass
    return None


def run_readability(html, url):
    try:
        out = subprocess.run(
            ["node", "/app/readability_cli.js", url], input=html[:8_000_000].encode("utf-8", "replace"),
            capture_output=True, timeout=20, env={"NODE_PATH": "/app/node_modules", "PATH": os.environ.get("PATH", "")},
        )
        if out.returncode != 0:
            return "", ""
        d = json.loads(out.stdout.decode("utf-8", "replace"))
        return d.get("text") or "", d.get("title") or ""
    except Exception:
        return "", ""


def extract_pdf(data):
    from pypdf import PdfReader
    rd = PdfReader(io.BytesIO(data))
    pages = rd.pages[:MAX_PDF_PAGES]
    title = ""
    try:
        title = (rd.metadata.title or "") if rd.metadata else ""
    except Exception:
        pass
    return "\n".join((pg.extract_text() or "") for pg in pages).strip(), title


def do_extract(url):
    import trafilatura
    res = {"ok": False, "status": None, "final_url": url, "title": "", "text": "", "method": "", "chars": 0,
           "error": None, "error_kind": None, "challenge": None}
    try:
        r, final = fetch(url)
    except _Fail as f:
        res["error"], res["error_kind"] = f.msg, f.kind
        return res
    res["status"], res["final_url"] = r.status_code, final
    ch = detect_challenge(r.status_code, r.headers, r.content)
    if ch:  # detection only: report and stop
        res["error"] = "blocked by anti-bot challenge (%s)" % ch
        res["error_kind"], res["challenge"] = "challenge", ch
        return res
    if r.status_code >= 400:
        res["error"], res["error_kind"] = "HTTP %d from origin" % r.status_code, "http"
        return res
    body = r.content
    if len(body) > MAX_BYTES:
        res["error"], res["error_kind"] = "response too large (%d bytes)" % len(body), "other"
        return res
    ctype = (r.headers.get("content-type") or "").lower()
    try:
        if "pdf" in ctype or body[:5] == b"%PDF-":
            text, title = extract_pdf(body)
            res.update(text=text, title=title, method="pypdf")
        else:
            text = trafilatura.extract(body, url=final, include_comments=False, include_tables=True,
                                       favor_recall=True) or ""
            title = ""
            try:
                md = trafilatura.extract_metadata(body, default_url=final)
                title = (md.title or "") if md else ""
            except Exception:
                pass
            method = "trafilatura"
            if len(text) < MIN_GOOD:  # same HTML through Readability, keep the longer result
                rtext, rtitle = run_readability(r.text, final)
                if len(rtext) > len(text):
                    text, method = rtext, "readability"
                    title = rtitle or title
            res.update(text=text, title=title, method=method)
    except Exception as e:
        res["error"], res["error_kind"] = "extract failed: %s: %s" % (type(e).__name__, str(e)[:160]), "other"
        return res
    res["chars"] = len(res["text"])
    res["ok"] = res["chars"] > 0
    if not res["ok"]:
        res["error"], res["error_kind"] = "empty extraction (raw %d bytes)" % len(body), "empty"
    return res


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "service": "local-web-extract-sidecar", "ts": int(time.time())})
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        if self.path != "/extract":
            return self._send(404, {"ok": False})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            url = json.loads(self.rfile.read(n) or b"{}").get("url", "")
        except Exception:
            return self._send(400, {"ok": False, "error": "bad request"})
        with _SEM:
            self._send(200, do_extract(url))

    def log_message(self, fmt, *a):  # never write URLs to container logs
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
