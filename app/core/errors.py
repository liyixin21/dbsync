"""
统一错误类型。

约定：所有业务异常继承 AppError，由 main.py 注册的处理器统一转成
    {"error": {"code": ..., "message": ..., "detail": ...}}
HTTPException 与 RequestValidationError 也走同一出口，保证前端只需处理一种形状。
"""
from typing import Any, Optional


class AppError(Exception):
    """所有业务异常的基类。"""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, *, detail: Optional[Any] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_payload(self) -> dict:
        payload: dict = {"code": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


# ---------------------------------------------------------------- 4xx 客户端错误

class BadRequestError(AppError):
    status_code = 400
    code = "BAD_REQUEST"


class ValidationError(BadRequestError):
    code = "VALIDATION_ERROR"


class AuthError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"


class TokenExpiredError(AuthError):
    code = "TOKEN_INVALID"


class ForbiddenError(AppError):
    status_code = 403
    code = "FORBIDDEN"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class RateLimitError(AppError):
    status_code = 429
    code = "RATE_LIMITED"


# ------------------------------------------------------- 领域内特化的客户端错误

class DatabaseInUseError(ConflictError):
    """数据库被同步任务或备份计划引用，不允许删除。"""
    code = "DATABASE_IN_USE"


class NameTakenError(ConflictError):
    """名称唯一性冲突。"""
    code = "NAME_TAKEN"


class CredentialError(BadRequestError):
    """凭据无法解密或不可用。"""
    code = "CREDENTIAL_INVALID"


class SyncConfigError(BadRequestError):
    """源库 binlog 配置不满足同步要求。"""
    code = "SYNC_CONFIG_INVALID"


class SyncCapacityError(ConflictError):
    """并发同步任务已达上限。"""
    code = "SYNC_CAPACITY_EXCEEDED"


# ---------------------------------------------------------------- 5xx 服务端错误

class ExternalServiceError(AppError):
    status_code = 502
    code = "EXTERNAL_SERVICE_ERROR"


class DatabaseConnectionError(ExternalServiceError):
    """MySQL 连接失败。"""
    code = "DATABASE_CONNECTION_FAILED"


class OperationFailedError(AppError):
    """备份/恢复/复制等子进程操作失败。"""
    status_code = 500
    code = "OPERATION_FAILED"
