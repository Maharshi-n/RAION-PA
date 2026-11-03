"""The call's brain: Sarvam's own LLM (SarvamBrain, one HTTPS request per turn) or Claude (Brain: one persistent
`claude -p` process, stream-json in/out; the WhatsApp agent uses Brain too)."""
import asyncio, json, os, re, shutil
from datetime import datetime
from pathlib import Path

import aiohttp

# "haiku" = latest Haiku your installed CLI knows
MODEL = os.environ.get("CLAUDE_MODEL", "haiku")
# empty dir so no project CLAUDE.md / settings get picked up
CWD = Path(__file__).parent / ".brain"
CWD.mkdir(exist_ok=True)

SYSTEM = """You are Raion, the office operator of Maa Narmada Group of Institutes, Dhamnod, on a live WhatsApp phone
call. They speak Hindi, English or Hinglish. Each turn you receive what they said (speech-to-text, may have small
errors), the time, and the letter on the office screen. Who is on the call, what you know about them and why the call
happened are at the end of these instructions.

Reply with ONLY one JSON object, no markdown, no other text:
{"actions": [...], "say": "..."}

Actions:
  {"op":"whatsapp","text":T}   hand work to Raion on WhatsApp, who has the office's files, tools and the web:
                               documents, lists, finding or checking things, messages, reminders and automations
                               ("kal 5 baje phir call karke yaad dilana"), anything you can't answer from what you know
                               here. T = the full request with every detail they said. Then say it will come on WhatsApp.
  {"op":"build","text":T}      admins only: build a website, web app or dashboard and put it live. Raion's builder makes
                               it on the college website, publishes it and sends the link on WhatsApp. T = everything
                               they want in it (what it shows or saves, for whom). Ask first only if what to build is
                               unclear. Then say the link will come on WhatsApp in a few minutes.
  {"op":"hangup"}              end the call after your "say": when they say bye / ok thanks / bas itna hi, or the
                               call's purpose is done and they want nothing more.
  Office-screen letter (only when they want to type a letter on the office computer's screen during the call; a letter
  to keep, send or print properly goes to whatsapp):
  {"op":"letter_new"}                                  blank letterhead
  {"op":"letter_set","field":F,"text":T}               F: ref date to subject body sign (replaces that field)
  {"op":"letter_append","text":T,"new_paragraph":B}    add dictated text to the end of the body
  {"op":"letter_replace","find":X,"with":Y}            correct text; X must be copied exactly from the letter
  {"op":"letter_undo"}                                 undo the last letter change
  {"op":"letter_print"}                                print / save as PDF

Reminder calls: if you called them for a reminder, the first message tells you they picked up and that you already
said namaste and your name. Then tell the reminder clearly in your own words (what, and any time or detail it has)
and ask if they need anything else. Repeat it if they ask. A request about it (snooze, call again later, send it in
writing) goes to whatsapp.

Letter writing rules:
- Write the letter in formal, correct Hindi (Devanagari) unless the caller asks for English. Turn casual dictation
  into proper letter language (e.g. "likho mahoday" -> "महोदय,"), but keep the caller's meaning and names.
- Speech-to-text gives Roman Hinglish; every Hindi word you put in the letter (letter_set, letter_append, the "with"
  of letter_replace) is in Devanagari, never Roman: "aapko soochit kiya jaata hai" -> "आपको सूचित किया जाता है",
  "mitron ko saathiyon karo" -> letter_replace "मित्रों" with "साथियों".
- "aaj ki date" = today's date in DD/MM/YYYY. "to" is the recipient block (lines separated by \\n).
- Corrections ("nahi nahi, isko saathiyon karo", "ye line hata do") refer to what was written most recently:
  use letter_replace with the exact existing text (or letter_set for a whole field). Never retype the whole body
  for a small fix.
- "Sign" defaults to "निदेशक\\nमाँ नर्मदा ग्रुप ऑफ इंस्टिट्यूट्स" if asked to add a signature without details.

"say": what you speak, in natural Hinglish. Every Hindi word in Devanagari ("काम हो गया", never "kaam ho gaya"); English
words can stay in Latin ("dashboard का link WhatsApp पर आ जाएगा"). Roman Hindi sounds wrong when read aloud. It is read
aloud by a male Hindi TTS voice, so use masculine first-person forms (बता रहा हूँ) and neutral "aap" forms for them
(don't assume their gender: say sir or madam only if their profile shows which, otherwise just "aap"). Usually one short sentence (max ~15 words); up to 3 short sentences when telling a
reminder or answering a question. Talk naturally; no lists of your abilities, no links, no long number strings.
Don't read the whole letter aloud; just confirm briefly (it's on the office computer's screen, not on WhatsApp). If you didn't understand, use no actions and ask briefly."""


