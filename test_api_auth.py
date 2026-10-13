"""Authentication regressions using isolated credentials and no providers."""
import hashlib
import json
import stat
import time

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from api_auth import AuthManager, COOKIE_NAME, install_auth, require_api_auth


@pytest.fixture
def auth_app(tmp_path):
    app = FastAPI()
    manager = install_auth(app, data_dir=tmp_path)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/account")
    @app.post("/api/account")
    def account():
        return {"cash": 100}

    with TestClient(app) as client:
        yield manager, client


def credentials(manager):
    manager.initialize()
    return json.loads(manager.credentials_path.read_text())


def test_default_gate_is_lazy_private_and_does_not_expose_credentials(auth_app):
    manager, client = auth_app
    assert manager.enabled and not manager.private_dir.exists()
    assert client.get("/health").json() == {"status": "ok"}
    assert not manager.private_dir.exists()
    status = client.get("/api/auth/status")
    assert status.status_code == 200
    assert status.json() == {"auth_required": True, "authenticated": False}
    values = credentials(manager)
    assert values["password"] != values["api_token"]
    assert len(values["password"]) >= 32
    assert stat.S_IMODE(manager.private_dir.stat().st_mode) == 0o700
    for path in (manager.credentials_path, manager.config_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    config = manager.config_path.read_text()
    assert values["password"] not in config and values["api_token"] not in config
    assert values["password"] not in status.text and values["api_token"] not in status.text
    assert client.get("/api/account").status_code == 401
    assert client.post("/api/account").status_code == 401
    assert client.get("/api/snapshot/unknown").status_code == 401


def test_login_cookie_origin_and_logout(auth_app):
    manager, client = auth_app
    login = client.post("/api/auth/login", json={"password": credentials(manager)["password"]})
    assert login.status_code == 200 and login.json()["success"]
    assert "token" not in login.json()
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/auth/status").json()["authenticated"]
    assert client.get("/api/account").status_code == 200
    assert client.post("/api/account", headers={"origin": "http://testserver"}).status_code == 200
    assert client.post("/api/account", headers={"origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/account", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/account").status_code == 401


def test_cross_origin_login_does_not_create_session(auth_app):
    manager, client = auth_app
    result = client.post("/api/auth/login", json={"password": credentials(manager)["password"]},
                         headers={"origin": "null"})
    assert result.status_code == 403 and not manager._sessions
    assert COOKIE_NAME not in client.cookies


def test_bearer_and_expiring_issued_session(auth_app):
    manager, client = auth_app
    values = credentials(manager)
    assert client.get("/api/account", headers={"authorization": "Bearer "+values["api_token"]}).status_code == 200
    login = client.post("/api/auth/login", json={"password": values["password"], "issue_token": True})
    token = login.json()["token"]
    client.cookies.clear()
    headers = {"authorization": "Bearer "+token}
    assert client.get("/api/account", headers=headers).status_code == 200
    manager._sessions[hashlib.sha256(token.encode()).hexdigest()] = 0
    assert client.get("/api/account", headers=headers).status_code == 401
    assert client.get("/api/account", headers={"authorization": "Bearer wrong"}).status_code == 401


def test_api_token_can_log_in_without_supplying_password(auth_app):
    manager, client = auth_app
    result = client.post("/api/auth/login", json={"token": credentials(manager)["api_token"]})
    assert result.status_code == 200 and client.get("/api/account").status_code == 200


def test_repeated_bad_passwords_are_limited_and_window_recovers(auth_app):
    manager, client = auth_app
    values = credentials(manager)
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    blocked = client.post("/api/auth/login", json={"password": values["password"]})
    assert blocked.status_code == 429 and blocked.headers["retry-after"]
    manager._failures = {key: [time.monotonic()-120] for key in manager._failures}
    assert client.post("/api/auth/login", json={"password": values["password"]}).status_code == 200


def test_restart_reuses_password_hash_and_does_not_reuse_sessions(auth_app):
    manager, client = auth_app
    values = credentials(manager)
    previous = manager.config_path.read_bytes()
    restored = AuthManager(manager.private_dir.parent)
    restored.initialize()
    assert restored.config_path.read_bytes() == previous
    assert json.loads(restored.credentials_path.read_text()) == values
    assert restored._sessions == {}


def test_explicit_test_disable_does_not_touch_private_files(tmp_path):
    app = FastAPI()
    manager = install_auth(app, data_dir=tmp_path, enabled=False)
    @app.post("/api/account")
    def account():
        return {"success": True}
    with TestClient(app) as client:
        assert client.get("/api/auth/status").json() == {"auth_required": False, "authenticated": True}
        assert client.post("/api/account").status_code == 200
    assert not manager.private_dir.exists()


def test_dependency_fails_closed_when_auth_was_not_installed():
    app = FastAPI()
    @app.get("/protected", dependencies=[Depends(require_api_auth)])
    def protected():
        return {"success": True}
    with TestClient(app) as client:
        assert client.get("/protected").status_code == 503


def test_https_cookies_are_secure(tmp_path):
    app = FastAPI()
    manager = install_auth(app, data_dir=tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        result = client.post("/api/auth/login", json={"password": credentials(manager)["password"]})
        assert "secure" in result.headers["set-cookie"].lower()


def test_private_credential_symlink_is_rejected_without_overwriting_target(tmp_path):
    manager = AuthManager(tmp_path)
    manager.private_dir.mkdir()
    target = tmp_path/"unrelated.json"
    target.write_text("original")
    manager.credentials_path.symlink_to(target)
    with pytest.raises(RuntimeError, match="symlinks"):
        manager.initialize()
    assert target.read_text() == "original"
