"""Raion's server: WhatsApp webhook (messages -> wa_agent, calls both ways), the voice-call loop (all Sarvam: STT ->
Sarvam LLM brain -> TTS; real work goes to the WhatsApp agent) and the local dashboard."""
import asyncio, base64, fractions, hashlib, hmac, json, os, time
from pathlib import Path

import aiohttp
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame, AudioResampler

ROOT = Path(__file__).parent


def load_env():
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env()
from brain import Brain, SarvamBrain  # noqa: E402  (reads CLAUDE_MODEL from env)
import pymupdf  # noqa: E402
import printing, wa_agent  # noqa: E402  (read the env loaded above)

WA_TOKEN = os.environ["WA_TOKEN"]
WA_PHONE_ID = os.environ["WA_PHONE_NUMBER_ID"]
GRAPH = f"https://graph.facebook.com/{os.environ.get('WA_GRAPH_VERSION', 'v23.0')}"
VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("WA_APP_SECRET", "")
OLD_WEBHOOK = os.environ.get("OLD_WEBHOOK_URL", "")
SARVAM_KEY = os.environ.get("SARVAM_API_KEY", "")
STT_MODEL = os.environ.get("SARVAM_STT_MODEL", "saaras:v4")
TTS_MODEL = os.environ.get("SARVAM_TTS_MODEL", "bulbul:v4-flash")
# calls have their own soft voice; WhatsApp voice notes keep SARVAM_SPEAKER / SARVAM_PACE
TTS_SPEAKER = os.environ.get("SARVAM_CALL_SPEAKER", "ratan_hi_latenight_warm")
TTS_PACE = float(os.environ.get("SARVAM_CALL_PACE", "1.1"))  # voice speed, 0.5-2.0
PORT = int(os.environ.get("PORT", "8080"))

# ponytail: Sarvam pay-as-you-go list prices + fixed USD->INR; a demo meter, not billing
STT_INR_PER_MIN, TTS_INR_PER_1K_CHARS, USD_INR = 0.49, 2.0, 88
GREETING = "नमस्ते! मैं Raion बोल रहा हूँ। बताइए, क्या करना है?"
OPENER = "नमस्ते, मैं Raion बोल रहा हूँ।"  # said the moment they pick up a reminder call, while the brain words the rest
# calls need a fast talker: the call brain only talks and hands real work to the WhatsApp agent (AGENT_MODEL).
# A Claude name here (CALL_MODEL=haiku) puts calls back on Claude.
CALL_MODEL = os.environ.get("CALL_MODEL") or "sarvam-105b-conversations"

# ---------------- shared state + dashboard broadcast ----------------
clients: set[web.WebSocketResponse] = set()
DOC_FIELDS = ("ref", "date", "to", "subject", "body", "sign")
BLANK_LETTER = {f: "" for f in DOC_FIELDS}
doc = dict(BLANK_LETTER)
doc_history: list[dict] = []  # for undo


def state():
    """What the brain sees each turn."""
    return {"letter": doc}


async def broadcast(msg):
    data = json.dumps(msg, ensure_ascii=False)
    for ws in list(clients):
        try:
            await ws.send_str(data)
        except Exception:
            clients.discard(ws)


async def apply(action: dict):
    """One letter action from the brain (anything else is ignored)."""
    global doc
    a = dict(action)
    kind = a.pop("op", "")
    if not kind.startswith("letter_"):
        return
    if kind == "letter_undo":
        if doc_history:
            doc = doc_history.pop()
    else:
        doc_history.append(dict(doc))
        if kind == "letter_new":
            doc = dict(BLANK_LETTER)
        elif kind == "letter_set" and a.get("field") in DOC_FIELDS:
            doc[a["field"]] = a.get("text", "")
        elif kind == "letter_append":
            body = doc["body"].rstrip()
            doc["body"] = (body + ("\n" if a.get("new_paragraph") else " ") if body else "") + a.get("text", "")
        elif kind == "letter_replace" and a.get("find"):
            # last occurrence, across fields from the bottom up: corrections are usually about what was just said
            for f in reversed(DOC_FIELDS):
                i = doc[f].rfind(a["find"])
                if i != -1:
                    doc[f] = doc[f][:i] + a.get("with", "") + doc[f][i + len(a["find"]):]
                    break
        elif kind != "letter_print":
            doc_history.pop()
            return
    await broadcast({"type": "tool", "name": kind, "args": a})
    await broadcast({"type": "letter", "doc": doc, "print": kind == "letter_print"})


