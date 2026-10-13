"""Local credentials and a small, default-on gate for the trading API.

install_auth(app, data_dir=...) registers auth routes and guards every /api
route except status/login. Construction has no filesystem side effects;
initialize() or the first enabled request creates private credentials. Sessions
are process-local and expire after eight hours or a server restart.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


COOKIE_NAME = "quant_session"
PUBLIC_API_PATHS = {"/api/auth/status", "/api/auth/login"}


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


class AuthManager:
    def __init__(self, data_dir, enabled=True, allowed_origins=None,
                 secure_cookie=False, session_seconds=8*3600,
                 max_login_failures=5, login_window_seconds=60):
        self.enabled = enabled
        self.private_dir = Path(data_dir) / "private"
        self.credentials_path = self.private_dir / "api_credentials.json"
        self.config_path = self.private_dir / "api_auth.json"
        self.allowed_origins = {_origin(o) for o in (allowed_origins or [])}
        if None in self.allowed_origins:
            raise ValueError("Invalid allowed origin")
        self.secure_cookie = secure_cookie
        self.session_seconds = session_seconds
        self.max_login_failures, self.login_window_seconds = max_login_failures, login_window_seconds
        self._config = None
        self._sessions, self._failures = {}, {}
        self._lock = threading.RLock()

    @staticmethod
    def _private_write(path, value):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")

    def initialize(self):
        if not self.enabled:
            return
        with self._lock:
            if self._config is not None:
                return
            if self.private_dir.is_symlink():
                raise RuntimeError("Authentication directory cannot be a symlink")
            self.private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.private_dir, 0o700)
            for path in (self.credentials_path, self.config_path):
                if path.is_symlink():
                    raise RuntimeError("Authentication files cannot be symlinks")
                if path.exists():
                    os.chmod(path, 0o600)
            if not self.config_path.exists():
                if not self.credentials_path.exists():
                    credentials = dict(password=secrets.token_urlsafe(32), api_token=secrets.token_urlsafe(32))
                    try:
                        self._private_write(self.credentials_path, credentials)
                    except FileExistsError:
                        pass
                credentials = json.loads(self.credentials_path.read_text(encoding="utf-8"))
                if not all(isinstance(credentials.get(k), str) and len(credentials[k]) >= 16
                           for k in ("password", "api_token")):
                    raise RuntimeError("Invalid private API credentials")
                salt = secrets.token_hex(16)
                iterations = 200000
                config = dict(version=1, salt=salt, iterations=iterations,
                              password_hash=hashlib.pbkdf2_hmac("sha256", credentials["password"].encode(),
                                                             bytes.fromhex(salt), iterations).hex(),
                              api_token_hash=_digest(credentials["api_token"]))
                try:
                    self._private_write(self.config_path, config)
                except FileExistsError:
                    pass
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            try:
                if (config["version"] != 1 or not isinstance(config["iterations"], int)
                        or isinstance(config["iterations"], bool) or not 100000 <= config["iterations"] <= 1000000
                        or len(bytes.fromhex(config["salt"])) != 16
                        or len(bytes.fromhex(config["password_hash"])) != 32
                        or len(bytes.fromhex(config["api_token_hash"])) != 32):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise RuntimeError("Invalid authentication configuration") from None
            self._config = config

    def check_origin(self, request):
        origin_header = request.headers.get("origin")
        if origin_header:
            origin = _origin(origin_header)
            expected = _origin(str(request.base_url))
            if origin is None or (origin != expected and origin not in self.allowed_origins):
                raise HTTPException(403, "请求来源不被允许")
        elif request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(403, "不允许跨站请求")

    def authenticated(self, request):
        if not self.enabled:
            return True
        self.initialize()
        authorization = request.headers.get("authorization", "")
        bearer = authorization[7:] if authorization.lower().startswith("bearer ") else None
        token = bearer or request.cookies.get(COOKIE_NAME)
        if not token or len(token) > 512:
            return False
        digest = _digest(token)
        if bearer and hmac.compare_digest(digest, self._config["api_token_hash"]):
            return True
        with self._lock:
            expiry = self._sessions.get(digest)
            if expiry is not None and expiry > time.monotonic():
                return True
            self._sessions.pop(digest, None)
        return False

    def require(self, request):
        if not self.authenticated(request):
            raise HTTPException(401, "请先登录", headers={"WWW-Authenticate": "Bearer"})
        if self.enabled and request.method not in ("GET", "HEAD", "OPTIONS"):
            self.check_origin(request)

    def login(self, request, password=None, api_token=None):
        self.check_origin(request)
        self.initialize()
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with self._lock:
            failures = [t for t in self._failures.get(client, []) if now-t < self.login_window_seconds]
            self._failures[client] = failures
            if len(failures) >= self.max_login_failures:
                raise HTTPException(429, "登录尝试过多，请稍后重试", headers={"Retry-After": str(self.login_window_seconds)})
            valid = False
            if api_token:
                valid = hmac.compare_digest(_digest(api_token), self._config["api_token_hash"])
            elif password:
                actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(self._config["salt"]),
                                             self._config["iterations"]).hex()
                valid = hmac.compare_digest(actual, self._config["password_hash"])
            if not valid:
                failures.append(now)
                if len(self._failures) > 4096:
                    self._failures = {k: v for k, v in self._failures.items()
                                      if v and now-v[-1] < self.login_window_seconds}
                raise HTTPException(401, "访问密码或令牌错误")
            self._failures.pop(client, None)
            self._sessions = {k: v for k, v in self._sessions.items() if v > now}
            if len(self._sessions) >= 1024:
                self._sessions.pop(min(self._sessions, key=self._sessions.get))
            token = secrets.token_urlsafe(32)
            self._sessions[_digest(token)] = now+self.session_seconds
        return token

    def logout(self, request):
        authorization = request.headers.get("authorization", "")
        token = request.cookies.get(COOKIE_NAME)
        if authorization.lower().startswith("bearer "):
            token = authorization[7:]
        if token:
            with self._lock:
                self._sessions.pop(_digest(token), None)


class LoginRequest(BaseModel):
    password: str | None = Field(None, max_length=512)
    token: str | None = Field(None, max_length=512)
    issue_token: bool = False


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, manager):
        super().__init__(app)
        self.manager = manager

    async def dispatch(self, request, call_next):
        path = request.url.path.rstrip("/") or "/"
        if (path == "/api" or path.startswith("/api/")) and path not in PUBLIC_API_PATHS:
            try:
                self.manager.require(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        return await call_next(request)


def require_api_auth(request: Request):
    """Optional FastAPI Depends hook; install_auth's middleware already gates /api."""
    manager = getattr(request.app.state, "api_auth", None)
    if manager is None:
        raise HTTPException(503, "API认证未配置")
    manager.require(request)


