import os, json, base64, asyncio
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

@app.get("/")
async def root_get():
    return PlainTextResponse("ok")

@app.head("/")
async def root_head():
    return PlainTextResponse("")

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
                    b64 = base64.b64encode(msg).decode()
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

            t = msg.get("type")
            if t == "start":
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
                                "audioFormat": {"format": "pcm_s16le", "container": "raw", "sampleRate": 16000}
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
                vapi_ws = await websockets.connect(
                    ws_url,
                    extra_headers={"Authorization": f"Bearer {api_key}"},
                    compression=None,
                    ping_interval=20,
                    ping_timeout=20
                )
                reader_task = asyncio.create_task(vapi_reader())
                continue

            if t == "audio":
                if vapi_ws is None:
                    continue
                b64 = None
                if isinstance(msg.get("data"), dict) and "audio_b64" in msg["data"]:
                    b64 = msg["data"]["audio_b64"]
                elif "audio_b64" in msg:
                    b64 = msg["audio_b64"]
                if not b64:
                    continue
                try:
                    buf = base64.b64decode(b64, validate=True)
                except:
                    continue
                try:
                    await vapi_ws.send(buf)
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
