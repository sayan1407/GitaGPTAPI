import os
import time
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL")


@contextmanager
def get_db_connection():
    """Context manager for acquiring and closing a database connection."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Initializes the database schema by ensuring the users, chat_sessions, and chat_messages tables exist."""
    create_tables_query = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        email VARCHAR(255) UNIQUE NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS chat_sessions (
        id VARCHAR(255) PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title VARCHAR(255) NOT NULL DEFAULT 'New Conversation',
        previous_response_id VARCHAR(255),
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_id ON chat_sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated_at ON chat_sessions(updated_at DESC);

    CREATE TABLE IF NOT EXISTS chat_messages (
        id VARCHAR(255) PRIMARY KEY,
        session_id VARCHAR(255) NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        role VARCHAR(50) NOT NULL,
        content TEXT NOT NULL,
        timestamp BIGINT NOT NULL,
        response_id VARCHAR(255),
        error BOOLEAN DEFAULT FALSE
    );

    CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages(session_id);
    CREATE INDEX IF NOT EXISTS idx_chat_messages_timestamp ON chat_messages(timestamp ASC);
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(create_tables_query)
        conn.commit()


def save_user_if_new(email: str, username: str) -> tuple[dict, bool]:
    """
    Checks if a user with the provided email exists in Neon PostgreSQL.
    If the user does not exist, inserts a new user record.
    Returns:
        tuple[dict, bool]: (user_dict, is_new_user)
    """
    clean_email = email.strip().lower()
    clean_username = username.strip() if username else clean_email.split("@")[0]

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Check if user already exists
            cur.execute(
                "SELECT id, username, email, created_at FROM users WHERE LOWER(email) = %s;",
                (clean_email,),
            )
            existing_user = cur.fetchone()
            if existing_user:
                return dict(existing_user), False

            # Insert new user with ON CONFLICT safety
            cur.execute(
                """
                INSERT INTO users (username, email)
                VALUES (%s, %s)
                ON CONFLICT (email) DO NOTHING
                RETURNING id, username, email, created_at;
                """,
                (clean_username, clean_email),
            )
            inserted_user = cur.fetchone()
            conn.commit()

            if inserted_user:
                return dict(inserted_user), True
            else:
                # Handle possible race condition if inserted concurrently
                cur.execute(
                    "SELECT id, username, email, created_at FROM users WHERE LOWER(email) = %s;",
                    (clean_email,),
                )
                user = cur.fetchone()
                return dict(user) if user else {"username": clean_username, "email": clean_email}, False


def get_or_create_user(email: str, username: str = "") -> dict:
    """Gets an existing user by email or creates a new one."""
    clean_email = email.strip().lower()
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, email, created_at FROM users WHERE LOWER(email) = %s;",
                (clean_email,),
            )
            existing_user = cur.fetchone()
            if existing_user:
                return dict(existing_user)
    user, _ = save_user_if_new(email=clean_email, username=username)
    return user


def get_user_sessions(user_email: str) -> list[dict]:
    """
    Retrieves all chat sessions for the specified user email along with their messages,
    ordered by most recently updated first.
    """
    clean_email = user_email.strip().lower()
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM users WHERE LOWER(email) = %s;",
                (clean_email,),
            )
            user = cur.fetchone()
            if not user:
                return []
            user_id = user["id"]

            cur.execute(
                """
                SELECT id, title, previous_response_id, created_at, updated_at
                FROM chat_sessions
                WHERE user_id = %s
                ORDER BY updated_at DESC;
                """,
                (user_id,),
            )
            sessions = cur.fetchall()
            if not sessions:
                return []

            session_ids = [s["id"] for s in sessions]
            cur.execute(
                """
                SELECT id, session_id, role, content, timestamp, response_id, error
                FROM chat_messages
                WHERE session_id = ANY(%s)
                ORDER BY timestamp ASC;
                """,
                (session_ids,),
            )
            all_messages = cur.fetchall()

            # Group messages by session_id
            messages_by_session: dict[str, list[dict]] = {s_id: [] for s_id in session_ids}
            for msg in all_messages:
                s_id = msg["session_id"]
                if s_id in messages_by_session:
                    messages_by_session[s_id].append({
                        "id": msg["id"],
                        "role": msg["role"],
                        "content": msg["content"],
                        "timestamp": int(msg["timestamp"]),
                        "responseId": msg["response_id"],
                        "error": bool(msg["error"]),
                    })

            result = []
            for s in sessions:
                result.append({
                    "id": s["id"],
                    "title": s["title"],
                    "createdAt": int(s["created_at"]),
                    "updatedAt": int(s["updated_at"]),
                    "previousResponseId": s["previous_response_id"],
                    "messages": messages_by_session.get(s["id"], []),
                })
            return result


