import os, json, base64, asyncio, array
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx
import websockets
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

def wss_url(path):
    base = os.environ.get("PUBLIC_BASE_URL", "")
    if not base:
        return "wss://example.com" + path
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    if base.endswith("/"):
        base = base[:-1]
    return base + path

def be16_to_le16(b: bytes) -> bytes:
    if not b:
        return b
    a = array.array("h")
    a.frombytes(b[1::2] + b[0::2])
    return a.tobytes()

def le16_to_be16(b: bytes) -> bytes:
    if not b:
        return b
    a = array.array("h")
    a.frombytes(b)
    bb = a.tobytes()
    return bb[1::2] + bb[0::2]

@app.get("/")
async def root():
    return PlainTextResponse("ok")

@app.get("/flow")
async def flow_get():
    return JSONResponse({"action": "Stream", "ws_url": wss_url("/media-stream"), "chunk_size": 400})

@app.post("/flow")
async def flow_post():
    return JSONResponse({"action": "Stream", "ws_url": wss_url("/media-stream"), "chunk_size": 400})

@app.post("/webhook")
async def webhook(req: Request):
    try:
        body = await req.json()
    except:
        body = {}
    print("frejun_webhook", json.dumps(body))
    return JSONResponse({"ok": True})

@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    vapi_ws = None
    reader_task = None
    next_chunk_id = 1
    closed = False

    async def close_all():
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            await ws.close()
        except:
            pass
        try:
            if vapi_ws:
                await vapi_ws.close()
        except:
            pass
        try:
            if reader_task:
                reader_task.cancel()
        except:
            pass

    async def vapi_reader():
        nonlocal next_chunk_id
        try:
            while True:
                msg = await vapi_ws.recv()
                if isinstance(msg, bytes):
                    be = le16_to_be16(msg)
                    b64 = base64.b64encode(be).decode()
                    out = {"type": "audio", "audio_b64": b64, "chunk_id": next_chunk_id}
                    next_chunk_id += 1
                    await ws.send_text(json.dumps(out))
                else:
                    try:
                        j = json.loads(msg)
                        print("vapi_control", json.dumps(j))
                    except:
                        pass
        except:
            await close_all()

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
            except:
                continue

            if msg.get("type") == "start":
                api_key = os.environ.get("VAPI_API_KEY", "")
                assistant_id = os.environ.get("VAPI_ASSISTANT_ID", "")
                if not api_key or not assistant_id:
                    await close_all()
                    break
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.post(
                        "https://api.vapi.ai/call",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "assistantId": assistant_id,
                            "transport": {
                                "provider": "vapi.websocket",
                                "audioFormat": {"format": "pcm_s16le", "container": "raw", "sampleRate": 8000}
                            }
                        }
                    )
                if r.status_code >= 300:
                    print("vapi_create_error", r.text)
                    await close_all()
                    break
                j = r.json()
                ws_url = j.get("websocketCallUrl") or j.get("websocketUrl") or (j.get("transport") or {}).get("websocketCallUrl")
                if not ws_url:
                    print("vapi_no_ws_url", json.dumps(j))
                    await close_all()
                    break
                vapi_ws = await websockets.connect(ws_url, extra_headers={"Authorization": f"Bearer {api_key}"})
                reader_task = asyncio.create_task(vapi_reader())
                continue

            if msg.get("type") == "audio":
                if vapi_ws is None:
                    continue
                try:
                    be = base64.b64decode(msg.get("audio_b64", ""), validate=True)
                except:
                    continue
                le = be16_to_le16(be)
                try:
                    await vapi_ws.send(le)
                except:
                    await close_all()
                    break
                continue

            if msg.get("type") in ["interrupt", "clear"]:
                continue
    except WebSocketDisconnect:
        await close_all()
    except:
        await close_all()