def install_auth(app: FastAPI, data_dir=None, enabled=True, allowed_origins=None, secure_cookie=False, **kwargs):
    manager = AuthManager(data_dir or Path(__file__).resolve().parent / "data",
                          enabled=enabled, allowed_origins=allowed_origins,
                          secure_cookie=secure_cookie, **kwargs)
    app.state.api_auth = manager
    app.add_middleware(AuthMiddleware, manager=manager)

    @app.get("/api/auth/status")
    def auth_status(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return dict(auth_required=bool(manager.enabled), authenticated=manager.authenticated(request))

    @app.post("/api/auth/login")
    def auth_login(body: LoginRequest, request: Request, response: Response):
        if not manager.enabled:
            return dict(success=True, auth_required=False)
        token = manager.login(request, body.password, body.token)
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(COOKIE_NAME, token, max_age=manager.session_seconds,
                            httponly=True, samesite="strict", path="/",
                            secure=manager.secure_cookie or request.url.scheme == "https")
        result = dict(success=True, auth_required=True)
        if body.issue_token:
            result["token"] = token
        return result

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        manager.logout(request)
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict",
                               secure=manager.secure_cookie or request.url.scheme == "https")
        response.headers["Cache-Control"] = "no-store"
        return dict(success=True)

    return manager