def claude_args(model, tools="", cwd_dirs=(), deny=(), permission_mode=None, allow=(), effort=None, mcp=None,
                agents=None):
    """Common `claude -p` flags: no user/project settings, hooks or plugins; only the tools we allow, our own MCP
    servers (mcp: {"name": {"command": ..., "args": [...], "env": {...}}}) and subagent types (agents: {"name":
    {"description": ..., "prompt": ..., "tools": [...]}}, used through the Agent tool).
    effort (low/medium/high) turns thinking on at that level; without it thinking is off (fast voice calls)."""
    args = ["-p", "--model", model, "--tools", tools, "--setting-sources", "",
            "--settings", json.dumps({"disableAllHooks": True, "alwaysThinkingEnabled": bool(effort)}),
            "--strict-mcp-config", "--no-session-persistence"]
    if mcp:
        args += ["--mcp-config", json.dumps({"mcpServers": mcp})]
    if agents:
        args += ["--agents", json.dumps(agents)]
    if effort:
        args += ["--effort", effort]
    for d in cwd_dirs:
        args += ["--add-dir", str(d)]
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    if allow:  # tools that would otherwise ask for permission (and so be refused in -p mode)
        args += ["--allowedTools", *allow]
    if deny:
        args += ["--disallowedTools", *deny]
    return args


def deny_writes(path: Path, recursive=True) -> list:
    """Permission rule blocking Write/Edit on a path (absolute rule form: //E/dir/**). Edit rules cover every
    file-editing tool (Write(...) rules aren't matched), and they hold even in bypassPermissions mode."""
    p = "//" + Path(path).resolve().as_posix().replace(":", "", 1) + ("/**" if recursive else "")
    return [f"Edit({p})"]


def deny_reads(path: Path) -> list:
    """Permission rule blocking Read (and Grep/Glob) under a folder."""
    return [f"Read(//{Path(path).resolve().as_posix().replace(':', '', 1)}/**)"]


ENV = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "MAX_THINKING_TOKENS": "0",
       "BASH_DEFAULT_TIMEOUT_MS": "600000", "BASH_MAX_TIMEOUT_MS": "3600000"}  # video renders can take minutes
ENV_THINKING = {k: v for k, v in ENV.items() if k != "MAX_THINKING_TOKENS"}  # for brains given an effort level


