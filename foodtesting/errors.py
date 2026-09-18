"""统一的接口错误类型。"""

from __future__ import annotations


class ApiError(Exception):
    """携带 HTTP 状态码与业务错误码的异常，由接口层转换为 JSON 响应。"""

    def __init__(self, status: int, message: str, code: str = "error"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
