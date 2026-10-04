"""领域错误类型，HTTP 层据此映射状态码。"""
from __future__ import annotations


class DomainError(Exception):
    """领域错误基类。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    """状态冲突：重复签署、修订非生效片段、重复发布等。"""

    status = 409
    code = "conflict"


class ValidationError(DomainError):
    """输入或前置条件不满足。"""

    status = 422
    code = "validation"
