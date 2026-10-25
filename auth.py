import os
import datetime
import httpx
import jwt
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from fastapi import HTTPException, status
from dotenv import load_dotenv

load_dotenv(override=True)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")

JWT_SECRET = os.getenv("JWT_SECRET") or GOOGLE_CLIENT_SECRET or "gitagpt_jwt_default_secret_key_change_in_production"
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_DAYS = 30


def create_user_access_token(email: str, username: str) -> str:
    """
    Creates a signed JWT access token for the authenticated user.
    """
    clean_email = email.strip().lower()
    clean_username = username.strip() if username else clean_email.split("@")[0]
    payload = {
        "sub": clean_email,
        "email": clean_email,
        "username": clean_username,
        "iat": datetime.datetime.now(datetime.timezone.utc),
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=JWT_EXPIRATION_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_user_access_token(token: str) -> dict:
    """
    Verifies the JWT access token and returns payload.
    Raises HTTPException(401) on failure.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token has expired. Please log in again.",
        )
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {str(e)}",
        )



async def verify_google_id_token(token: str) -> dict:
    """
    Verifies a Google ID Token (JWT) either locally using Google public certs
    or via Google's tokeninfo API.
    """
    # Attempt verification using google-auth library
    try:
        req = google_requests.Request()
        audience = GOOGLE_CLIENT_ID if GOOGLE_CLIENT_ID else None
        payload = google_id_token.verify_oauth2_token(token, req, audience=audience)
        if payload.get("iss") not in ["accounts.google.com", "https://accounts.google.com"]:
            raise ValueError("Token issuer is not Google")
        return payload
    except Exception:
        pass

    # Fallback: Verify via Google's official tokeninfo endpoint
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": token},
                timeout=10.0,
            )
            if resp.status_code == 200:
                payload = resp.json()
                if GOOGLE_CLIENT_ID and payload.get("aud") != GOOGLE_CLIENT_ID:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Google token audience mismatch",
                    )
                return payload
            else:
                error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                error_desc = error_data.get("error_description", resp.text)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Google ID token verification failed: {error_desc}",
                )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not verify Google ID token: {str(e)}",
        )


async def verify_google_access_token(token: str) -> dict:
    """
    Retrieves user profile info from Google using an OAuth2 Access Token.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                error_desc = error_data.get("error_description", resp.text)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Google access token verification failed: {error_desc}",
                )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not verify Google access token: {str(e)}",
        )


async def exchange_google_code_for_user(code: str, redirect_uri: str | None = None) -> dict:
    """
    Exchanges a Google OAuth authorization code for tokens and extracts user profile info.
    """
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be configured to exchange authorization codes.",
        )

    candidates = [redirect_uri] if redirect_uri else ["postmessage", GOOGLE_REDIRECT_URI]
    last_error = ""

    for effective_redirect in candidates:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "code": code,
                        "client_id": GOOGLE_CLIENT_ID,
                        "client_secret": GOOGLE_CLIENT_SECRET,
                        "redirect_uri": effective_redirect,
                        "grant_type": "authorization_code",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    token_data = resp.json()
                    if "id_token" in token_data:
                        return await verify_google_id_token(token_data["id_token"])
                    elif "access_token" in token_data:
                        return await verify_google_access_token(token_data["access_token"])
                    else:
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail="Google token response did not contain id_token or access_token",
                        )
                else:
                    error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    error_desc = error_data.get("error_description", resp.text)
                    last_error = error_desc
        except HTTPException:
            raise
        except Exception as e:
            last_error = str(e)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Google authorization code exchange failed: {last_error}",
    )


async def authenticate_google_user(
    token: str | None = None,
    credential: str | None = None,
    id_token: str | None = None,
    access_token: str | None = None,
    code: str | None = None,
    redirect_uri: str | None = None,
) -> tuple[str, str]:
    """
    Authenticates a Google OAuth user via token, credential, or code.
    Returns:
        tuple[str, str]: (email, username)
    """
    raw_token = credential or id_token or token or access_token

    user_info = None

    if code:
        user_info = await exchange_google_code_for_user(code, redirect_uri)
    elif raw_token:
        # Check if it looks like an access token or an ID token (JWT has 3 dot-separated parts)
        if raw_token.startswith("ya29."):
            user_info = await verify_google_access_token(raw_token)
        else:
            try:
                user_info = await verify_google_id_token(raw_token)
            except HTTPException:
                # If ID token verification failed, try as access token before giving up
                try:
                    user_info = await verify_google_access_token(raw_token)
                except HTTPException:
                    raise
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Google authentication parameter: provide 'token', 'credential', 'id_token', 'access_token', or 'code'.",
        )

    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Failed to retrieve user information from Google.",
        )

    email = user_info.get("email")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google profile does not contain an email address.",
        )

    # Prefer 'name', then 'given_name', then email prefix
    username = user_info.get("name") or user_info.get("given_name") or email.split("@")[0]

    return email, username


def get_google_login_url(redirect_uri: str | None = None, state: str | None = None) -> str:
    """
    Generates the Google OAuth authorization URL for code flow redirection.
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GOOGLE_CLIENT_ID is not configured.",
        )
    effective_redirect = redirect_uri or GOOGLE_REDIRECT_URI
    base_url = "https://accounts.google.com/o/oauth2/v2/auth"
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": effective_redirect,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state
    import urllib.parse
    query_string = urllib.parse.urlencode(params)
    return f"{base_url}?{query_string}"
