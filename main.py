#!/usr/bin/env python3
"""
Python launcher: VLESS + WebSocket proxy (Xray-core), stdlib only.

Port of https://github.com/free-dam-man/wispbyte-xray-ws-skill (Node) to Python.

What it does
  1) downloads Xray-core for this CPU (x86_64 / arm64) from GitHub, caches it
  2) writes the Xray config from env
  3) listens on $SERVER_PORT / $PORT:
       - VLESS+WS on WS_PATH  -> proxy traffic
       - any other HTTP path  -> small "OK" page (keeps PaaS health checks happy)
  4) restarts Xray if it crashes

Env
  SERVER_PORT | PORT   listen port (Pterodactyl injects SERVER_PORT, PaaS injects PORT)
  UUID                 VLESS id (fresh UUIDv4 per deploy; random one printed if unset)
  WS_PATH              WebSocket path (default /tk-rail)
  XRAY_VERSION         default v26.10.21
  XRAY_ZIP_URL         override download URL
  PUBLIC_HOST          optional: domain/IP clients use (for printing the share link)
  PUBLIC_TLS           "1" if the platform terminates TLS for you (PaaS domain, port 443)
  TG_BOT_TOKEN         optional: Telegram bot token (aliases TELEGRAM_BOT_TOKEN; a "123:ABC" value in
                       TELEGRAM_CHAT_ID is also taken as the token)
  TG_CHAT_ID           optional: Telegram chat id to receive the node link
  CF_TUNNEL_TOKEN      optional: Cloudflare Tunnel token; when set, cloudflared is
                       downloaded and run alongside xray (tunnel ingress must point to
                       http://localhost:8080, one of the EXTRA_PORTS)
  If PUBLIC_HOST is unset, the domain is taken from common PaaS env vars or from the
  first public HTTP request's Host header, then the link is pushed to Telegram once.
"""
import io, json, os, platform, signal, subprocess, sys, threading, time, urllib.parse, urllib.request, uuid, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("SERVER_PORT") or os.environ.get("PORT") or 8080)
UUID = os.environ.get("UUID") or ""
WS_PATH = os.environ.get("WS_PATH", "/tk-rail")
WS_PATH = WS_PATH if WS_PATH.startswith("/") else "/" + WS_PATH
XRAY_VERSION = os.environ.get("XRAY_VERSION", "v26.10.21")
XRAY_BIN = os.path.join(HERE, "xray")
CONFIG = os.path.join(HERE, "xray-config.json")
CF_BIN = os.path.join(HERE, "cloudflared")
WS_INNER = PORT + 10000 if PORT < 55000 else PORT - 10000   # internal ports (localhost only)
WEB_INNER = WS_INNER + 1
# extra public ports: some PaaS route to a fixed port without telling the app (e.g. 3000/5000/8000)
EXTRA_PORTS = [int(p) for p in os.environ.get("EXTRA_PORTS", "3000,5000,8000,8080,8888").split(",")
               if p.strip().isdigit() and int(p) not in (PORT, WS_INNER, WEB_INNER)]


def _tg_conf():
    tok = os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    chat = os.environ.get("TG_CHAT_ID") or ""
    t2 = os.environ.get("TELEGRAM_CHAT_ID") or ""
    if ":" in t2 and not tok:
        tok = t2
    elif t2 and not chat:
        chat = t2
    return tok.strip(), chat.strip()


_pushed = set()
_push_lock = threading.Lock()


def push_node(host):
    host = (host or "").split(",")[0].split(":")[0].strip().lower()
    if not host or host in ("localhost",) or host.replace(".", "").isdigit():
        return
    with _push_lock:
        if host in _pushed:
            return
        _pushed.add(host)
    os.environ["PUBLIC_HOST"] = host
    link = share_link()
    log("share link:", link)
    tok, chat = _tg_conf()
    if not (tok and chat):
        return

    def send():
        text = f"pyxray node online\nhost: {host}\n\n{link}"
        data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
        for n in range(3):
            try:
                urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=20).read()
                log("node pushed to telegram")
                return
            except Exception as e:
                log("telegram push failed:", e)
                time.sleep(5)
    threading.Thread(target=send, daemon=True).start()


def env_host():
    for k in ("PUBLIC_HOST", "RAILWAY_PUBLIC_DOMAIN", "RENDER_EXTERNAL_HOSTNAME", "KOYEB_PUBLIC_DOMAIN",
              "FLY_APP_NAME", "VERCEL_URL", "APP_DOMAIN", "DOMAIN", "HOSTNAME_PUBLIC"):
        v = os.environ.get(k)
        if v:
            if k == "FLY_APP_NAME":
                v = v + ".fly.dev"
            return v.replace("https://", "").replace("http://", "").strip("/")
    return ""


def log(*a):
    print("[pyxray]", *a, flush=True)


def zip_url():
    if os.environ.get("XRAY_ZIP_URL"):
        return os.environ["XRAY_ZIP_URL"]
    m = platform.machine().lower()
    name = {"x86_64": "64", "amd64": "64", "aarch64": "arm64-v8a", "arm64": "arm64-v8a",
            "armv7l": "arm32-v7a", "i686": "32", "i386": "32"}.get(m, "64")
    return f"https://github.com/XTLS/Xray-core/releases/download/{XRAY_VERSION}/Xray-linux-{name}.zip"