def upsert_session(user_email: str, session: dict) -> dict:
    """
    Inserts or updates a complete chat session and its messages.
    """
    user = get_or_create_user(user_email)
    user_id = user["id"]

    session_id = session["id"]
    title = session.get("title") or "New Conversation"
    current_ms = int(time.time() * 1000)
    created_at = int(session.get("createdAt") or session.get("created_at") or current_ms)
    updated_at = int(session.get("updatedAt") or session.get("updated_at") or current_ms)
    previous_response_id = session.get("previousResponseId") or session.get("previous_response_id")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO chat_sessions (id, user_id, title, previous_response_id, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    previous_response_id = EXCLUDED.previous_response_id,
                    updated_at = EXCLUDED.updated_at
                RETURNING id, title, previous_response_id, created_at, updated_at;
                """,
                (session_id, user_id, title, previous_response_id, created_at, updated_at),
            )
            saved_session = dict(cur.fetchone())

            messages = session.get("messages", [])
            saved_messages = []
            for msg in messages:
                msg_id = msg["id"]
                role = msg["role"]
                content = msg["content"]
                timestamp = int(msg.get("timestamp") or current_ms)
                response_id = msg.get("responseId") or msg.get("response_id")
                error = bool(msg.get("error", False))

                cur.execute(
                    """
                    INSERT INTO chat_messages (id, session_id, role, content, timestamp, response_id, error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        content = EXCLUDED.content,
                        response_id = EXCLUDED.response_id,
                        error = EXCLUDED.error
                    RETURNING id, role, content, timestamp, response_id, error;
                    """,
                    (msg_id, session_id, role, content, timestamp, response_id, error),
                )
                m_row = cur.fetchone()
                saved_messages.append({
                    "id": m_row["id"],
                    "role": m_row["role"],
                    "content": m_row["content"],
                    "timestamp": int(m_row["timestamp"]),
                    "responseId": m_row["response_id"],
                    "error": bool(m_row["error"]),
                })
        conn.commit()

    return {
        "id": saved_session["id"],
        "title": saved_session["title"],
        "createdAt": int(saved_session["created_at"]),
        "updatedAt": int(saved_session["updated_at"]),
        "previousResponseId": saved_session["previous_response_id"],
        "messages": saved_messages,
    }


def save_message(user_email: str, session_id: str, message: dict) -> dict:
    """
    Appends or updates a single message in a session, and updates the session's timestamp.
    """
    user = get_or_create_user(user_email)
    user_id = user["id"]

    msg_id = message["id"]
    role = message["role"]
    content = message["content"]
    current_ms = int(time.time() * 1000)
    timestamp = int(message.get("timestamp") or current_ms)
    response_id = message.get("responseId") or message.get("response_id")
    error = bool(message.get("error", False))

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Ensure session exists and belongs to user
            cur.execute(
                "SELECT id FROM chat_sessions WHERE id = %s AND user_id = %s;",
                (session_id, user_id),
            )
            session_row = cur.fetchone()
            if not session_row:
                cur.execute(
                    """
                    INSERT INTO chat_sessions (id, user_id, title, previous_response_id, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING;
                    """,
                    (session_id, user_id, "New Conversation", response_id, timestamp, timestamp),
                )

            cur.execute(
                """
                INSERT INTO chat_messages (id, session_id, role, content, timestamp, response_id, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    content = EXCLUDED.content,
                    response_id = EXCLUDED.response_id,
                    error = EXCLUDED.error
                RETURNING id, role, content, timestamp, response_id, error;
                """,
                (msg_id, session_id, role, content, timestamp, response_id, error),
            )
            m_row = cur.fetchone()

            # Update session timestamp and previous_response_id
            if response_id:
                cur.execute(
                    """
                    UPDATE chat_sessions
                    SET updated_at = %s, previous_response_id = %s
                    WHERE id = %s AND user_id = %s;
                    """,
                    (timestamp, response_id, session_id, user_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE chat_sessions
                    SET updated_at = %s
                    WHERE id = %s AND user_id = %s;
                    """,
                    (timestamp, session_id, user_id),
                )
        conn.commit()

    return {
        "id": m_row["id"],
        "role": m_row["role"],
        "content": m_row["content"],
        "timestamp": int(m_row["timestamp"]),
        "responseId": m_row["response_id"],
        "error": bool(m_row["error"]),
    }


def update_session(
    user_email: str,
    session_id: str,
    title: str | None = None,
    previous_response_id: str | None = None,
    updated_at: int | None = None,
) -> bool:
    """Updates title, previous_response_id, or updated_at for a session."""
    clean_email = user_email.strip().lower()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE LOWER(email) = %s;", (clean_email,))
            user = cur.fetchone()
            if not user:
                return False
            user_id = user[0]

            updates = []
            params = []
            if title is not None:
                updates.append("title = %s")
                params.append(title)
            if previous_response_id is not None:
                updates.append("previous_response_id = %s")
                params.append(previous_response_id)
            if updated_at is not None:
                updates.append("updated_at = %s")
                params.append(updated_at)
            else:
                updates.append("updated_at = %s")
                params.append(int(time.time() * 1000))

            if not updates:
                return True

            params.extend([session_id, user_id])
            query = f"UPDATE chat_sessions SET {', '.join(updates)} WHERE id = %s AND user_id = %s;"
            cur.execute(query, tuple(params))
            affected = cur.rowcount > 0
        conn.commit()
    return affected


def delete_session(user_email: str, session_id: str) -> bool:
    """Deletes a chat session and cascades deletion to all its messages."""
    clean_email = user_email.strip().lower()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE LOWER(email) = %s;", (clean_email,))
            user = cur.fetchone()
            if not user:
                return False
            user_id = user[0]

            cur.execute("DELETE FROM chat_sessions WHERE id = %s AND user_id = %s;", (session_id, user_id))
            affected = cur.rowcount > 0
        conn.commit()
    return affected


def clear_user_sessions(user_email: str) -> int:
    """Deletes all chat sessions belonging to the user."""
    clean_email = user_email.strip().lower()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE LOWER(email) = %s;", (clean_email,))
            user = cur.fetchone()
            if not user:
                return 0
            user_id = user[0]

            cur.execute("DELETE FROM chat_sessions WHERE user_id = %s;", (user_id,))
            count = cur.rowcount
        conn.commit()
    return count


def batch_sync_sessions(user_email: str, sessions: list[dict]) -> list[dict]:
    """Syncs a batch of chat sessions to the database."""
    synced = []
    for session in sessions:
        synced.append(upsert_session(user_email, session))
    return synced
