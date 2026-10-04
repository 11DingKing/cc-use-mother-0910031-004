"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期的业务错误基类。"""

    status = 400
    code = "invalid_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


class RuleError(DomainError):
    """请求本身合法，但按领域规则无法处理（如已排场次无可用版本）。"""

    status = 422
    code = "rule_violation"
