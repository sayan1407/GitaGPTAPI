import time
import uuid
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from main import app, chat_limiter
from auth import create_user_access_token, verify_user_access_token
import database
import jwt


@pytest.fixture(autouse=True)
def setup_db():
    database.init_db()


def test_vuln_01_unauthenticated_request_rejected():
    client = TestClient(app)
    # Attempting to access sessions without Bearer token must return 401 even if user_email is sent
    res = client.get("/sessions?user_email=victim@example.com")
    assert res.status_code == 401
    assert "Authentication required" in res.json()["detail"]

    # Attempting to create session without token must return 401
    res = client.post("/sessions", json={"user_email": "victim@example.com", "session": {"id": "s1", "title": "t"}})
    assert res.status_code == 401

    # Attempting to delete session without token must return 401
    res = client.delete("/sessions/s1?user_email=victim@example.com")
    assert res.status_code == 401


def test_vuln_02_cross_user_session_tampering_prevented():
    client = TestClient(app)
    uid_a = uuid.uuid4().hex[:8]
    uid_b = uuid.uuid4().hex[:8]
    email_a = f"victim_{uid_a}@example.com"
    email_b = f"attacker_{uid_b}@example.com"
    token_a = create_user_access_token(email_a, f"Victim {uid_a}")
    token_b = create_user_access_token(email_b, f"Attacker {uid_b}")

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    session_id = f"victim_sess_{uid_a}"

    # 1. Victim creates a legitimate session
    res = client.put(
        f"/sessions/{session_id}",
        headers=headers_a,
        json={"session": {"id": session_id, "title": "Victim's Private Chat"}},
    )
    assert res.status_code == 200

    # 2. Attacker attempts to overwrite Victim's session -> must be 403 Forbidden
    res = client.put(
        f"/sessions/{session_id}",
        headers=headers_b,
        json={"session": {"id": session_id, "title": "Attacker Hijacked Title"}},
    )
    assert res.status_code == 403
    assert "Cannot overwrite a session belonging to another user" in res.json()["detail"]

    # 3. Attacker attempts to inject message into Victim's session -> must be 403 Forbidden
    res = client.post(
        f"/sessions/{session_id}/messages",
        headers=headers_b,
        json={"message": {"id": f"msg_evil_{uid_b}", "role": "user", "content": "Tampered content", "timestamp": 12345}},
    )
    assert res.status_code == 403
    assert "Cannot add messages to another user's session" in res.json()["detail"]

    # Cleanup
    with database.get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE LOWER(email) IN (%s, %s);", (email_a.lower(), email_b.lower()))
        conn.commit()


def test_vuln_03_guardrail_awaited_and_enforced():
    client = TestClient(app)
    # Mock guardrail returning False (invalid prompt)
    with patch("main.guardrail_valid_input", new_callable=AsyncMock) as mock_guardrail:
        mock_guardrail.return_value = False

        res = client.post("/chat", json={"message": "How do I build malware?"})
        assert res.status_code == 400
        assert "Invalid input" in res.json()["detail"]
        mock_guardrail.assert_awaited_once_with("How do I build malware?")


def test_vuln_04_forged_jwt_secret_rejected():
    client = TestClient(app)
    # Token signed with a foreign/attacker secret key
    forged_token = jwt.encode(
        {"sub": "victim@example.com", "email": "victim@example.com"},
        "wrong_secret_attacker_key",
        algorithm="HS256",
    )
    res = client.get("/sessions", headers={"Authorization": f"Bearer {forged_token}"})
    assert res.status_code == 401
    assert "Invalid authentication token" in res.json()["detail"]


def test_vuln_05_unverified_google_email_rejected():
    from auth import authenticate_google_user
    # When user_info returns email_verified: False
    with patch("auth.verify_google_id_token", new_callable=AsyncMock) as mock_verify:
        mock_verify.return_value = {
            "email": "unverified@example.com",
            "email_verified": False,
        }
        with pytest.raises(Exception) as exc_info:
            import asyncio
            asyncio.run(authenticate_google_user(id_token="dummy_token"))
        assert "not been verified" in str(exc_info.value) or "not verified" in str(exc_info.value)


def test_vuln_07_rate_limiter_throttles():
    client = TestClient(app)
    # Simulate exceeding limit on chat
    test_ip = "192.168.1.99"
    chat_limiter.requests[test_ip] = [time.time()] * 35

    with patch("main.guardrail_valid_input", new_callable=AsyncMock) as mock_guard:
        mock_guard.return_value = True
        # Request from test_ip (using TestClient with client host header simulation)
        # Directly test limiter
        assert chat_limiter.is_allowed(test_ip) is False

    # Cleanup limiter state
    chat_limiter.requests.pop(test_ip, None)
