"""讲解内容版本发布的 HTTP API（仅标准库）。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import DomainError
from .service import NarrationService


def _query(query: dict, name: str, default=None):
    values = query.get(name)
    return values[0] if values else default


def build_routes(service: NarrationService) -> list:
    """路由表：handler 返回 (status, payload)。"""
    routes = []

    def route(method: str, pattern: str):
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

        def decorator(fn):
            routes.append((method, regex, fn))
            return fn

        return decorator

    @route("GET", "/api/health")
    def health(params, query, body):
        return 200, {"status": "ok"}

    # ---- 主题 / 受众 / 来源 ----
    @route("POST", "/api/themes")
    def create_theme(params, query, body):
        return 201, service.create_theme(
            name=body.get("name"), description=body.get("description", ""), actor=body.get("actor", "system")
        )

    @route("GET", "/api/themes")
    def list_themes(params, query, body):
        return 200, {"themes": service.list_themes()}

    @route("POST", "/api/audiences")
    def create_audience(params, query, body):
        return 201, service.create_audience(
            code=body.get("code"), name=body.get("name"),
            min_age=body.get("min_age"), max_age=body.get("max_age"),
            actor=body.get("actor", "system"),
        )

    @route("GET", "/api/audiences")
    def list_audiences(params, query, body):
        return 200, {"audiences": service.list_audiences()}

    @route("POST", "/api/sources")
    def create_source(params, query, body):
        return 201, service.create_source(
            title=body.get("title"), publisher=body.get("publisher", ""),
            url=body.get("url", ""), accessed_at=body.get("accessed_at"),
            actor=body.get("actor", "system"),
        )

    @route("GET", "/api/sources")
    def list_sources(params, query, body):
        return 200, {"sources": service.list_sources()}

    # ---- 内容片段 ----
    @route("POST", "/api/fragments")
    def create_fragment(params, query, body):
        return 201, service.create_fragment(
            theme_id=body.get("theme_id"), audience_code=body.get("audience_code"),
            title=body.get("title"), body=body.get("body"),
            source_ids=body.get("source_ids") or (), actor=body.get("actor", "system"),
        )

    @route("GET", "/api/fragments")
    def list_fragments(params, query, body):
        return 200, {"fragments": service.list_fragments(
            theme_id=_query(query, "theme_id"),
            audience_code=_query(query, "audience_code"),
            status=_query(query, "status"),
        )}

    @route("GET", "/api/fragments/{fragment_id}")
    def get_fragment(params, query, body):
        return 200, service.get_fragment(params["fragment_id"])

    @route("POST", "/api/fragments/{fragment_id}/revise")
    def revise_fragment(params, query, body):
        return 201, service.revise_fragment(
            params["fragment_id"], mode=body.get("mode"), actor=body.get("actor", "system"),
            title=body.get("title"), body=body.get("body"),
            source_ids=body.get("source_ids"), note=body.get("note"),
        )

    @route("POST", "/api/fragments/{fragment_id}/withdraw")
    def withdraw_fragment(params, query, body):
        return 200, service.withdraw_fragment(
            params["fragment_id"], reason=body.get("reason"), actor=body.get("actor", "system")
        )

    # ---- 发布草稿与签署 ----
    @route("POST", "/api/drafts")
    def create_draft(params, query, body):
        return 201, service.create_draft(
            theme_id=body.get("theme_id"), audience_code=body.get("audience_code"),
            fragment_ids=body.get("fragment_ids") or (), actor=body.get("actor", "system"),
        )

    @route("GET", "/api/drafts/{draft_id}")
    def get_draft(params, query, body):
        return 200, service.get_draft(params["draft_id"])

    @route("POST", "/api/drafts/{draft_id}/items")
    def replace_draft_items(params, query, body):
        return 200, service.replace_draft_items(
            params["draft_id"], fragment_ids=body.get("fragment_ids") or (),
            actor=body.get("actor", "system"),
        )

    @route("POST", "/api/drafts/{draft_id}/signoffs")
    def sign_draft(params, query, body):
        return 200, service.sign_draft(
            params["draft_id"], role=body.get("role"), signer=body.get("signer")
        )

    @route("POST", "/api/drafts/{draft_id}/publish")
    def publish_draft(params, query, body):
        return 201, service.publish_draft(params["draft_id"], actor=body.get("actor", "system"))

    @route("POST", "/api/drafts/{draft_id}/abandon")
    def abandon_draft(params, query, body):
        return 200, service.abandon_draft(params["draft_id"], actor=body.get("actor", "system"))

    # ---- 版本 ----
    @route("GET", "/api/versions")
    def list_versions(params, query, body):
        return 200, {"versions": service.list_versions(
            theme_id=_query(query, "theme_id"), audience_code=_query(query, "audience_code")
        )}

    @route("GET", "/api/versions/{version_id}")
    def get_version(params, query, body):
        return 200, service.get_version(params["version_id"])

    @route("POST", "/api/versions/{version_id}/withdraw")
    def withdraw_version(params, query, body):
        return 200, service.withdraw_version(
            params["version_id"], reason=body.get("reason"), actor=body.get("actor", "system")
        )

    @route("GET", "/api/versions/{version_id}/impact")
    def version_impact(params, query, body):
        return 200, service.version_impact(params["version_id"])

    @route("GET", "/api/diff")
    def diff(params, query, body):
        from_id = _query(query, "from")
        to_id = _query(query, "to")
        if not from_id or not to_id:
            return 422, {"error": {"code": "validation", "message": "diff 需要 from 与 to 两个版本编号"}}
        return 200, service.diff_versions(from_id, to_id)

    # ---- 场次 ----
    @route("POST", "/api/sessions")
    def create_session(params, query, body):
        return 201, service.create_session(
            theme_id=body.get("theme_id"), audience_code=body.get("audience_code"),
            title=body.get("title"), scheduled_start=body.get("scheduled_start"),
            pin_version_id=body.get("pin_version_id"), actor=body.get("actor", "system"),
        )

    @route("GET", "/api/sessions")
    def list_sessions(params, query, body):
        return 200, {"sessions": service.list_sessions(
            status=_query(query, "status"),
            theme_id=_query(query, "theme_id"),
            audience_code=_query(query, "audience_code"),
        )}

    @route("GET", "/api/sessions/{session_id}")
    def get_session(params, query, body):
        return 200, service.get_session(params["session_id"])

    @route("POST", "/api/sessions/{session_id}/complete")
    def complete_session(params, query, body):
        return 200, service.complete_session(
            params["session_id"], actor=body.get("actor", "system"),
            version_id=body.get("version_id"), at=body.get("at"),
        )

    @route("POST", "/api/sessions/{session_id}/cancel")
    def cancel_session(params, query, body):
        return 200, service.cancel_session(params["session_id"], actor=body.get("actor", "system"))

    # ---- 审计 ----
    @route("GET", "/api/audit")
    def audit(params, query, body):
        limit = _query(query, "limit")
        return 200, {"events": service.audit_log(limit=int(limit) if limit else 100)}

    return routes


class _Handler(BaseHTTPRequestHandler):
    server_version = "NarrationVersioning/0.1"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainError("请求体不是合法 JSON", code="bad_json", status=400)
        if not isinstance(body, dict):
            raise DomainError("请求体必须是 JSON 对象", code="bad_json", status=400)
        return body

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            body = self._read_body() if method == "POST" else {}
            for route_method, pattern, handler in self.server.routes:
                if route_method != method:
                    continue
                match = pattern.match(parsed.path)
                if not match:
                    continue
                status, payload = handler(match.groupdict(), query, body)
                self._respond(status, payload)
                return
            self._respond(404, {"error": {"code": "not_found", "message": "接口不存在"}})
        except DomainError as exc:
            self._respond(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
            self.log_error("未处理异常：%r", exc)
            self._respond(500, {"error": {"code": "internal", "message": "服务器内部错误"}})

    def _respond(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 保持安静，错误走 log_error
        return


def make_server(service: NarrationService, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.routes = build_routes(service)
    return server