def ensure_xray():
    if os.path.exists(XRAY_BIN) and os.path.getsize(XRAY_BIN) > 1_000_000:
        os.chmod(XRAY_BIN, 0o755)
        log("reusing cached xray")
        return
    url = zip_url()
    last = None
    for n in range(1, 4):
        try:
            log(f"downloading {url} (attempt {n})")
            req = urllib.request.Request(url, headers={"User-Agent": "pyxray/1.0"})
            data = urllib.request.urlopen(req, timeout=120).read()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                with z.open("xray") as src, open(XRAY_BIN, "wb") as dst:
                    dst.write(src.read())
            os.chmod(XRAY_BIN, 0o755)
            log("xray extracted, bytes:", os.path.getsize(XRAY_BIN))
            return
        except Exception as e:
            last = e
            log("download failed:", e)
            time.sleep(3)
    raise SystemExit(f"[pyxray] FATAL: cannot get xray: {last}")


def cf_url():
    if os.environ.get("CF_URL"):
        return os.environ["CF_URL"]
    m = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(m, "amd64")
    return f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"


def ensure_cloudflared():
    if os.path.exists(CF_BIN) and os.path.getsize(CF_BIN) > 1_000_000:
        os.chmod(CF_BIN, 0o755)
        log("reusing cached cloudflared")
        return
    url = cf_url()
    last = None
    for n in range(1, 4):
        try:
            log(f"downloading {url} (attempt {n})")
            req = urllib.request.Request(url, headers={"User-Agent": "pyxray/1.0"})
            data = urllib.request.urlopen(req, timeout=120).read()
            with open(CF_BIN, "wb") as dst:
                dst.write(data)
            os.chmod(CF_BIN, 0o755)
            log("cloudflared downloaded, bytes:", os.path.getsize(CF_BIN))
            return
        except Exception as e:
            last = e
            log("cloudflared download failed:", e)
            time.sleep(3)
    raise RuntimeError(f"cannot get cloudflared: {last}")


def run_tunnel(token):
    while True:
        try:
            child = subprocess.Popen([CF_BIN, "tunnel", "run", "--protocol", "http2", "--token", token])
            code = child.wait()
            log(f"cloudflared exited code={code}, restarting in 5s")
        except Exception as e:
            log("cloudflared spawn failed:", e)
        time.sleep(5)


def write_config():
    def public(port):
        return {"listen": "0.0.0.0", "port": port, "protocol": "vless",
                "settings": {"clients": [{"id": UUID}], "decryption": "none",
                             "fallbacks": [{"path": WS_PATH, "dest": WS_INNER, "xver": 0},
                                           {"dest": WEB_INNER, "xver": 0}]},
                "streamSettings": {"network": "tcp", "security": "none"}}
    cfg = {
        "log": {"loglevel": os.environ.get("XRAY_LOGLEVEL", "warning")},
        "inbounds": [
            {   # public port: VLESS raw TCP, every plain HTTP request falls back by path
                "listen": "0.0.0.0", "port": PORT, "protocol": "vless",
                "settings": {"clients": [{"id": UUID}], "decryption": "none",
                             "fallbacks": [{"path": WS_PATH, "dest": WS_INNER, "xver": 0},
                                           {"dest": WEB_INNER, "xver": 0}]},
                "streamSettings": {"network": "tcp", "security": "none"},
            },
            {   # internal: VLESS + WS
                "listen": "127.0.0.1", "port": WS_INNER, "protocol": "vless",
                "settings": {"clients": [{"id": UUID}], "decryption": "none"},
                "streamSettings": {"network": "ws", "security": "none", "wsSettings": {"path": WS_PATH}},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
            },
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}, {"protocol": "blackhole", "tag": "block"}],
    }
    cfg["inbounds"] += [public(p) for p in EXTRA_PORTS]
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


class Web(BaseHTTPRequestHandler):
    def do_GET(self):
        push_node(self.headers.get("X-Forwarded-Host") or self.headers.get("Host"))
        body = b"OK\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_HEAD = do_GET

    def log_message(self, *a):
        pass


def share_link():
    host = os.environ.get("PUBLIC_HOST", "<server-ip-or-domain>")
    p = urllib.parse.quote(WS_PATH, safe="")
    if os.environ.get("PUBLIC_TLS") == "1":
        return f"vless://{UUID}@{host}:443?encryption=none&security=tls&sni={host}&type=ws&host={host}&path={p}#pyxray"
    return f"vless://{UUID}@{host}:{PORT}?encryption=none&security=none&type=ws&host={host}&path={p}#pyxray"


def main():
    global UUID
    if not UUID:
        UUID = str(uuid.uuid4())
        log("WARNING: UUID not set, generated one for this run (set env UUID to keep it fixed)")
    ensure_xray()
    write_config()
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", WEB_INNER), Web).serve_forever, daemon=True).start()
    cf_token = os.environ.get("CF_TUNNEL_TOKEN", "").strip()
    if cf_token:
        try:
            ensure_cloudflared()
            threading.Thread(target=run_tunnel, args=(cf_token,), daemon=True).start()
            log("cloudflared tunnel starting (ingress must point to http://localhost:8080)")
        except Exception as e:
            log("cloudflared unavailable, continuing without tunnel:", e)
    log(f"listening on {PORT} (+{EXTRA_PORTS}), ws path {WS_PATH}")
    if env_host():
        push_node(env_host())
    else:
        log("share link (host unknown until first request):", share_link())

    child = None

    def stop(*_):
        if child and child.poll() is None:
            child.terminate()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while True:
        child = subprocess.Popen([XRAY_BIN, "run", "-c", CONFIG])
        code = child.wait()
        log(f"xray exited code={code}, restarting in 5s")
        time.sleep(5)


if __name__ == "__main__":
    main()
