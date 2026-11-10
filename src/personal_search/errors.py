from __future__ import annotations


class GatewayError(Exception):
    """An intentionally credential-free, user-facing failure."""

    def __init__(self, code: str, message: str, **details):
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


def safe_error(exc: Exception) -> dict:
    if isinstance(exc, GatewayError):
        return exc.as_dict()
    # Network/library exceptions can contain URLs, cookies or filesystem contents.
    return {
        "code": "INTERNAL_ERROR",
        "message": "内部错误；请检查本地诊断。",
        "type": type(exc).__name__,
    }
