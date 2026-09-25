"""
OpenList 客户端。

用于把备份文件上传到 OpenList 的指定目录。

API 约定（见 https://openlist.apifox.cn/）：
  认证  POST /api/auth/login   body {"username","password"}  → data.token（默认 48 小时有效）
  上传  PUT  /api/fs/put       Header: Authorization, File-Path(URL 编码)
                               body 为 application/octet-stream 原始字节流

token 缓存在进程内，快过期时自动重新登录，避免每次上传都登录一遍。
"""
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlparse

import httpx
from loguru import logger

# 上传大文件时不给整体超时，只限制连接与读取间隔
_CONNECT_TIMEOUT = 15.0
_WRITE_TIMEOUT = 300.0
_READ_TIMEOUT = 120.0

# token 默认 48 小时，提前 30 分钟视为过期以防边界情况
_TOKEN_SAFETY_MARGIN = 1800
_TOKEN_TTL = 48 * 3600


def _httpx_request(method: str, url: str, *, verify: bool = True, **kwargs) -> httpx.Response:
    """
    发起请求。

    用显式构造的 Client 而不是模块级 httpx.post/put：
    后者会读取环境变量并触发 NO_PROXY 解析缺陷。

    verify 属于 Client 构造参数而非 request() 参数，
    因此在这里单独取出传给构造函数。
    """
    with httpx.Client(trust_env=False, verify=verify) as client:
        return client.request(method, url, **kwargs)


class OpenListError(Exception):
    """OpenList 交互失败。消息面向使用者，不含堆栈细节。"""


@dataclass
class OpenListConfig:
    """连接配置。"""

    base_url: str
    username: str
    password: str
    remote_dir: str = "/"
    verify_ssl: bool = True

    def normalized_base(self) -> str:
        """
        规范化服务地址：去掉结尾斜杠，但保留协议中的 `//`。

        直接 rstrip("/") 会把 "http://" 削成 "http:"，
        继而报出「必须以 http:// 开头」这种自相矛盾的提示。
        """
        raw = (self.base_url or "").strip()
        if not raw:
            return ""
        # 仅当斜杠位于路径部分时才去掉
        scheme_sep = raw.find("://")
        if scheme_sep == -1:
            return raw.rstrip("/")
        head = raw[: scheme_sep + 3]
        tail = raw[scheme_sep + 3 :]
        if not tail:
            return head
        return head + tail.rstrip("/")

    def normalized_dir(self) -> str:
        """规范化远程目录：统一以 / 开头、不以 / 结尾。"""
        raw = (self.remote_dir or "/").strip()
        if not raw.startswith("/"):
            raw = "/" + raw
        if len(raw) > 1 and raw.endswith("/"):
            raw = raw.rstrip("/")
        return raw


@dataclass
class UploadResult:
    """单次上传结果。"""

    success: bool
    remote_path: str = ""
    message: str = ""