class Brain:
    """Call brain by default (fast, no tools). The WhatsApp agent passes its own sandbox + system prompt file."""

    def __init__(self, model=None, cwd=CWD, system=SYSTEM, system_file=None, **sandbox):
        self.proc = None
        self.cost_usd = 0.0  # cumulative, from the CLI's result events
        self.last_id = None
        self.model, self.cwd, self.system, self.system_file, self.sandbox = model or MODEL, cwd, system, system_file, sandbox

    async def start(self):
        exe = shutil.which("claude") or "claude"
        prompt = ["--system-prompt-file", str(self.system_file)] if self.system_file else ["--system-prompt", self.system]
        self.proc = await asyncio.create_subprocess_exec(
            exe, *claude_args(self.model, **self.sandbox), *prompt,
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            cwd=self.cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=2**24, env=ENV_THINKING if self.sandbox.get("effort") else ENV,
        )

    async def send(self, text: str):
        """Agent mode: queue a user message. Sent mid-turn, the CLI hands it to the model at its next step (like
        typing while Claude Code works); otherwise it starts a turn. Read what happens from events()."""
        line = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(line, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def interrupt(self):
        """Stop the running turn (its tool call is cancelled); the conversation stays."""
        self.proc.stdin.write(b'{"type":"control_request","request_id":"stop","request":{"subtype":"interrupt"}}\n')
        await self.proc.stdin.drain()

    async def events(self):
        """Every event the CLI prints, until the process ends."""
        while raw := await self.proc.stdout.readline():
            ev = json.loads(raw)
            if ev.get("type") == "result":
                self.cost_usd = ev.get("total_cost_usd") or self.cost_usd
            yield ev

    @property
    def alive(self):
        return self.proc is not None and self.proc.returncode is None

    async def ask(self, heard: str, screen_state: dict) -> dict:
        line = {"type": "user", "message": {"role": "user", "content": turn(heard) + screen(screen_state)}}
        self.proc.stdin.write((json.dumps(line, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                raise RuntimeError("claude process exited")
            ev = json.loads(raw)
            if ev.get("type") == "result":  # usually the previous turn's, arriving ~2s after its answer
                self.cost_usd = ev.get("total_cost_usd") or self.cost_usd
                if ev.get("is_error"):
                    raise RuntimeError(str(ev.get("result")))
            elif ev.get("type") == "assistant":
                # act on the answer as soon as it exists instead of waiting for the result event.
                # One reply can arrive as several events with the same id (thinking block, then text),
                # and the CLI can repeat the previous reply late: take the first text per id only.
                text = "".join(b.get("text", "") for b in ev["message"].get("content", []) if b.get("type") == "text")
                if text and ev["message"].get("id") != self.last_id:
                    self.last_id = ev["message"].get("id")
                    return parse(text)

    async def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()


SARVAM_CHAT = "https://api.sarvam.ai/v1/chat/completions"
# ponytail: Sarvam 105B list prices (₹15 in / ₹60 out per 1M tokens) at ₹88 a dollar, for the call's cost meter
SARVAM_USD_IN, SARVAM_USD_OUT = 15 / 88 / 1e6, 60 / 88 / 1e6


class SarvamBrain:
    """Call brain on Sarvam's LLM (sarvam-105b-conversations: made for voice agents, thinking off for speed).
    Same start/ask/close as Brain; the conversation is kept here and sent whole each turn."""

    def __init__(self, model, system_file, key):
        self.model, self.key, self.http, self.cost_usd = model, key, None, 0.0
        self.messages = [{"role": "system", "content": Path(system_file).read_text(encoding="utf-8")}]

    async def start(self):
        self.http = aiohttp.ClientSession(headers={"api-subscription-key": self.key},
                                          timeout=aiohttp.ClientTimeout(total=20))

    async def ask(self, heard: str, screen_state: dict) -> dict:
        # only the newest turn carries the letter on screen, so a long letter doesn't fill the 32K context every turn
        # ponytail: a very long call can still outgrow it; drop the oldest turns if that ever shows up
        self.messages.append({"role": "user", "content": turn(heard) + screen(screen_state)})
        try:
            async with self.http.post(SARVAM_CHAT, json={
                    "model": self.model, "messages": self.messages, "reasoning_effort": None, "temperature": 0.3,
                    "max_tokens": 800, "response_format": {"type": "json_object"}}) as r:
                d = await r.json(content_type=None)
                if r.status != 200:
                    raise RuntimeError(f"Sarvam chat {r.status}: {d}")
        except BaseException:
            self.messages.pop()
            raise
        self.messages[-1]["content"] = turn(heard)
        text = d["choices"][0]["message"].get("content") or ""
        self.messages.append({"role": "assistant", "content": text})
        u = d.get("usage") or {}
        self.cost_usd += u.get("prompt_tokens", 0) * SARVAM_USD_IN + u.get("completion_tokens", 0) * SARVAM_USD_OUT
        return parse(text)

    async def close(self):
        if self.http:
            await self.http.close()


def turn(heard: str) -> str:
    return f"They said: {heard}\nNow: {datetime.now():%A %d/%m/%Y %H:%M}"


def screen(screen_state: dict) -> str:
    return f"\nScreen: {json.dumps(screen_state, ensure_ascii=False, separators=(',', ':'))}"


def parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)  # tolerate stray prose / code fences around the JSON
    try:
        out = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        out = {}
    return {"actions": out.get("actions") or [], "say": out.get("say") or ""}


if __name__ == "__main__":
    async def demo():
        import time
        assert parse('```json\n{"actions":[{"op":"clear"}],"say":"हो गया"}\n```')["actions"] == [{"op": "clear"}]
        assert parse("garbage")["say"] == ""
        # drive the real server-side state so the brain sees what the dashboard would show
        import app
        f = CWD / "demo_prompt.md"
        f.write_text(SYSTEM + "\n\n# On the call: Manoj Nahar, admin (principal/director)\n# Why: they called you.",
                     encoding="utf-8")
        b = SarvamBrain(app.CALL_MODEL, f, app.SARVAM_KEY) if app.CALL_MODEL.startswith("sarvam") else Brain()
        print("brain:", app.CALL_MODEL)
        await b.start()
        for said in ["letter head par kuch type karwana hai, template utha kar lao",
                     "ab ispe aaj ki date daalo aur likho, mahoday, aapko soochit kiya jaata hai ki kal sanstha mein sabhi mitron ki baithak hogi",
                     "nahi nahi, mitron ko saathiyon karo",
                     "aur haan, kal subah 9 baje mujhe call karke fees wali meeting yaad dilana",
                     "college website par ek fees collection ka dashboard bana do, course wise kitni fees aayi, aur live kar dena",
                     "theek hai bas, bye"]:
            t = time.time()
            r = await b.ask(said, app.state())
            for a in r["actions"]:
                await app.apply(a)
            print(f"{time.time() - t:.1f}s {said}\n    -> {r['actions']}\n    -> say: {r['say']}")
            assert r["say"], "empty reply"
        print("LETTER:", app.doc)
        await b.close()

    asyncio.run(demo())
