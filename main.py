import os
import time
from fastapi import FastAPI, HTTPException, Header, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI
import json
from contextlib import asynccontextmanager

from database import (
    init_db,
    save_user_if_new,
    get_user_sessions,
    upsert_session,
    save_message,
    update_session,
    delete_session,
    clear_user_sessions,
    batch_sync_sessions,
)
from auth import (
    authenticate_google_user,
    get_google_login_url,
    create_user_access_token,
    verify_user_access_token,
)

load_dotenv(override=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="GitaGPTAPI", lifespan=lifespan)

origins = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "https://gitagpt-dun.vercel.app"
    
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = AsyncOpenAI()

guardrail_agent_instruction = """
You are a gurdrail agent that verifies if the user input is appropiate to ask to a Bhagavad Gita scholar. 
You should check if the user input is about dealing with personal problem, spiritual guidance, or questions related to the Bhagavad Gita. 
"""
class ValidUserInput(BaseModel):
    is_valid_input: bool


async def guardrail_valid_input(input_text: str) -> bool:
    
    result = await client.responses.parse(
         instructions=guardrail_agent_instruction,
         model="gpt-5.4-mini",
         input=input_text,
         text_format=ValidUserInput
    )
    return result.output_parsed.is_valid_input

class ChatRequest(BaseModel):
    message: str
    previous_response_id : str | None = None

class ChatResponse(BaseModel):
    reply: str
    response_id : str

instruction = """
   You are scholar of Bhagavad Gita, a sacred Hindu scripture. \
   Your task is to answer questions based on the text and teachings of the Bhagavad Gita. Questions can be about teaching, philosophy of Bhagwat Gita or it can be about user's personal life and how the teachings of Bhagavad Gita can be applied to it.
   While answering, you should provide insights and interpretations that reflect the philosophical and spiritual teachings of the Gita. 
   You can mention the verse you are referring to in your answer. If you don't know the answer, say you don't know.
"""

@app.post("/chat")
async def chat(request: ChatRequest):
    async def generate():
        print("Generator started", flush=True)
        is_valid_input = guardrail_valid_input(request.message)
        if not is_valid_input:
            raise HTTPException(status_code=400, detail="Invalid input. Please ask questions related to Bhagavad Gita or personal guidance.")
        try:
            stream = await client.responses.create(
                instructions=instruction,
                model="gpt-4o-mini",
                input=request.message,
                stream=True,
                previous_response_id=request.previous_response_id
            )

            async for event in stream:
               if event.type == "response.output_text.delta":
                   print(event.delta, end="", flush=True)
                   yield f"data: {json.dumps({
                                           'type': 'chunk',
                                           'content': event.delta
                                       })}\n\n"
               elif event.type == "response.completed":
                    print("Stream finished", flush=True)
                    yield f"response_id: {event.response.id}\n\n"
                    
        except Exception as e:
            print(f"Streaming error: {e}", flush=True)
            yield f"data: {json.dumps({
                'type': 'error',
                'content': str(e)
            })}\n\n"
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class GoogleAuthRequest(BaseModel):
    token: str | None = None
    credential: str | None = None
    id_token: str | None = None
    access_token: str | None = None
    code: str | None = None
    redirect_uri: str | None = None


class GoogleAuthResponse(BaseModel):
    username: str
    email: str
    access_token: str | None = None
    token: str | None = None
    token_type: str = "bearer"
    is_new_user: bool | None = None
    message: str | None = None


@app.post("/auth/google", response_model=GoogleAuthResponse)
async def google_auth(request: GoogleAuthRequest):
    """
    Authenticate a user via Google OAuth:
    - Accepts an ID token / credential, access token, or authorization code.
    - If it's a new user, saves username and email in Neon PostgreSQL.
    - Generates and returns a signed session JWT along with profile info.
    """
    email, username = await authenticate_google_user(
        token=request.token,
        credential=request.credential,
        id_token=request.id_token,
        access_token=request.access_token,
        code=request.code,
        redirect_uri=request.redirect_uri,
    )

    user, is_new = save_user_if_new(email=email, username=username)
    access_token = create_user_access_token(user["email"], user["username"])

    return GoogleAuthResponse(
        username=user["username"],
        email=user["email"],
        access_token=access_token,
        token=access_token,
        token_type="bearer",
        is_new_user=is_new,
        message="User registered successfully." if is_new else "User logged in successfully.",
    )


@app.api_route("/auth/google/callback", methods=["GET", "POST"], response_model=GoogleAuthResponse)
async def google_auth_callback(
    request: Request,
    code: str | None = None,
    token: str | None = None,
    credential: str | None = None,
    redirect_uri: str | None = None,
):
    """
    Handles Google OAuth redirect callbacks:
    - Accepts authorization code via POST JSON body or query parameter (fixing CWE-598).
    - If it's a new user, saves username and email in Neon PostgreSQL.
    - Generates and returns a signed session JWT along with profile info.
    """
    body_data = {}
    if request.method == "POST":
        try:
            body_data = await request.json()
        except Exception:
            pass

    effective_code = code or body_data.get("code")
    effective_token = token or body_data.get("token") or body_data.get("id_token") or body_data.get("access_token")
    effective_credential = credential or body_data.get("credential")
    effective_redirect_uri = redirect_uri or body_data.get("redirect_uri")

    email, username = await authenticate_google_user(
        token=effective_token,
        credential=effective_credential,
        code=effective_code,
        redirect_uri=effective_redirect_uri,
    )

    user, is_new = save_user_if_new(email=email, username=username)
    access_token = create_user_access_token(user["email"], user["username"])

    return GoogleAuthResponse(
        username=user["username"],
        email=user["email"],
        access_token=access_token,
        token=access_token,
        token_type="bearer",
        is_new_user=is_new,
        message="User registered successfully." if is_new else "User logged in successfully.",
    )


