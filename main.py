import os
import time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI
import json

load_dotenv(override=True)

app = FastAPI(title="GitaGPTAPI")

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
