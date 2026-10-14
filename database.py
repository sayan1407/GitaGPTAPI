import os
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv(override=True)

DEFAULT_DATABASE_URL = "postgresql://neondb_owner:npg_txf4TwN5lSYX@ep-round-violet-b5sx8fmk-pooler.c-7.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


@contextmanager
def get_db_connection():
    """Context manager for acquiring and closing a database connection."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Initializes the database schema by ensuring the users table exists."""
    create_table_query = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username VARCHAR(255) NOT NULL,
        email VARCHAR(255) UNIQUE NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(create_table_query)
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

