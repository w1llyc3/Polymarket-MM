"""Local HTTP shell; requests enqueue commands, never touch account SQLite."""
import argparse
import hashlib
import hmac
import json
import secrets
import signal
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .service import ConsoleService

APP = "NBA_MM_LOCAL_CONSOLE_V2_2"
STATIC = Path(__file__).parent/"static"


def make_server(service, port=8765):
    token = secrets.token_urlsafe(32)
    root_id = hashlib.sha256(str(service.root).encode()).hexdigest()[:16]
    hosts = {"127.0.0.1:"+str(port), "localhost:"+str(port)}
    origins = {"http://"+host for host in hosts}

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass  # never log authorization headers or arbitrary input

        def send(self, status, value, content_type="application/json; charset=utf-8", filename=None):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if filename:
                self.send_header("Content-Disposition", 'attachment; filename="'+filename+'"')
            self.end_headers()
            self.wfile.write(raw)

        def trusted(self, writing=False):
            if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") not in hosts:
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in origins:
                return False
            if writing:
                return origin in origins and hmac.compare_digest(self.headers.get("X-Console-Token", ""), token)
            return self.headers.get("Sec-Fetch-Site") not in {"cross-site"}

        def do_GET(self):
            if not self.trusted():
                self.send(403, {"error": "拒绝非本地来源"})
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/health":
                from ..api import utc_ms
                self.send(200, {"app": APP, "root_id": root_id, "owner_alive": bool(service.thread and service.thread.is_alive()), "owner_responsive": utc_ms()-service.read().get("server_ms", 0) < 4000})
            elif path == "/api/bootstrap":
                self.send(200, {"app": APP, "token": token, "real_trading_permission": False})
            elif path == "/api/state":
                self.send(200, service.read())
            elif path.startswith("/api/receipt/"):
                receipt = service.receipt(path.rsplit("/", 1)[1])
                self.send(200 if receipt else 404, receipt or {"error": "回执不存在"})
            elif path.startswith("/api/export/"):
                with service.lock:
                    file = service.exports.get(path.rsplit("/", 1)[1])
                if file:
                    self.send(200, file.read_bytes(), filename="nba-paper-report.json")
                else:
                    self.send(404, {"error": "导出不存在，请重新导出"})
            elif path in {"/", "/app.js", "/style.css"}:
                name, mime = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}[path]
                self.send(200, (STATIC/name).read_bytes(), mime+"; charset=utf-8")
            else:
                self.send(404, {"error": "页面不存在"})

        def do_POST(self):
            if not self.trusted(writing=True):
                self.send(403, {"error": "来源或会话令牌无效，请从本地页面重试"})
                return
            if self.path != "/api/command":
                self.send(404, {"error": "接口不存在"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768 or self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("仅支持有界 JSON 命令")
                command = json.loads(self.rfile.read(length))
                result = service.submit(command)
                self.send(202, result)
            except (ValueError, TypeError, UnicodeError):
                self.send(400, {"error": "命令格式或幂等键无效"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if port == 0:
        hosts.clear()
        hosts.update({"127.0.0.1:"+str(server.server_port), "localhost:"+str(server.server_port)})
        origins.clear()
        origins.update("http://"+host for host in hosts)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="NBA MM 本地操作台，只支持公开数据与 Paper")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须在1024至65535之间")
    root = Path(__file__).resolve().parents[2]
    url = "http://127.0.0.1:"+str(args.port)
    root_id = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    # Probe known app before constructing any runner/writer.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url+"/api/health", timeout=1) as response:
            existing = json.load(response)
        if existing.get("app") == APP and existing.get("root_id") == root_id:
            if not existing.get("owner_alive") or not existing.get("owner_responsive"):
                print("已有操作台后台异常或更新超时，请先关闭旧操作台进程，再重新启动。未启动第二个写者。")
                raise SystemExit(1)
            print("操作台已经运行，复用已有实例："+url)
            if not args.no_browser:
                webbrowser.open(url)
            return
    except Exception:
        pass
    service = None
    try:
        service = ConsoleService(root)
        server = make_server(service, args.port)
    except OSError:
        print("启动失败：端口被其他程序占用或 data/console 无写入权限。关闭冲突程序，或用 --port 8766；检查目录权限后重试。未启动第二个账户写者。")
        raise SystemExit(1)
    try:
        service.start()
    except RuntimeError as error:
        server.server_close()
        print("启动失败："+str(error)+"。未创建第二个账户写者。")
        raise SystemExit(1)
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print("NBA MM 本地操作台："+url+"；无真实交易权限。关网页不停止后台；请用停止按钮。")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        service.close()
        server.server_close()


if __name__ == "__main__":
    main()
