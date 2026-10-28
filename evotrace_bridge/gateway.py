"""One metered DeepSeek gateway for every real coding and Meta request."""
from __future__ import annotations

import json
import os
import secrets
import hashlib
import hmac
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .common import BridgeError, now, write_json


def authorized_connection(path=None):
    selected = Path(path or os.environ.get('TRACEWALTZ_ENV_FILE') or Path(__file__).resolve().parents[1] / '.env')
    values = {}
    if selected.exists():
        for line in selected.read_text(encoding='utf-8-sig').splitlines():
            name, separator, value = line.partition('=')
            if separator and name.strip() and not name.strip().startswith('#'):
                values[name.strip()] = value.strip().strip('"').strip("'")
    def get(name, default=''):
        return os.environ.get(name, values.get(name, default))
    key = get('DEEPSEEK_API_KEY')
    if not key or key == 'replace-me':
        raise BridgeError('missing_credential', 'Set DEEPSEEK_API_KEY in the environment or project .env')
    return get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/'), key, get('DEEPSEEK_MODEL', 'deepseek-flash')


class RequestLedger:
    @contextmanager
    def connect(self):
        database = sqlite3.connect(self.path, timeout=30)
        try:
            with database:
                yield database
        finally:
            database.close()

    def __init__(self, directory, limit=None):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "requests.sqlite"
        if limit is not None and (type(limit) is not int or limit < 1):
            raise BridgeError('invalid_budget', '调用上限必须是正整数')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, started TEXT, status TEXT, result TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, request_limit INTEGER)")
            old = db.execute("SELECT request_limit FROM settings WHERE id=1").fetchone()
            if old and limit is not None and old[0] != limit:
                raise BridgeError("budget_changed", "预算台账已建立，不能通过重启改变请求上限")
            db.execute("INSERT OR IGNORE INTO settings VALUES (1,?)", (limit if limit is not None else 500,))
            db.execute('CREATE TABLE IF NOT EXISTS budget_authorizations (id INTEGER PRIMARY KEY, at TEXT, previous_limit INTEGER, request_limit INTEGER, used_requests INTEGER, reason TEXT)')

    @property
    def limit(self):
        with self.connect() as db:
            return db.execute('SELECT request_limit FROM settings WHERE id=1').fetchone()[0]

    def authorize_limit(self, limit, *, reason, expected_previous):
        if type(limit) is not int or limit < 1 or not isinstance(reason, str) or not reason.strip():
            raise BridgeError('invalid_budget_authorization', '预算调整需要正整数上限和明确授权依据')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT request_limit FROM settings WHERE id=1').fetchone()[0]
            if previous != expected_previous:
                raise BridgeError('budget_authorization_conflict', '实际调用上限与待调整版本不同')
            used = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            if limit < used:
                raise BridgeError('budget_below_usage', '新上限不能低于已有调用次数')
            record = {'at': now(), 'previous_limit': previous, 'request_limit': limit,
                      'used_requests': used, 'reason': reason.strip()}
            db.execute('UPDATE settings SET request_limit=? WHERE id=1', (limit,))
            db.execute('INSERT INTO budget_authorizations(at,previous_limit,request_limit,used_requests,reason) VALUES (?,?,?,?,?)',
                       tuple(record[k] for k in ('at', 'previous_limit', 'request_limit', 'used_requests', 'reason')))
        write_json(self.root / 'summary.json', self.summary())
        return record

    def authorizations(self):
        with self.connect() as db:
            rows = db.execute('SELECT at,previous_limit,request_limit,used_requests,reason FROM budget_authorizations ORDER BY id').fetchall()
        return [dict(zip(('at', 'previous_limit', 'request_limit', 'used_requests', 'reason'), row)) for row in rows]

    def reserve(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            limit = db.execute('SELECT request_limit FROM settings WHERE id=1').fetchone()[0]
            if count >= limit:
                raise BridgeError("api_limit_reached", f"已达到本次授权的 {limit} 次调用上限")
            cursor = db.execute("INSERT INTO requests(started,status) VALUES (?,?)", (now(), "submitted"))
            return cursor.lastrowid

    def finish(self, request_id, result):
        with self.connect() as db:
            db.execute("UPDATE requests SET status=?,result=? WHERE id=?", (result["status"], json.dumps(result), request_id))
        write_json(self.root / "summary.json", self.summary())

    def mark_interrupted_before(self, cutoff, *, reason):
        """Retain paid reservations whose owning runtime is authoritatively gone."""
        with self.connect() as db:
            rows = db.execute("SELECT id FROM requests WHERE status='submitted' AND started < ?", (cutoff,)).fetchall()
            for (request_id,) in rows:
                result = {'status': 'response_unknown', 'reason': reason, 'observed_at': now(), 'cutoff': cutoff}
                db.execute('UPDATE requests SET status=?,result=? WHERE id=?', ('response_unknown', json.dumps(result), request_id))
        write_json(self.root / 'summary.json', self.summary())
        return len(rows)

    def summary(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,status,result FROM requests ORDER BY id").fetchall()
        usage = [json.loads(r[2]).get("usage", {}) for r in rows if r[2]]
        return {"limit": self.limit, "reserved_requests": len(rows), "remaining_requests": self.limit - len(rows),
                "http_successes": sum(r[1] == "http_200" for r in rows),
                "unknown_outcomes": sum(r[1] in {'submitted', 'response_unknown'} for r in rows),
                "reported_tokens": {k: sum(u.get(k, 0) or 0 for u in usage) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}}


@contextmanager
def gateway(directory, *, limit=None, connection=None, bind_host='127.0.0.1', advertised_host=None, port=0):
    base, key, model = connection or authorized_connection()
    ledger = RequestLedger(directory, limit)
    # Stable across a gateway restart for the same authorized provider and ledger;
    # no API key or reusable derived credential is written to disk.
    token = hmac.new(key.encode(), ('tracewaltz-gateway-v1:' + str(ledger.path.resolve())).encode(), hashlib.sha256).hexdigest()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def send(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).replace(key, "[redacted]").encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def do_GET(self):
            if self.path == "/bridge/usage":
                return self.send(200, {**ledger.summary(), "model": model, "endpoint": base})
            if self.path.endswith("models"):
                return self.send(200, {"object": "list", "data": [{"id": model, "object": "model"}]})
            self.send(404, {"error": "unknown gateway path"})
        def do_POST(self):
            if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                return self.send(401, {'error': 'gateway authorization required'})
            if self.path not in {"/chat/completions", "/v1/chat/completions"}:
                return self.send(404, {"error": "unsupported gateway path"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16 * 1024 * 1024:
                    return self.send(400, {"error": "invalid request size"})
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('request must be an object')
                request_id = ledger.reserve()
            except BridgeError as exc:
                return self.send(429, {"error": {"message": str(exc), "type": exc.code}})
            except (ValueError, TypeError):
                return self.send(400, {"error": "invalid JSON"})
            wants_stream = payload.get("stream") is True
            requested_model = payload.get('model')
            payload['model'] = model
            root = ledger.root / f"request-{request_id:04d}"
            root.mkdir()
            write_json(root / 'gateway-policy.json', {'requested_model': requested_model, 'actual_model': model,
                       'model_mapping': 'authorized-connection', 'generation_parameters': 'preserved'})
            write_json(root / "request.json", payload)
            started = time.monotonic()
            streamed = False
            try:
                url = base + ("/chat/completions" if base.endswith("/v1") else "/chat/completions")
                request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=600) as response:
                    if wants_stream:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/event-stream')
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        streamed = True
                        pieces = []
                        connected = True
                        while True:
                            piece = response.read1(65536)
                            if not piece:
                                break
                            pieces.append(piece)
                            if connected:
                                try:
                                    self.wfile.write(piece)
                                    self.wfile.flush()
                                except (BrokenPipeError, ConnectionResetError):
                                    connected = False
                        raw = b''.join(pieces)
                        self.close_connection = True
                    else:
                        raw = response.read()
                text = raw.decode("utf-8").replace(key, "[redacted]")
                if wants_stream:
                    (root / "response.sse").write_text(text, encoding="utf-8")
                    chunks = [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:") and line[5:].strip() not in {"", "[DONE]"}]
                    usage = next((c["usage"] for c in reversed(chunks) if c.get("usage")), {})
                else:
                    result = json.loads(text)
                    write_json(root / "response.json", result)
                    usage = result.get("usage", {})
                ledger.finish(request_id, {"status": "http_200", "usage": usage, "duration_ms": round((time.monotonic() - started) * 1000)})
                if streamed:
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if wants_stream else "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            except urllib.error.HTTPError as exc:
                ledger.finish(request_id, {"status": "http_" + str(exc.code), "duration_ms": round((time.monotonic() - started) * 1000)})
                if streamed:
                    self.close_connection = True
                else:
                    self.send(exc.code, {"error": {"message": "upstream HTTP error", "type": "upstream_error"}})
            except Exception as exc:
                ledger.finish(request_id, {"status": "response_unknown", "reason": type(exc).__name__})
                if streamed:
                    self.close_connection = True
                else:
                    self.send(502, {"error": {"message": "upstream outcome unknown", "type": "upstream_error"}})
    server = ThreadingHTTPServer((bind_host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://{advertised_host or bind_host}:{server.server_port}", "ledger": ledger, "model": model, 'token': token}
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()
        write_json(ledger.root / "summary.json", ledger.summary())
