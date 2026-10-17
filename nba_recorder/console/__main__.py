"""Local HTTP shell; requests enqueue commands, never touch account SQLite."""
import argparse
import hashlib
import hmac
import json
import secrets
import signal
import sqlite3
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
            elif path == '/api/report-download':
                from urllib.parse import urlparse,parse_qs
                from .history import resolve
                import re
                try:
                    p=parse_qs(urlparse(self.path).query,strict_parsing=True)
                    if set(p)!={'session_id','report_id','format'} or any(len(v)!=1 for v in p.values()):raise ValueError('download')
                    resolve(service.data,p['session_id'][0]);rid=p['report_id'][0];fmt=p['format'][0]
                    if not re.fullmatch('[0-9a-f]{32}',rid) or fmt not in {'json','md'}:raise ValueError('format')
                    file=service.data/(p['session_id'][0]+'-report-'+rid+'.'+fmt)
                    if file.is_symlink():raise ValueError('symlink')
                    self.send(200,file.read_bytes(),'text/markdown; charset=utf-8' if fmt=='md' else 'application/json',filename='nba-research-report.'+fmt)
                except (ValueError,KeyError,OSError):self.send(400,{'error':'报告文件不存在或引用无效'})
            elif path in {'/api/sessions','/api/history','/api/research','/api/reports'}:
                from urllib.parse import urlparse, parse_qs
                from . import history
                try:
                    params=parse_qs(urlparse(self.path).query,strict_parsing=True)
                    if any(len(v)!=1 for v in params.values()) or set(params)-{'session_id'}:raise ValueError('query')
                    sid=params.get('session_id',[None])[0]
                    if path=='/api/sessions':value=history.index(service.data)
                    elif path=='/api/history':value=history.view(service.data,sid)
                    elif path=='/api/research':
                        from .research import analyze
                        value=analyze(history.resolve(service.data,sid))
                    else:
                        history.resolve(service.data,sid)
                        value=[json.loads(p.read_text(encoding='utf-8')) for p in sorted(service.data.glob(sid+'-report-*.json'))]
                    self.send(200,value)
                except (ValueError,KeyError,OSError,sqlite3.Error):self.send(400,{'error':'研究会话不可用'})
            elif path == "/api/chart-event":
                from urllib.parse import urlparse, parse_qs
                try:
                    params=parse_qs(urlparse(self.path).query,strict_parsing=True)
                    if set(params)!={'session_id','seq'} or any(len(v)!=1 for v in params.values()):raise ValueError('event reference')
                    self.send(200,service.chart_event(params['session_id'][0],params['seq'][0]))
                except (ValueError,KeyError,OSError,sqlite3.Error):
                    self.send(400,{'error':'当前会话事件引用无效'})
            elif path == "/api/chart":
                from urllib.parse import urlparse, parse_qs
                try:
                    params=parse_qs(urlparse(self.path).query,strict_parsing=True)
                    if any(len(v)!=1 for v in params.values()) or set(params)-{'session_id','asset_id','from','to','input','journal','phase','game'}:
                        raise ValueError('chart params')
                    self.send(200,service.chart_query({k:v[0] for k,v in params.items()}))
                except (ValueError,KeyError,OSError,sqlite3.Error):
                    self.send(400,{"error":"有界当前会话图表查询无效"})
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
            elif path in {"/", "/app.js", "/style.css", "/chart.js", "/workbench.js"}:
                name, mime = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"), "/chart.js": ("chart.js", "text/javascript"), "/workbench.js": ("workbench.js", "text/javascript")}[path]
                self.send(200, (STATIC/name).read_bytes(), mime+"; charset=utf-8")
            else:
                self.send(404, {"error": "页面不存在"})

        def do_POST(self):
            if not self.trusted(writing=True):
                self.send(403, {"error": "来源或会话令牌无效，请从本地页面重试"})
                return
            if self.path not in {"/api/command", "/api/reports"}:
                self.send(404, {"error": "接口不存在"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768 or self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("仅支持有界 JSON 命令")
                command = json.loads(self.rfile.read(length))
                if self.path == '/api/reports':
                    from .history import save_report
                    if set(command)-{'session_id','kind','as_of_ms','note'}:raise ValueError('report fields')
                    if command.get('kind')=='Q4_AUTO':raise ValueError('auto report is owner-triggered only')
                    result=save_report(service.data,command['session_id'],command['kind'],command.get('as_of_ms'),command.get('note',''))
                    self.send(201,result)
                    return
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