# ---------------- audio: TTS -> caller ----------------
class OutTrack(MediaStreamTrack):
    """Plays 24kHz mono PCM16 from the buffer, silence when empty. aiortc's Opus encoder resamples."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.buf = bytearray()
        self.pts = 0
        self.start = None

    async def recv(self):
        n = 480  # 20ms @ 24k
        if self.start is None:
            self.start = time.time()
        wait = self.start + self.pts / 24000 - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        chunk = bytes(self.buf[:n * 2]).ljust(n * 2, b"\0")
        del self.buf[:n * 2]
        frame = AudioFrame(format="s16", layout="mono", samples=n)
        frame.planes[0].update(chunk)
        frame.sample_rate, frame.pts, frame.time_base = 24000, self.pts, fractions.Fraction(1, 24000)
        self.pts += n
        return frame


def strip_wav(b: bytes) -> bytes:
    if b[:4] == b"RIFF" and (i := b.find(b"data")) != -1:
        return b[i + 8:]
    return b


# ---------------- one call session ----------------
async def run_session(*args, **kw):  # counts live calls, so watchdog.ps1 never restarts Raion mid-call
    wa_agent.CALLS += 1
    wa_agent.busy_mark()
    try:
        await call_session(*args, **kw)
    finally:
        wa_agent.CALLS -= 1
        wa_agent.busy_mark()


async def call_session(pc: RTCPeerConnection, in_track, out: OutTrack, label: str, num=None, reminder=None,
                       picked_up: asyncio.Event | None = None, call_id=None):
    """One call. num: the WhatsApp number (None = browser test). reminder + picked_up: a call we placed, which starts
    talking when they pick up."""
    if not SARVAM_KEY:
        print("SARVAM_API_KEY missing in .env")
        return
    await broadcast({"type": "call", "status": "started", "from": label})
    started, tts_chars = time.time(), 0
    muted = False  # set on barge-in so leftover TTS audio of the interrupted reply is dropped
    hdr = {"Api-Subscription-Key": SARVAM_KEY}
    prompt = await asyncio.to_thread(wa_agent.call_prompt, num, reminder)
    brain = (SarvamBrain(CALL_MODEL, prompt, SARVAM_KEY) if CALL_MODEL.startswith("sarvam")
             else Brain(model=CALL_MODEL, system_file=prompt))
    said: list[tuple[str, str]] = []  # the conversation, for their WhatsApp chat afterwards

    def cost():
        return round((time.time() - started) / 60 * STT_INR_PER_MIN + tts_chars / 1000 * TTS_INR_PER_1K_CHARS
                     + brain.cost_usd * USD_INR, 2)

    stt_url = ("wss://api.sarvam.ai/speech-to-text/ws?" + "&".join(f"{k}={v}" for k, v in {
        "language-code": "hi-IN", "model": STT_MODEL, "mode": "codemix", "sample_rate": 16000,
        "input_audio_codec": "pcm_s16le", "vad_signals": "true"}.items()))
    tts_url = f"wss://api.sarvam.ai/text-to-speech/ws?model={TTS_MODEL}"

    async with aiohttp.ClientSession() as http, \
            http.ws_connect(stt_url, headers=hdr, heartbeat=20) as stt, \
            http.ws_connect(tts_url, headers=hdr, heartbeat=20) as tts:
        await tts.send_json({"type": "config", "data": {
            "language_code": "hi-IN", "speaker": TTS_SPEAKER, "speech_sample_rate": 24000,
            "output_audio_codec": "linear16", "pace": TTS_PACE, "min_buffer_size": 30, "max_chunk_length": 150}})

        turn = 0

        async def speak(text):
            nonlocal tts_chars, muted, turn
            if not any(c.isalpha() for c in text):  # Sarvam rejects letter-less text and closes the socket
                return
            muted = False
            turn += 1
            tts_chars += len(text)
            said.append(("Raion", text))
            await broadcast({"type": "transcript", "role": "ai", "text": text, "item": f"ai{turn}"})
            await tts.send_json({"type": "text", "data": {"text": text}})
            await tts.send_json({"type": "flush"})

        async def pump_in():
            rs, pending = AudioResampler(format="s16", layout="mono", rate=16000), bytearray()
            while True:
                frame = await in_track.recv()
                for f in rs.resample(frame):
                    pending.extend(bytes(f.planes[0])[: f.samples * 2])
                if len(pending) >= 3200:  # send ~100ms at a time
                    await stt.send_json({"audio": {"data": base64.b64encode(pending).decode(),
                                                   "sample_rate": 16000, "encoding": "audio/wav"}})
                    pending.clear()

        heard: asyncio.Queue[str] = asyncio.Queue()

        async def pump_stt():
            nonlocal muted
            async for msg in stt:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                ev = json.loads(msg.data)
                data = ev.get("data") or {}
                if ev.get("type") == "events" and data.get("signal_type") == "START_SPEECH":
                    muted = True  # barge-in: stop talking the moment the caller speaks
                    out.buf.clear()
                    await broadcast({"type": "speaking", "who": "user"})
                elif ev.get("type") == "data" and data.get("transcript", "").strip():
                    text = data["transcript"].strip()
                    said.append(("Them", text))
                    await broadcast({"type": "transcript", "role": "user", "text": text})
                    if picked_up and not picked_up.is_set():  # they're talking: picked up even if no status came yet
                        picked_up.set()
                    heard.put_nowait(text)
                elif ev.get("type") == "error":
                    print("stt error:", data)
                    await broadcast({"type": "error", "message": f"STT: {data}"})

        async def pump_tts():
            async for msg in tts:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                ev = json.loads(msg.data)
                if ev.get("type") == "audio" and not muted:
                    out.buf.extend(strip_wav(base64.b64decode(ev["data"]["audio"])))
                elif ev.get("type") == "error":
                    print("tts error:", ev.get("data"))
                    await broadcast({"type": "error", "message": f"TTS: {ev.get('data')}"})

        async def think():
            await brain.ready
            while True:
                text = await heard.get()
                while not heard.empty():  # they kept talking while we thought: take it all at once
                    text += " " + heard.get_nowait()
                try:
                    r = await brain.ask(text, state())
                except Exception as e:
                    print("brain error:", repr(e))
                    await broadcast({"type": "error", "message": f"Brain: {e}"})
                    continue
                hang = False
                for a in r["actions"]:
                    if a.get("op") in ("whatsapp", "build") and a.get("text") and num:
                        await broadcast({"type": "tool", "name": a["op"], "args": a})
                        await wa_agent.from_call(num, str(a["text"]), build=a["op"] == "build")
                    elif a.get("op") == "hangup":
                        hang = True
                    else:
                        await apply(a)
                await speak(r["say"])
                await broadcast({"type": "cost", "inr": cost()})
                if hang:  # let the goodbye play out, then end the call
                    await asyncio.sleep(2)
                    while out.buf:
                        await asyncio.sleep(0.2)
                    await asyncio.sleep(0.5)
                    if call_id:
                        await wa_action(call_id, "terminate")
                    return

        async def greet():
            if picked_up is None:
                await speak(GREETING)
                return
            await picked_up.wait()
            await speak(OPENER)
            heard.put_nowait("[They picked up the reminder call. You already said: " + OPENER + " Now tell the reminder.]")

        closed = asyncio.Event()

        @pc.on("connectionstatechange")
        async def _():
            if pc.connectionState in ("failed", "closed"):
                closed.set()

        brain.ready = asyncio.create_task(brain.start())
        greeter = asyncio.create_task(greet())
        tasks = [asyncio.create_task(c) for c in (pump_in(), pump_stt(), pump_tts(), think(), closed.wait())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks + [greeter]:
            t.cancel()
    await brain.close()
    await pc.close()
    await broadcast({"type": "call", "status": "ended", "cost": cost()})
    if num and len(said) > 1:  # more than our own greeting: the call goes into their chat
        await wa_agent.call_ended(num, said, reminder)


async def answer_offer(sdp: str, label: str, on_answer, num=None):
    """Shared by WhatsApp and browser: build the peer, answer the offer, start the session."""
    pc, out = RTCPeerConnection(), OutTrack()
    got = asyncio.get_running_loop().create_future()
    pc.on("track", lambda tr: tr.kind == "audio" and not got.done() and got.set_result(tr))
    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
    pc.addTrack(out)
    await pc.setLocalDescription(await pc.createAnswer())
    if not await on_answer(pc.localDescription.sdp):
        await pc.close()
        return pc
    asyncio.create_task(run_session(pc, await got, out, label, num))
    return pc


# ---------------- WhatsApp ----------------
wa_calls: dict[str, RTCPeerConnection] = {}
outgoing: dict[str, dict] = {}  # calls we placed, until they end


async def wa_call(body: dict) -> dict | None:
    async with aiohttp.ClientSession() as s, s.post(f"{GRAPH}/{WA_PHONE_ID}/calls",
                                                    json={"messaging_product": "whatsapp", **body},
                                                    headers={"Authorization": f"Bearer {WA_TOKEN}"}) as r:
        text = await r.text()
        print(f"wa {body.get('action')}: {r.status} {text[:300]}")
        return json.loads(text) if r.status == 200 else None


async def wa_action(call_id, action, sdp=None):
    return await wa_call({"call_id": call_id, "action": action,
                          **({"session": {"sdp_type": "answer", "sdp": sdp}} if sdp else {})}) is not None


async def handle_wa_call(call):
    cid, num = call["id"], call.get("from", "")
    if num not in wa_agent.ALLOWED or wa_agent.in_maintenance():  # only the office's own people, never 1-2 AM
        await wa_action(cid, "reject")
        if num in wa_agent.ALLOWED:
            await wa_agent.maintenance_note(num)
        return
    await wa_agent.call_started(num)

    async def accept(sdp):
        return await wa_action(cid, "pre_accept", sdp) and await wa_action(cid, "accept", sdp)

    try:
        wa_calls[cid] = await answer_offer(call["session"]["sdp"], "+" + num, accept, num)
    except Exception as e:
        print("call setup failed:", repr(e))
        await wa_action(cid, "terminate")


async def call_out(num: str, reminder: str) -> bool:
    """Call them on WhatsApp to say a reminder, then talk (wa_agent.CALLER). True once they pick up; False if the
    call couldn't be placed or wasn't answered (the reminder then goes as a message)."""
    pc, out = RTCPeerConnection(), OutTrack()
    got = asyncio.get_running_loop().create_future()
    pc.on("track", lambda tr: tr.kind == "audio" and not got.done() and got.set_result(tr))
    pc.addTrack(out)
    await pc.setLocalDescription(await pc.createOffer())  # aiortc gathers ICE here: the offer is complete
    r = await wa_call({"to": num, "action": "connect", "session": {"sdp_type": "offer", "sdp": pc.localDescription.sdp}})
    if not r:
        await pc.close()
        return False
    cid = r["calls"][0]["id"]
    o = outgoing[cid] = {"pc": pc, "out": out, "got": got, "num": num, "reminder": reminder,
                         "picked": asyncio.Event(), "ended": asyncio.Event()}
    wa_calls[cid] = pc
    waits = [asyncio.create_task(o["picked"].wait()), asyncio.create_task(o["ended"].wait())]
    await asyncio.wait(waits, timeout=90, return_when=asyncio.FIRST_COMPLETED)  # ponytail: ~ WhatsApp's ring time
    for t in waits:
        t.cancel()
    if o["picked"].is_set():
        await wa_agent.call_started(num)
        return True
    if not o["ended"].is_set():
        await wa_action(cid, "terminate")
    outgoing.pop(cid, None)
    wa_calls.pop(cid, None)
    await pc.close()
    return False


async def call_connected(call):
    """WhatsApp's answer to a call we placed: media can start; talking waits for the pickup."""
    o = outgoing.get(call["id"])
    if not o:
        return
    try:
        await o["pc"].setRemoteDescription(RTCSessionDescription(sdp=call["session"]["sdp"], type="answer"))
        track = await asyncio.wait_for(o["got"], 10)
    except Exception as e:
        print("outgoing call setup failed:", repr(e))
        await wa_action(call["id"], "terminate")
        o["ended"].set()
        return
    asyncio.create_task(run_session(o["pc"], track, o["out"], "→ +" + o["num"], o["num"], o["reminder"],
                                    o["picked"], call["id"]))


