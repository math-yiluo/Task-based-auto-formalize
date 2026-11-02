"""Operator requests carry the project's operator token, from a loopback host and origin."""
import os
import socket
import stat
import threading

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from formal_geyser.coordination import local_operator as module
from formal_geyser.coordination.api import create_app
from formal_geyser.coordination.config import initialize
from formal_geyser.coordination.models import ProjectConfig

pytestmark = pytest.mark.real_operator


class Request:
    def __init__(self, headers=None, cookies=None):
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.cookies = cookies or {}


def test_a_token_is_issued_once_readable_only_by_its_user(tmp_path):
    token = module.issue_token(tmp_path, "taskhub")
    path = module.token_path(tmp_path, "taskhub")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and len(token) >= 40
    assert module.issue_token(tmp_path, "taskhub") == token == module.operator_token(tmp_path, "taskhub")
    assert module.issue_token(tmp_path, "lean-service") != token  # Each service has its own.
    assert module.token_path(tmp_path / "other", "taskhub") != path  # And each project.
    assert not list(path.parent.glob(".*"))  # No temporary file is left behind.


@pytest.mark.parametrize("headers,cookies,accepted", [
    ({"Host": "127.0.0.1:8765", "Authorization": "Bearer right"}, {}, True),
    ({"Host": "localhost:8765"}, {module.COOKIE: "right"}, True),
    ({"Host": "[::1]:8765", "Origin": "http://[::1]:8765"}, {module.COOKIE: "right"}, True),
    ({"Host": "127.0.0.1:8765", "Authorization": "Bearer wrong"}, {}, False),
    ({"Host": "127.0.0.1:8765"}, {}, False),
    # A web page that resolved its own name to the loopback address (DNS rebinding).
    ({"Host": "attacker.example:8765", "Authorization": "Bearer right"}, {}, False),
    # A foreign page's request, even with the browser's cookie.
    ({"Host": "127.0.0.1:8765", "Origin": "http://attacker.example"}, {module.COOKIE: "right"}, False),
    ({"Host": "127.0.0.1:8765", "Origin": "null"}, {module.COOKIE: "right"}, False),
    ({}, {module.COOKIE: "right"}, False),
])
def test_requests_need_a_loopback_host_the_same_origin_and_the_token(headers, cookies, accepted):
    assert module.operator_request(Request(headers, cookies), "right") is accepted


def test_no_request_is_accepted_without_a_token():
    assert not module.operator_request(Request({"Host": "127.0.0.1", "Authorization": "Bearer "}), None)


def test_a_browser_signs_in_once_with_the_link_and_keeps_a_strict_cookie(tmp_path):
    initialize(tmp_path, ProjectConfig())
    with TestClient(create_app(tmp_path, resume_runs=False), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/").status_code == 401
        assert client.get("/v1/status").status_code == 401
        assert client.get("/login", params={"token": "wrong"}).status_code == 401
        token = module.operator_token(tmp_path, "taskhub")
        signed = client.get("/login", params={"token": token}, follow_redirects=False)
        assert signed.status_code == 303 and signed.headers["location"] == "/"
        cookie = signed.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert client.get("/").status_code == 200 and client.get("/v1/status").status_code == 200
        assert client.get("/dashboard.js").status_code == 200
        assert client.get("/v1/status", headers={"Origin": "http://attacker.example"}).status_code == 401


def test_served_api_accepts_a_local_client_with_the_token(tmp_path):
    initialize(tmp_path, ProjectConfig())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path, resume_runs=False), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30) as client:
            for _ in range(300):
                if server.started:
                    break
                threading.Event().wait(0.1)
            assert client.get("/v1/status").status_code == 401
            response = client.get("/v1/status", headers=module.bearer(tmp_path, "taskhub"))
            assert response.status_code == 200, response.text
    finally:
        server.should_exit = True
        thread.join(30)
        listener.close()


def test_tests_keep_their_tokens_out_of_the_users_runtime_directory():
    assert os.environ["FORMAL_GEYSER_RUNTIME_DIR"]
