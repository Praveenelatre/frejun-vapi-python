import os, json, base64, asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx, websockets

app = FastAPI()

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
VAPI_API_KEY = os.getenv("VAPI_API_KEY", "")
VAPI_ASSISTANT_ID = os.getenv("VAPI_ASSISTANT_ID", "")
PORT = int(os.getenv("PORT", "10000"))

def wss_url(path):
    if not PUBLIC_BASE_URL:
        return "wss://example.com" + path
    u = PUBLIC_BASE_URL.rstrip("/")
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    if u.startswith("https://"):
        u = "wss://" + u[len("https://"):]
    return u + path

@app.get("/")
async def root_get():
    return PlainTextResponse("ok")

@app.head("/")
async def root_head():
    return PlainTextResponse("")

@app.get("/flow")
async def flow_get():
    return JSONResponse({"action":"Stream","ws_url":wss_url("/media-stream"),"chunk_size":320})

@app.post("/flow")
async def flow_post():
    return JSONResponse({"action":"Stream","ws_url":wss_url("/media-stream"),"chunk_size":320})

@app.post("/webhook")
async def webhook(req: Request):
    try:
        body = await req.json()
    except:
        body = {}
    print("frejun_webhook", json.dumps(body))
    return JSONResponse({"ok": True})

async def create_vapi_call():
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.vapi.ai/call",
            headers={"Authorization":f"Bearer {VAPI_API_KEY}","Content-Type":"application/json"},
            json={
                "assistantId": VAPI_ASSISTANT_ID,
                "transport": {
                    "provider": "vapi.websocket",
                    "audioFormat": {"format":"pcm_s16le","container":"raw","sampleRate":16000}
                }
            }
        )
        r.raise_for_status()
        j = r.json()
        return j.get("websocketCallUrl") or j.get("websocketUrl") or (j.get("transport") or {}).get("websocketCallUrl")

def get_audio_b64(msg: dict):
    d = msg.get("data")
    if isinstance(d, dict) and "audio_b64" in d:
        return d["audio_b64"]
    return msg.get("audio_b64")

@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    vapi_ws = None
    reader_task = None
    next_chunk_id = 1

    async def close_all():
        try:
            if vapi_ws:
                await vapi_ws.close()
        except:
            pass
        try:
            await ws.close()
        except:
            pass

    async def vapi_reader():
        nonlocal next_chunk_id
        try:
            while True:
                msg = await vapi_ws.recv()
                if isinstance(msg, bytes):
                    b64 = base64.b64encode(msg).decode()
                    out = {"type":"audio","audio_b64": b64, "chunk_id": next_chunk_id}
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
            txt = await ws.receive_text()
            try:
                msg = json.loads(txt)
            except:
                continue

            t = msg.get("type") or msg.get("event")

            if t == "start":
                url = await create_vapi_call()
                vapi_ws = await websockets.connect(
                    url,
                    extra_headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
                    compression=None,
                    ping_interval=20,
                    ping_timeout=20
                )
                reader_task = asyncio.create_task(vapi_reader())
                continue

            if t == "audio":
                if not vapi_ws:
                    continue
                a64 = get_audio_b64(msg)
                if not a64:
                    continue
                try:
                    raw = base64.b64decode(a64, validate=True)
                except:
                    continue
                try:
                    await vapi_ws.send(raw)
                except:
                    await close_all()
                    break
                continue

            if t in ["interrupt", "clear"]:
                continue

    except WebSocketDisconnect:
        await close_all()
    except:
        await close_all()