class _TokenCache:
    """进程内 token 缓存。按 (base_url, username) 区分。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}

    def get(self, key: tuple[str, str]) -> Optional[str]:
        with self._lock:
            entry = self._tokens.get(key)
            if entry is None:
                return None
            token, expires_at = entry
            if time.time() >= expires_at:
                del self._tokens[key]
                return None
            return token

    def set(self, key: tuple[str, str], token: str, ttl: int = _TOKEN_TTL) -> None:
        with self._lock:
            self._tokens[key] = (token, time.time() + max(60, ttl - _TOKEN_SAFETY_MARGIN))

    def invalidate(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._tokens.pop(key, None)


_token_cache = _TokenCache()


class OpenListClient:
    """OpenList API 客户端。"""

    def __init__(self, config: OpenListConfig) -> None:
        self.config = config
        self._key = (config.normalized_base(), config.username)

    # ------------------------------------------------------------ 认证

    def _login(self) -> str:
        base = self.config.normalized_base()
        if not base:
            raise OpenListError("未配置 OpenList 地址")

        # 地址格式校验：给出可操作的提示，而不是让底层抛出
        # 「Invalid port: ':1]'」这类无从下手的解析错误。
        if not base.startswith(("http://", "https://")):
            raise OpenListError(
                f"服务地址必须以 http:// 或 https:// 开头，当前为「{base}」"
            )

        parsed = urlparse(base)
        if not parsed.hostname:
            raise OpenListError(f"服务地址格式不正确，无法解析主机名：「{base}」")
        try:
            # 访问一次以触发非法端口的解析错误
            _ = parsed.port
        except ValueError:
            raise OpenListError(f"服务地址中的端口号不合法：「{base}」") from None

        if not self.config.username or not self.config.password:
            raise OpenListError("未配置 OpenList 用户名或密码")

        try:
            response = _httpx_request(
                "POST",
                f"{base}/api/auth/login",
                json={"username": self.config.username, "password": self.config.password},
                timeout=httpx.Timeout(15.0),
                verify=self.config.verify_ssl,
                follow_redirects=True,
            )
        except httpx.ConnectError as exc:
            raise OpenListError(
                f"无法连接到 {base}，请确认地址与端口正确且服务正在运行（{exc}）"
            ) from exc
        except httpx.RequestError as exc:
            raise OpenListError(f"访问 {base} 失败：{exc}") from exc
        except ValueError as exc:
            # httpx 在解析畸形地址（如 IPv6 方括号用法不当）时抛 ValueError，
            # 它不是 RequestError 的子类，不单独捕获会穿透成 500。
            raise OpenListError(
                f"服务地址无法解析：「{base}」。"
                "请填写形如 http://主机:端口 的地址"
            ) from exc

        if response.status_code != 200:
            raise OpenListError(
                f"OpenList 登录失败：HTTP {response.status_code} {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenListError("OpenList 登录返回的不是 JSON，请确认地址指向 OpenList 服务") from exc

        if payload.get("code") != 200:
            message = payload.get("message") or "未知错误"
            raise OpenListError(f"OpenList 登录被拒绝：{message}")

        token = (payload.get("data") or {}).get("token")
        if not token:
            raise OpenListError("OpenList 登录响应缺少 token 字段")

        _token_cache.set(self._key, token)
        logger.info(f"OpenList 登录成功：{base} ({self.config.username})")
        return token

    def _token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = _token_cache.get(self._key)
            if cached:
                return cached
        return self._login()

    def _auth_headers(self) -> dict:
        return {"Authorization": self._token()}

    # ------------------------------------------------------------ 目录

    def ensure_directory(self, path: Optional[str] = None) -> None:
        """确保远程目录存在（逐级创建，已存在则忽略）。"""
        target = (path or self.config.normalized_dir()).strip()
        if not target or target == "/":
            return

        base = self.config.normalized_base()
        segments = [s for s in target.split("/") if s]
        current = ""
        for segment in segments:
            current = f"{current}/{segment}"
            try:
                response = _httpx_request(
                    "POST",
                    f"{base}/api/fs/mkdir",
                    headers={"Authorization": self._token(), "Content-Type": "application/json"},
                    json={"path": current},
                    timeout=httpx.Timeout(15.0),
                    verify=self.config.verify_ssl,
                    follow_redirects=True,
                )
            except httpx.ConnectError as exc:
                raise OpenListError(
                    f"创建目录 {current} 时无法连接 {base}，请确认 OpenList 服务可用"
                ) from exc
            except httpx.RequestError as exc:
                raise OpenListError(f"创建目录失败（{current}）：{exc}") from exc
            except ValueError as exc:
                raise OpenListError(f"服务地址无法解析，无法创建目录：{exc}") from exc

            if response.status_code == 401:
                # token 失效，刷新后重试一次
                response = _httpx_request(
                    "POST",
                    f"{base}/api/fs/mkdir",
                    headers={
                        "Authorization": self._token(force_refresh=True),
                        "Content-Type": "application/json",
                    },
                    json={"path": current},
                    timeout=httpx.Timeout(15.0),
                    verify=self.config.verify_ssl,
                    follow_redirects=True,
                )

            if response.status_code != 200:
                raise OpenListError(
                    f"创建目录失败（{current}）：HTTP {response.status_code}"
                )

            payload = response.json()
            # 目录已存在时 OpenList 返回非 200 的 code，属于可接受情况
            if payload.get("code") != 200 and "exist" not in str(payload.get("message", "")).lower():
                raise OpenListError(
                    f"创建目录失败（{current}）：{payload.get('message') or payload.get('code')}"
                )

    # ------------------------------------------------------------ 上传

    def upload_file(
        self,
        local_path: str,
        remote_dir: Optional[str] = None,
        remote_name: Optional[str] = None,
    ) -> UploadResult:
        """
        上传单个文件。

        采用流式读取，避免把大文件整体载入内存。
        """
        if not os.path.isfile(local_path):
            raise OpenListError(f"本地文件不存在：{local_path}")

        directory = (remote_dir or self.config.normalized_dir()).strip() or "/"
        name = remote_name or os.path.basename(local_path)
        remote_path = f"{directory.rstrip('/')}/{name}" if directory != "/" else f"/{name}"

        self.ensure_directory(directory)

        size = os.path.getsize(local_path)
        base = self.config.normalized_base()

        def _put(token: str) -> httpx.Response:
            with open(local_path, "rb") as fh:
                return _httpx_request(
                    "PUT",
                    f"{base}/api/fs/put",
                    headers={
                        "Authorization": token,
                        # File-Path 需要 URL 编码；httpx 不会自动编码 header 值
                        "File-Path": quote(remote_path),
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(size),
                    },
                    content=fh,
                    timeout=httpx.Timeout(
                        connect=_CONNECT_TIMEOUT,
                        read=_READ_TIMEOUT,
                        write=_WRITE_TIMEOUT,
                        pool=_CONNECT_TIMEOUT,
                    ),
                    verify=self.config.verify_ssl,
                    follow_redirects=True,
                )

        try:
            response = _put(self._token())
            if response.status_code == 401:
                response = _put(self._token(force_refresh=True))
        except httpx.ConnectError as exc:
            raise OpenListError(
                f"上传时无法连接 {base}，请确认 OpenList 服务可用（{exc}）"
            ) from exc
        except httpx.RequestError as exc:
            raise OpenListError(f"上传请求失败：{exc}") from exc
        except ValueError as exc:
            raise OpenListError(f"服务地址无法解析，无法上传：{exc}") from exc

        if response.status_code != 200:
            raise OpenListError(
                f"上传失败：HTTP {response.status_code} {response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        code = payload.get("code")
        if code != 200:
            raise OpenListError(f"上传被拒绝：{payload.get('message') or code}")

        logger.info(f"OpenList 上传完成：{remote_path}（{size} 字节）")
        return UploadResult(success=True, remote_path=remote_path, message="上传成功")

    # ------------------------------------------------------------ 连通性

    def test_connection(self) -> dict:
        """
        连接自检：登录 → 确认目录可访问/可创建。

        返回结构化结果，便于接口直接把失败原因透出给用户。
        """
        info: dict = {"logged_in": False, "directory": None, "writable": False}

        self._login()
        info["logged_in"] = True

        directory = self.config.normalized_dir()
        info["directory"] = directory

        if directory != "/":
            self.ensure_directory(directory)
        info["writable"] = True

        return info
