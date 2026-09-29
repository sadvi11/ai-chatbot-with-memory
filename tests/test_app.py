"""Tests for the chatbot service.

The first test here is the one that matters most. This module previously
raised ImportError on line 3 (`HTTPAuthCredentials` does not exist in
fastapi.security), so the application could not start and no endpoint ever
served a request -- and nothing in the repository would have told you. A test
that does nothing but import the module would have caught it on the first CI
run.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError


def test_module_imports():
    """The app must be importable. Guards against the ImportError class of bug."""
    import main
    assert main.app is not None


def test_all_routes_register():
    import main
    paths = {r.path for r in main.app.routes if hasattr(r, "path")}
    for expected in ("/health", "/chat", "/conversation/{user_id}", "/metrics/{user_id}"):
        assert expected in paths, f"route {expected} missing"


def test_model_is_not_a_retired_id():
    """claude-3-5-sonnet-20241022 was retired 2025-10-28 and returns 404.

    Pinning a retired model is silent: the code looks fine and every request
    fails at runtime. Fail the build instead.
    """
    import main
    retired = {
        "claude-3-5-sonnet-20241022",
        "claude-3-5-sonnet-20240620",
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-5-haiku-20241022",
        "claude-3-7-sonnet-20250219",
    }
    assert main.MODEL not in retired, f"{main.MODEL} is a retired model and will 404"


def test_user_id_pattern_rejects_bad_input():
    """Field(pattern=...) — Pydantic v2 spelling. `regex=` raises on v2."""
    import main
    with pytest.raises(ValidationError):
        main.ChatRequest(message="hi", user_id="bad id!")


def test_blank_message_rejected():
    """@field_validator — Pydantic v2 decorator. @validator is v1."""
    import main
    with pytest.raises(ValidationError):
        main.ChatRequest(message="   ", user_id="alice")


def test_valid_request_is_accepted_and_stripped():
    import main
    req = main.ChatRequest(message="  hello  ", user_id="alice_1")
    assert req.message == "hello"


def test_chat_requires_authentication():
    """Auth must run before anything else touches the request."""
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as client:
        assert client.post("/chat", json={"message": "hi", "user_id": "alice"}).status_code == 401


# --- authentication -------------------------------------------------------
# This service published a default SECRET_KEY, so anyone reading the repository
# could sign a token for any user_id and read that user's history. These tests
# hold the two properties that fix it: no key means no service, and tokens die.

def _credentials(token: str):
    from fastapi.security import HTTPAuthorizationCredentials
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_startup_fails_without_a_secret_key():
    """No SECRET_KEY must mean no service, rather than a forgeable one."""
    env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
    env["AWS_REGION"] = "us-east-1"
    result = subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0, "importing without SECRET_KEY must fail"
    assert "SECRET_KEY is not set" in result.stderr


def test_a_freshly_issued_token_is_accepted():
    import main
    assert main.verify_token(_credentials(main.make_token("alice"))) == "alice"


def test_an_expired_token_is_rejected():
    import main
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:
        main.verify_token(_credentials(main.make_token("alice", ttl_seconds=-1)))
    assert caught.value.status_code == 401
    assert "expired" in caught.value.detail.lower()


def test_extending_the_expiry_invalidates_the_signature():
    """The expiry is inside the signed message, so it cannot be edited."""
    import main
    from fastapi import HTTPException
    user_id, expires_at, signature = main.make_token("alice", ttl_seconds=-1).split(":")
    forged = f"{user_id}:{int(expires_at) + 10_000}:{signature}"
    with pytest.raises(HTTPException) as caught:
        main.verify_token(_credentials(forged))
    assert caught.value.status_code == 401


def test_a_token_signed_with_the_old_default_key_is_rejected():
    """The exact attack the published default allowed."""
    import hashlib
    import hmac
    import main
    from fastapi import HTTPException
    message = f"mallory:{int(time.time()) + 3600}"
    signature = hmac.new(b"your-secret-key-change-in-production",
                         message.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException) as caught:
        main.verify_token(_credentials(f"{message}:{signature}"))
    assert caught.value.status_code == 401