@app.get("/auth/google/login")
async def google_login(redirect_uri: str | None = None, state: str | None = None):
    """
    Returns the Google OAuth consent URL for client redirection.
    """
    login_url = get_google_login_url(redirect_uri=redirect_uri, state=state)
    return {"login_url": login_url}


# ---------------------------------------------------------
# Chat History Persistence Endpoints
# ---------------------------------------------------------

async def get_current_user_email(
    authorization: str | None = Header(None),
    user_email: str | None = None,
) -> str:
    """
    Validates token and returns authenticated user email.
    Enforces authorization to prevent BOLA / IDOR.
    """
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        else:
            token = authorization
        payload = verify_user_access_token(token)
        token_email = payload.get("email") or payload.get("sub")
        if not token_email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token payload is missing user identity",
            )
        # Prevent BOLA/IDOR: If user_email is also provided, ensure it matches the token
        if user_email and user_email.strip().lower() != token_email.strip().lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: Cannot access or modify another user's sessions.",
            )
        return token_email.strip().lower()

    # Fallback for unauthenticated legacy calls / backwards compatibility
    if user_email and user_email.strip():
        return user_email.strip().lower()

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required. Please provide a Bearer token or user_email.",
    )


class MessageModel(BaseModel):
    id: str
    role: str
    content: str
    timestamp: int
    responseId: str | None = None
    response_id: str | None = None
    isStreaming: bool | None = False
    error: bool | None = False


class ChatSessionModel(BaseModel):
    id: str
    title: str = "New Conversation"
    createdAt: int | None = None
    created_at: int | None = None
    updatedAt: int | None = None
    updated_at: int | None = None
    previousResponseId: str | None = None
    previous_response_id: str | None = None
    messages: list[MessageModel] = []


class SessionSaveRequest(BaseModel):
    user_email: str | None = None
    session: ChatSessionModel


class MessageSaveRequest(BaseModel):
    user_email: str | None = None
    message: MessageModel


class SessionUpdateRequest(BaseModel):
    user_email: str | None = None
    title: str | None = None
    previous_response_id: str | None = None
    updated_at: int | None = None


class BatchSyncRequest(BaseModel):
    user_email: str | None = None
    sessions: list[ChatSessionModel]


@app.get("/sessions")
async def get_sessions(
    user_email: str | None = None,
    authorization: str | None = Header(None),
):
    """Retrieves all chat sessions and their messages for a user."""
    effective_email = await get_current_user_email(authorization, user_email)
    return get_user_sessions(effective_email)


@app.post("/sessions")
async def create_session(
    payload: SessionSaveRequest,
    authorization: str | None = Header(None),
):
    """Creates or updates a chat session for a user."""
    effective_email = await get_current_user_email(authorization, payload.user_email)
    return upsert_session(effective_email, payload.session.model_dump())


@app.put("/sessions/{session_id}")
async def update_full_session(
    session_id: str,
    payload: SessionSaveRequest,
    authorization: str | None = Header(None),
):
    """Upserts a full session and all its messages."""
    effective_email = await get_current_user_email(authorization, payload.user_email)
    data = payload.session.model_dump()
    data["id"] = session_id
    return upsert_session(effective_email, data)


@app.patch("/sessions/{session_id}")
async def patch_session_metadata(
    session_id: str,
    payload: SessionUpdateRequest,
    authorization: str | None = Header(None),
):
    """Updates session title, previous_response_id, or updated_at."""
    effective_email = await get_current_user_email(authorization, payload.user_email)
    success = update_session(
        user_email=effective_email,
        session_id=session_id,
        title=payload.title,
        previous_response_id=payload.previous_response_id,
        updated_at=payload.updated_at,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Session not found or user mismatch")
    return {"success": True, "session_id": session_id}


@app.post("/sessions/{session_id}/messages")
async def add_session_message(
    session_id: str,
    payload: MessageSaveRequest,
    authorization: str | None = Header(None),
):
    """Adds or updates a single message in a session."""
    effective_email = await get_current_user_email(authorization, payload.user_email)
    return save_message(effective_email, session_id, payload.message.model_dump())


@app.delete("/sessions/{session_id}")
async def remove_session(
    session_id: str,
    user_email: str | None = None,
    authorization: str | None = Header(None),
):
    """Deletes a specific session and all its messages."""
    effective_email = await get_current_user_email(authorization, user_email)
    success = delete_session(effective_email, session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found or user mismatch")
    return {"success": True, "deleted_session_id": session_id}


@app.delete("/sessions")
async def clear_all_sessions(
    user_email: str | None = None,
    authorization: str | None = Header(None),
):
    """Deletes all sessions belonging to the user."""
    effective_email = await get_current_user_email(authorization, user_email)
    count = clear_user_sessions(effective_email)
    return {"success": True, "deleted_count": count}


@app.post("/sessions/sync")
async def sync_sessions(
    payload: BatchSyncRequest,
    authorization: str | None = Header(None),
):
    """Syncs a batch of sessions (e.g. initial localStorage migration to database)."""
    effective_email = await get_current_user_email(authorization, payload.user_email)
    return batch_sync_sessions(effective_email, [s.model_dump() for s in payload.sessions])
