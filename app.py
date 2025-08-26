import os, json, base64, asyncio, audioop
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
        if r.status_code >= 300:
            raise RuntimeError(r.text)
        j = r.json()
        return j.get("websocketCallUrl") or j.get("websocketUrl") or (j.get("transport") or {}).get("websocketCallUrl")

def parse_start(msg):
    enc = None
    rate = 8000
    ch = 1
    d = msg.get("data") if isinstance(msg.get("data"), dict) else {}
    enc = d.get("encoding") or msg.get("encoding") or ""
    rate = int(d.get("sample_rate") or msg.get("sample_rate") or rate)
    ch = int(d.get("channels") or msg.get("channels") or ch)
    return (enc.lower(), rate, ch)

def frejun_audio_b64(msg):
    if isinstance(msg.get("data"), dict) and "audio_b64" in msg["data"]:
        return msg["data"]["audio_b64"]
    if "audio_b64" in msg:
        return msg["audio_b64"]
    return None

def l16_be_to_le(b):
    if not b:
        return b
    return b[1::2] + b[0::2]

def l16_le_to_be(b):
    if not b:
        return b
    return b[1::2] + b[0::2]

def to_vapi_bytes(b, enc, rate_in):
    if not b:
        return b
    if enc.startswith("audio/pcmu") or "ulaw" in enc:
        lin = audioop.ulaw2lin(b, 2)
        out, _ = audioop.ratecv(lin, 2, 1, 8000, 16000, None)
        return out
    if enc.startswith("audio/l16"):
        le = l16_be_to_le(b)
        if rate_in != 16000:
            out, _ = audioop.ratecv(le, 2, 1, rate_in, 16000, None)
            return out
        return le
    le = l16_be_to_le(b)
    out, _ = audioop.ratecv(le, 2, 1, rate_in, 16000, None)
    return out

def from_vapi_bytes(b, enc, rate_out):
    if not b:
        return b
    if enc.startswith("audio/pcmu") or "ulaw" in enc:
        down, _ = audioop.ratecv(b, 2, 1, 16000, 8000, None)
        ul = audioop.lin2ulaw(down, 2)
        return ul
    if enc.startswith("audio/l16"):
        if rate_out != 16000:
            down, _ = audioop.ratecv(b, 2, 1, 16000, rate_out, None)
        else:
            down = b
        be = l16_le_to_be(down)
        return be
    down, _ = audioop.ratecv(b, 2, 1, 16000, rate_out, None)
    be = l16_le_to_be(down)
    return be

@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    vapi_ws = None
    reader_task = None
    next_chunk_id = 1
    enc = "audio/l16"
    rate = 8000
    ch = 1

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
                    out = from_vapi_bytes(msg, enc, rate)
                    b64 = base64.b64encode(out).decode()
                    await ws.send_text(json.dumps({"type":"audio","audio_b64":b64,"chunk_id":next_chunk_id}))
                    next_chunk_id += 1
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
                enc, rate, ch = parse_start(msg)
                url = await create_vapi_call()
                vapi_ws = await websockets.connect(url, extra_headers={"Authorization": f"Bearer {VAPI_API_KEY}"}, compression=None, ping_interval=20, ping_timeout=20)
                reader_task = asyncio.create_task(vapi_reader())
                continue
            if t == "audio":
                if not vapi_ws:
                    continue
                a64 = frejun_audio_b64(msg)
                if not a64:
                    continue
                try:
                    raw = base64.b64decode(a64, validate=True)
                except:
                    continue
                pcm16 = to_vapi_bytes(raw, enc, rate)
                try:
                    vapi_ws.send(pcm16)
                except:
                    await close_all()
                    break
                continue
            if t in ["interrupt","clear"]:
                continue
    except WebSocketDisconnect:
        await close_all()
    except:
        await close_all()