async def webhook_get(req):
    q = req.query
    if q.get("hub.mode") == "subscribe" and VERIFY_TOKEN and q.get("hub.verify_token") == VERIFY_TOKEN:
        return web.Response(text=q.get("hub.challenge", ""))
    return web.Response(status=403)


async def webhook_post(req):
    raw = await req.read()
    sig = req.headers.get("X-Hub-Signature-256", "")
    if APP_SECRET and not hmac.compare_digest(sig, "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()):
        return web.Response(status=401)
    data = json.loads(raw or b"{}")
    changes = [c for e in data.get("entry", []) for c in e.get("changes", [])]
    unhandled = False  # anything the agent didn't take (other senders, statuses...) may go to the old webhook
    for ch in changes:
        value = ch.get("value", {})
        if ch.get("field") == "calls":
            for call in value.get("calls", []):
                if call.get("event") == "connect" and call.get("direction") == "BUSINESS_INITIATED":
                    asyncio.create_task(call_connected(call))
                elif call.get("event") == "connect":
                    asyncio.create_task(handle_wa_call(call))
                elif call.get("event") == "terminate":
                    if o := outgoing.pop(call["id"], None):
                        o["ended"].set()
                    if pc := wa_calls.pop(call["id"], None):
                        asyncio.create_task(pc.close())
            for st in value.get("statuses", []):  # calls we placed: RINGING, ACCEPTED, REJECTED
                if (o := outgoing.get(st.get("id"))) and st.get("status") == "ACCEPTED":
                    o["picked"].set()
                elif o and st.get("status") == "REJECTED":
                    o["ended"].set()
            continue
        names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts", [])}
        for m in value.get("messages", []):
            if not await wa_agent.handle_incoming(m, names.get(m.get("from"), "")):
                unhandled = True
        if not value.get("messages"):
            unhandled = True
    if OLD_WEBHOOK and unhandled:
        # forward untouched (same body + signature) so the existing integration keeps working
        asyncio.create_task(forward(raw, sig))
    return web.Response(text="ok")


async def forward(raw, sig):
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(OLD_WEBHOOK, data=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    except Exception as e:
        print("forward failed:", repr(e))


# ---------------- dashboard (local only) ----------------
@web.middleware
async def local_only(req, handler):
    # the tunnel adds these headers; only /webhook may be reached from the internet
    if req.path != "/webhook" and ("Cf-Connecting-IP" in req.headers or "X-Forwarded-For" in req.headers):
        return web.Response(status=403)
    return await handler(req)


async def index(_):
    return web.FileResponse(ROOT / "dashboard.html")


async def ws_handler(req):
    ws = web.WebSocketResponse()
    await ws.prepare(req)
    clients.add(ws)
    await ws.send_str(json.dumps({"type": "letter", "doc": doc}, ensure_ascii=False))
    async for _ in ws:
        pass
    clients.discard(ws)
    return ws


async def browser_offer(req):
    body = await req.json()
    answer = {}

    async def keep(sdp):
        answer["sdp"] = sdp
        return True

    await answer_offer(body["sdp"], "Browser mic", keep)
    return web.json_response({"sdp": answer["sdp"], "type": "answer"})


async def mem_file(req):
    """Previews and received files for the dashboard (local only, inside memory/)."""
    path = (wa_agent.MEM / req.match_info["path"]).resolve()
    if not path.is_relative_to(wa_agent.MEM.resolve()) or not path.is_file():
        return web.Response(status=404)
    return web.FileResponse(path)


async def printers_get(_):
    try:
        return web.json_response(await asyncio.to_thread(printing.list_printers))
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def printers_set(req):
    """{"name": "...", "alias": "Office"} and/or {"name": "...", "default": true}"""
    body = await req.json()
    s = printing.load_settings()
    if "alias" in body:
        s.setdefault("aliases", {})[body["name"]] = body["alias"].strip()
    if body.get("default"):
        s["default_printer"] = body["name"]
    printing.save_settings(s)
    return await printers_get(req)


async def printers_test(req):
    name = (await req.json())["name"]
    pdf = ROOT / ".runtime" / "test_page.pdf"
    pdf.parent.mkdir(exist_ok=True)
    with pymupdf.open() as d:
        pg = d.new_page()
        pg.insert_text((72, 90), "MNA Raion - printer test", fontsize=22)
        pg.insert_text((72, 130), f"{name}  ·  {time.strftime('%d/%m/%Y %H:%M')}", fontsize=13)
        d.save(pdf)
    out = str(ROOT / ".runtime" / "test_print_output.pdf") if "print to pdf" in name.lower() else None
    try:
        await asyncio.to_thread(printing.print_pdf, pdf, name, 1, out, 150)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def on_startup(_):
    wa_agent.ui, wa_agent.CALLER = broadcast, call_out
    asyncio.create_task(wa_agent.background())


app = web.Application(middlewares=[local_only])
app.on_startup.append(on_startup)
app.add_routes([web.get("/", index), web.get("/ws", ws_handler), web.post("/offer", browser_offer),
                web.get("/webhook", webhook_get), web.post("/webhook", webhook_post),
                web.get("/mem/{path:.+}", mem_file), web.get("/api/printers", printers_get),
                web.post("/api/printers", printers_set), web.post("/api/printers/test", printers_test)])

if __name__ == "__main__":
    print(f"Dashboard: http://localhost:{PORT}")
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)  # only this PC (ngrok brings WhatsApp in)
