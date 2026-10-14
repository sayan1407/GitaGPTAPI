import uuid
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from main import app
import database


@pytest.fixture(autouse=True)
def ensure_db():
    database.init_db()


def test_missing_auth_params_returns_400():
    client = TestClient(app)
    response = client.post("/auth/google", json={})
    assert response.status_code == 400
    assert "Missing Google authentication parameter" in response.json()["detail"]


def test_invalid_token_returns_401():
    client = TestClient(app)
    # A random invalid token passed to Google
    response = client.post("/auth/google", json={"token": "invalid_dummy_token_xyz"})
    assert response.status_code == 401


def test_new_user_registration_and_db_persistence():
    client = TestClient(app)
    unique_id = uuid.uuid4().hex[:8]
    test_email = f"user_{unique_id}@example.com"
    test_username = f"User {unique_id}"

    # Mock authenticate_google_user returning this user's profile
    with patch("main.authenticate_google_user", new_callable=AsyncMock) as mock_auth:
        mock_auth.return_value = (test_email, test_username)

        # 1. First authentication: new user
        response = client.post("/auth/google", json={"token": "mock_google_token"})
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == test_email
        assert data["username"] == test_username
        assert data["is_new_user"] is True

        # Verify record exists in Neon PostgreSQL
        with database.get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT username, email FROM users WHERE LOWER(email) = %s;", (test_email.lower(),))
                row = cur.fetchone()
                assert row is not None
                assert row[0] == test_username
                assert row[1] == test_email

        # 2. Second authentication: existing user
        response2 = client.post("/auth/google", json={"token": "mock_google_token"})
        assert response2.status_code == 200
        data2 = response2.json()
        assert data2["email"] == test_email
        assert data2["username"] == test_username
        assert data2["is_new_user"] is False

        # Cleanup test user from DB
        with database.get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE LOWER(email) = %s;", (test_email.lower(),))
            conn.commit()


def test_get_callback_endpoint():
    client = TestClient(app)
    unique_id = uuid.uuid4().hex[:8]
    test_email = f"cb_{unique_id}@example.com"
    test_username = f"Callback User {unique_id}"

    with patch("main.authenticate_google_user", new_callable=AsyncMock) as mock_auth:
        mock_auth.return_value = (test_email, test_username)

        response = client.get(f"/auth/google/callback?code=mock_auth_code_{unique_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == test_email
        assert data["username"] == test_username
        assert data["is_new_user"] is True

        # Cleanup
        with database.get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE LOWER(email) = %s;", (test_email.lower(),))
            conn.commit()

