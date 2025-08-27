import base64
import json
import logging
import os
from typing import Annotated

import httpx
from fastapi import APIRouter, Body, HTTPException, WebSocket, status, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from teler import AsyncClient, CallFlow
from teler.streams import StreamConnector, StreamOp, StreamType

router = APIRouter()
logger = logging.getLogger(__name__)

# Global variables
VAPI_API_KEY = os.getenv("VAPI_API_KEY", "")
VAPI_ASSISTANT_ID = os.getenv("VAPI_ASSISTANT_ID", "")
VAPI_SAMPLE_RATE = os.getenv("VAPI_SAMPLE_RATE", 8000)

SERVER_DOMAIN = os.getenv("SERVER_DOMAIN", "")

TELER_API_KEY = os.getenv("TELER_API_KEY", "")
FROM_NUMBER = "+91123*******"
TO_NUMBER = "+91456*******"

class CallFlowRequest(BaseModel):
    call_id: str
    account_id: str
    from_number: str
    to_number: str

async def call_stream_handler(message: str):
    """
    Handle incoming websocket messages from Teler.
    """
    msg = json.loads(message)
    if msg["type"] == "audio":
        payload  = base64.b64decode(msg["data"]["audio_b64"].encode('utf-8'))
        return (payload, StreamOp.RELAY)
    return ({}, StreamOp.PASS)

def remote_stream_handler():
    """
    Handle incoming websocket messages from Vapi.
    """
    chunk_id = 1

    async def handler(message: str):
        nonlocal chunk_id
        if isinstance(message, bytes):
            audio_b64 = base64.b64encode(message).decode('utf-8')
            payload = json.dumps(
                {
                    "type": "audio",
                    "audio_b64": audio_b64,
                    "chunk_id": chunk_id,
                }
            )
            chunk_id += 1
            return (payload, StreamOp.RELAY)
        else:
            logger.info(f"VAPI Control: {message}")
            return ({}, StreamOp.PASS)

    return handler

@router.post("/flow", status_code=status.HTTP_200_OK, include_in_schema=False)
async def stream_flow(payload: CallFlowRequest):
    """
    Build and return Stream flow.
    """
    stream_flow = CallFlow.stream(
        ws_url=f"wss://{SERVER_DOMAIN}/media-stream",
        chunk_size=500,
        record=True
    )
    return JSONResponse(stream_flow)

@router.post("/webhook", status_code=status.HTTP_200_OK, include_in_schema=False)
async def webhook_receiver(data: Annotated[dict, Body()]):
    """
    Log webhook payload.
    """
    logger.info(f"--------Webhook Payload-------- {data}")
    return JSONResponse(content="Webhook received.")

@router.get("/initiate-call", status_code=status.HTTP_200_OK)
async def initiate_call():
    """
    Initiate a call using Teler SDK.
    """
    try:
        async with AsyncClient(api_key=TELER_API_KEY, timeout=10) as client:
            call = await client.calls.create(
                from_number=f"{FROM_NUMBER}",
                to_number=f"{TO_NUMBER}",
                flow_url=f"https://{SERVER_DOMAIN}/flow",
                status_callback_url=f"https://{SERVER_DOMAIN}/webhook",
                record=True,
            )
            logger.info(f"Call created: {call}")
        return JSONResponse(content={"success": True})
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create call.")

@router.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connected.")
    vapi_ws_url = await create_vapi_call()
    connector = StreamConnector(
        stream_type=StreamType.BIDIRECTIONAL,
        remote_url=vapi_ws_url,
        call_stream_handler=call_stream_handler,
        remote_stream_handler=remote_stream_handler()
    )
    await connector.bridge_stream(websocket)

async def create_vapi_call():
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.vapi.ai/call",
            headers={"Authorization":f"Bearer {VAPI_API_KEY}","Content-Type":"application/json"},
            json={
                "assistantId": VAPI_ASSISTANT_ID,
                "transport": {"provider":"vapi.websocket","audioFormat":{"format":"pcm_s16le","container":"raw","sampleRate":VAPI_SAMPLE_RATE}}
            }
        )
        r.raise_for_status()
        j = r.json()
        return j.get("transport").get("websocketCallUrl")

# -------- add FastAPI app so uvicorn can find app:app --------
app = FastAPI()
app.include_router(router)
