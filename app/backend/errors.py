from __future__ import annotations


class DomainError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class NotFoundError(DomainError):
    def __init__(self, message: str = "资源不存在", details: object | None = None) -> None:
        super().__init__(404, "not_found", message, details)


class UnauthorizedError(DomainError):
    def __init__(self, message: str = "未登录或会话已失效", details: object | None = None) -> None:
        super().__init__(401, "unauthorized", message, details)


class ForbiddenError(DomainError):
    def __init__(self, message: str = "没有执行该操作的权限", details: object | None = None) -> None:
        super().__init__(403, "forbidden", message, details)


class ConflictError(DomainError):
    def __init__(self, message: str, details: object | None = None) -> None:
        super().__init__(409, "conflict", message, details)


class ValidationError(DomainError):
    def __init__(self, message: str, details: object | None = None) -> None:
        super().__init__(422, "validation_error", message, details)


class RateLimitError(DomainError):
    def __init__(self, message: str = "请求过于频繁，请稍后重试") -> None:
        super().__init__(429, "rate_limited", message)