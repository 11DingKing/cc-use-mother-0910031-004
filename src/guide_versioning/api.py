"""HTTP API（标准库 http.server，零第三方依赖）。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import DomainError
from .service import GuideService


def _json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ApiHandler(BaseHTTPRequestHandler):
    service: GuideService  # 由 server 注入

    server_version = "GuideVersioning/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    # ---- 公共处理 ----------------------------------------------------

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}", code="bad_json", status=400)
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象", code="bad_json", status=400)
        return value

    def _handle(self, fn: Callable[[], Any], status: int = 200) -> None:
        try:
            with self.service.store.lock:
                result = fn()
            _json_response(self, result if result is not None else {"ok": True}, status)
        except DomainError as exc:
            _json_response(self, exc.to_dict(), exc.status)
        except Exception as exc:  # noqa: BLE001 - 兜底，不泄露栈
            _json_response(self, {"error": {"code": "internal", "message": str(exc)}}, 500)

    def _q(self, name: str, default: str | None = None, required: bool = True) -> str | None:
        query = parse_qs(urlparse(self.path).query)
        values = query.get(name)
        if values:
            return values[0]
        if required:
            raise DomainError(f"缺少查询参数：{name}", code="missing_param", status=400)
        return default

    # ---- 路由 --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        with self.service.store.lock:
            self._route_get()

    def _route_get(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        svc = self.service

        if path == "/health":
            return self._handle(lambda: {"status": "ok"})
        if path == "/api/audiences":
            return self._handle(svc.list_audiences)
        if path == "/api/themes":
            return self._handle(svc.list_themes)
        if path == "/api/sources":
            return self._handle(svc.list_sources)
        if path == "/api/sessions":
            status_param = self._q("status", required=False)
            return self._handle(lambda: svc.list_sessions(status_param))

        m = re.fullmatch(r"/api/audiences/([^/]+)", path)
        if m:
            return self._handle(lambda: svc.get_audience(m.group(1)))
        m = re.fullmatch(r"/api/themes/([^/]+)", path)
        if m:
            return self._handle(lambda: svc.get_theme(m.group(1)))
        m = re.fullmatch(r"/api/sources/([^/]+)", path)
        if m:
            return self._handle(lambda: svc.get_source(m.group(1)))
        m = re.fullmatch(r"/api/fragments/([^/]+)", path)
        if m:
            return self._handle(lambda: svc.get_fragment(m.group(1)))
        if path == "/api/fragments":
            theme = self._q("theme"); audience = self._q("audience")
            return self._handle(lambda: svc.list_fragments(theme, audience))
        if path == "/api/outlines":
            theme = self._q("theme"); audience = self._q("audience")
            return self._handle(lambda: svc.get_outline(theme, audience))
        if path == "/api/releases":
            theme = self._q("theme"); audience = self._q("audience")
            return self._handle(lambda: svc.list_releases(theme, audience))

        m = re.fullmatch(r"/api/releases/(\d+)", path)
        if m:
            return self._handle(lambda: svc.get_release(int(m.group(1))))
        m = re.fullmatch(r"/api/releases/(\d+)/chain", path)
        if m:
            rid = int(m.group(1))
            rel = svc.get_release(rid)
            return self._handle(lambda: svc.release_chain(rel["theme_code"], rel["audience_code"]))
        m = re.fullmatch(r"/api/releases/(\d+)/affected", path)
        if m:
            return self._handle(lambda: svc.affected_sessions(int(m.group(1))))
        m = re.fullmatch(r"/api/releases/(\d+)/diff", path)
        if m:
            other = int(self._q("other"))
            return self._handle(lambda: svc.diff_releases(int(m.group(1)), other))

        m = re.fullmatch(r"/api/sessions/(\d+)", path)
        if m:
            return self._handle(lambda: svc.get_session(int(m.group(1))))

        _json_response(self, {"error": {"code": "not_found", "message": f"无此路由：{path}"}}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        svc = self.service
        body = self._read_json_safe()
        if isinstance(body, DomainError):
            return _json_response(self, body.to_dict(), body.status)

        def go() -> Any:
            if path == "/api/audiences":
                return svc.create_audience(
                    body["code"], body["name"], body.get("min_age"), body.get("max_age"))
            if path == "/api/themes":
                return svc.create_theme(body["code"], body["title"])
            if path == "/api/sources":
                return svc.create_source(
                    body["code"], body["title"],
                    **{k: v for k, v in body.items()
                       if k in {"author", "publisher", "uri", "locator", "year"}})
            if path == "/api/fragments":
                return svc.create_fragment(
                    body["code"], body["theme_code"], body["audience_code"], body["kind"],
                    body["title"], body["body"], body.get("source_refs"),
                    body.get("replaces_code"))
            if path == "/api/outlines":
                return svc.set_outline_entries(
                    body["theme_code"], body["audience_code"], body["fragment_codes"])
            if path == "/api/releases":
                return svc.publish(
                    body["theme_code"], body["audience_code"],
                    body.get("change_kind", "correction"), body.get("reason"),
                    major_bump=bool(body.get("major_bump", False)))
            if path == "/api/sessions":
                return svc.schedule_session(
                    body["theme_code"], body["audience_code"],
                    body["starts_at"], body.get("venue"))

            m = re.fullmatch(r"/api/fragments/([^/]+)/corrections", path)
            if m:
                return svc.correct_fragment(
                    m.group(1),
                    {k: v for k, v in body.items() if k in {"title", "body"}},
                    body.get("reason"))
            m = re.fullmatch(r"/api/releases/(\d+)/sign", path)
            if m:
                return svc.sign(
                    int(m.group(1)), body["role"], body["signer"],
                    body.get("comment"), body.get("expected_row_version"))
            m = re.fullmatch(r"/api/releases/(\d+)/withdraw", path)
            if m:
                return svc.withdraw(
                    int(m.group(1)), body.get("note", ""),
                    force=bool(body.get("force", False)))
            m = re.fullmatch(r"/api/sessions/(\d+)/start", path)
            if m:
                return svc.start_session(int(m.group(1)))
            m = re.fullmatch(r"/api/sessions/(\d+)/complete", path)
            if m:
                return svc.complete_session(int(m.group(1)))

            raise DomainError(f"无此路由：{path}", code="not_found", status=404)

        self._handle(go, 201 if path in {
            "/api/audiences", "/api/themes", "/api/sources", "/api/fragments",
            "/api/outlines", "/api/releases", "/api/sessions"} else 200)

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        body = self._read_json_safe()
        if isinstance(body, DomainError):
            return _json_response(self, body.to_dict(), body.status)
        if path == "/api/outlines":
            return self._handle(lambda: self.service.set_outline_entries(
                body["theme_code"], body["audience_code"], body["fragment_codes"]))
        _json_response(self, {"error": {"code": "not_found", "message": f"无此路由：{path}"}}, 404)

    def _read_json_safe(self) -> dict | DomainError:
        try:
            return self._read_json()
        except DomainError as exc:
            return exc


def build_server(host: str, port: int, service: GuideService) -> ThreadingHTTPServer:
    handler = ApiHandler
    handler.service = service

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return _Server((host, port), handler)
