import time
import uuid
import pytest
from fastapi.testclient import TestClient
from main import app
import database


@pytest.fixture(autouse=True)
def setup_db():
    database.init_db()


def test_chat_history_full_lifecycle():
    client = TestClient(app)
    uid = uuid.uuid4().hex[:8]
    test_email = f"chat_user_{uid}@example.com"
    session_id = f"sess_{uid}"
    msg1_id = f"msg_u_{uid}"
    msg2_id = f"msg_a_{uid}"

    now_ms = int(time.time() * 1000)

    # 1. Initially user should have no sessions
    res = client.get(f"/sessions?user_email={test_email}")
    assert res.status_code == 200
    assert res.json() == []

    # 2. Create a session with 2 messages via PUT /sessions/{session_id}
    session_payload = {
        "user_email": test_email,
        "session": {
            "id": session_id,
            "title": "Initial Conversation",
            "createdAt": now_ms,
            "updatedAt": now_ms,
            "previousResponseId": "resp_001",
            "messages": [
                {
                    "id": msg1_id,
                    "role": "user",
                    "content": "Teach me about devotion.",
                    "timestamp": now_ms,
                },
                {
                    "id": msg2_id,
                    "role": "assistant",
                    "content": "Bhakti yoga is devotion to the Supreme.",
                    "timestamp": now_ms + 100,
                    "responseId": "resp_001",
                },
            ],
        },
    }
    res = client.put(f"/sessions/{session_id}", json=session_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == session_id
    assert data["title"] == "Initial Conversation"
    assert len(data["messages"]) == 2

    # 3. GET /sessions should now return the session
    res = client.get(f"/sessions?user_email={test_email}")
    assert res.status_code == 200
    sessions = res.json()
    assert len(sessions) == 1
    assert sessions[0]["id"] == session_id
    assert sessions[0]["title"] == "Initial Conversation"
    assert len(sessions[0]["messages"]) == 2
    assert sessions[0]["messages"][0]["content"] == "Teach me about devotion."

    # 4. Add a third message via POST /sessions/{session_id}/messages
    msg3_id = f"msg_u2_{uid}"
    msg_payload = {
        "user_email": test_email,
        "message": {
            "id": msg3_id,
            "role": "user",
            "content": "Which chapter discusses this?",
            "timestamp": now_ms + 200,
        },
    }
    res = client.post(f"/sessions/{session_id}/messages", json=msg_payload)
    assert res.status_code == 200
    msg_data = res.json()
    assert msg_data["id"] == msg3_id
    assert msg_data["content"] == "Which chapter discusses this?"

    # Verify message count increased to 3
    res = client.get(f"/sessions?user_email={test_email}")
    sessions = res.json()
    assert len(sessions[0]["messages"]) == 3

    # 5. Rename session via PATCH /sessions/{session_id}
    patch_payload = {
        "user_email": test_email,
        "title": "Bhakti Yoga Chapter Discussion",
    }
    res = client.patch(f"/sessions/{session_id}", json=patch_payload)
    assert res.status_code == 200

    res = client.get(f"/sessions?user_email={test_email}")
    sessions = res.json()
    assert sessions[0]["title"] == "Bhakti Yoga Chapter Discussion"

    # 6. Delete the session via DELETE /sessions/{session_id}
    res = client.delete(f"/sessions/{session_id}?user_email={test_email}")
    assert res.status_code == 200

    res = client.get(f"/sessions?user_email={test_email}")
    assert res.json() == []

    # 7. Test batch sync (localStorage migration)
    sync_session_id = f"sync_sess_{uid}"
    batch_payload = {
        "user_email": test_email,
        "sessions": [
            {
                "id": sync_session_id,
                "title": "Migrated from LocalStorage",
                "createdAt": now_ms,
                "updatedAt": now_ms,
                "previousResponseId": None,
                "messages": [
                    {
                        "id": f"msg_sync_{uid}",
                        "role": "user",
                        "content": "Migrated question",
                        "timestamp": now_ms,
                    }
                ],
            }
        ],
    }
    res = client.post("/sessions/sync", json=batch_payload)
    assert res.status_code == 200
    synced = res.json()
    assert len(synced) == 1
    assert synced[0]["title"] == "Migrated from LocalStorage"

    # Verify synced session appears
    res = client.get(f"/sessions?user_email={test_email}")
    assert len(res.json()) == 1

    # 8. Clear all sessions via DELETE /sessions
    res = client.delete(f"/sessions?user_email={test_email}")
    assert res.status_code == 200
    assert res.json()["deleted_count"] >= 1

    res = client.get(f"/sessions?user_email={test_email}")
    assert res.json() == []

    # Cleanup user
    with database.get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE LOWER(email) = %s;", (test_email.lower(),))
        conn.commit()


def test_bola_idor_protection_and_bearer_auth():
    """
    Verifies that:
    1. Authenticated user can manage sessions via Bearer token without needing user_email.
    2. Attempting to access/modify another user's sessions using a different user_email is blocked with 403 Forbidden.
    3. Invalid or forged tokens return 401 Unauthorized.
    """
    from auth import create_user_access_token
    client = TestClient(app)
    uid_a = uuid.uuid4().hex[:8]
    uid_b = uuid.uuid4().hex[:8]
    email_a = f"user_a_{uid_a}@example.com"
    email_b = f"user_b_{uid_b}@example.com"
    token_a = create_user_access_token(email_a, f"User A {uid_a}")
    token_b = create_user_access_token(email_b, f"User B {uid_b}")

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 1. User A creates a session using Bearer token
    session_id = f"sess_a_{uid_a}"
    now_ms = int(time.time() * 1000)
    res = client.put(
        f"/sessions/{session_id}",
        headers=headers_a,
        json={
            "session": {
                "id": session_id,
                "title": "User A Private Session",
                "createdAt": now_ms,
                "updatedAt": now_ms,
                "messages": [
                    {
                        "id": f"msg_a_{uid_a}",
                        "role": "user",
                        "content": "Secret query by User A",
                        "timestamp": now_ms,
                    }
                ],
            }
        },
    )
    assert res.status_code == 200

    # 2. User A retrieves sessions with Bearer token (no user_email param needed)
    res = client.get("/sessions", headers=headers_a)
    assert res.status_code == 200
    sessions_a = res.json()
    assert len(sessions_a) == 1
    assert sessions_a[0]["title"] == "User A Private Session"

    # 3. User B cannot see User A's session
    res = client.get("/sessions", headers=headers_b)
    assert res.status_code == 200
    assert res.json() == []

    # 4. BOLA / IDOR Attack: User A tries to tamper with User B's account by sending user_email=email_b
    res = client.get(f"/sessions?user_email={email_b}", headers=headers_a)
    assert res.status_code == 403
    assert "Cannot access or modify another user's sessions" in res.json()["detail"]

    # 5. Invalid token returns 401
    res = client.get("/sessions", headers={"Authorization": "Bearer invalid_forged_token"})
    assert res.status_code == 401

    # Cleanup
    with database.get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE LOWER(email) IN (%s, %s);", (email_a.lower(), email_b.lower()))
        conn.commit()
