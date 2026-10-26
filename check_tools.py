"""Self-check for Raion's tool plumbing, no model and nothing sent: raion_mcp.py <-> tool API <-> run_tool.
Run: .venv\\Scripts\\python.exe check_tools.py"""
import asyncio, json, os, shutil, sys
from datetime import datetime as D

NUM = "919000000099"
os.environ.update({"WA_DRY_RUN": "1", "ALLOWED_NUMBERS": NUM, "ADMIN_NUMBERS": ""})
import app  # noqa: F401  (loads .env)
import wa_agent as wa


async def main():
    s = wa.session(NUM, "check")
    s.key = "k"
    wa.KEYS["k"] = s
    (s.p / "work" / "t.html").write_text("<html><body><h1>Hello</h1><p>[DATE]</p></body></html>", encoding="utf-8")
    p = await asyncio.create_subprocess_exec(sys.executable, "-I", str(wa.ROOT / "raion_mcp.py"), stdin=-1, stdout=-1,
                                             env={**os.environ, "RAION_URL": await wa.tool_api(), "RAION_KEY": "k"})

    async def rpc(i, method, params=None):
        p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n").encode())
        await p.stdin.drain()
        return json.loads(await p.stdout.readline())["result"]

    try:
        assert (await rpc(1, "initialize", {"protocolVersion": "2025-06-18"}))["serverInfo"]["name"] == "raion"
        names = [t["name"] for t in (await rpc(2, "tools/list"))["tools"]]
        assert "send_message" in names and "print" not in names, names  # print is admin-only
        r = await rpc(3, "tools/call", {"name": "make_pdf", "arguments": {"html": "work/t.html", "name": "t"}})
        assert json.loads(r["content"][0]["text"])["unfilled"] == ["[DATE]"]
        assert r["content"][1]["type"] == "image"  # the model sees its own preview
        r = await rpc(4, "tools/call", {"name": "send_message", "arguments": {"text": "x", "files": ["outbox/t.pdf"]}})
        assert r.get("isError") and "placeholders" in r["content"][0]["text"]  # unfinished builds can't go out
        r = await rpc(5, "tools/call", {"name": "send_message", "arguments": {"text": "नमस्ते"}})
        assert not r.get("isError") and wa.SENT[-1]["text"] == "नमस्ते"  # Hindi survives the pipe

        # an automation run's key: only the build tools, and what it builds is kept to go with its message
        built = []
        wa.JOB_KEYS["jk"] = (s, built)
        (s.p / "work" / "u.html").write_text("<html><body><h1>Report</h1></body></html>", encoding="utf-8")
        async with wa.aiohttp.ClientSession(await wa.tool_api(), headers={"X-Raion-Key": "jk"}) as c:
            assert {t["name"] for t in await (await c.get("/tools")).json()} == set(wa.BUILD_TOOLS)
            r = await (await c.post("/call", json={"name": "send_message", "arguments": {"text": "x"}})).json()
            assert r.get("isError") and "only build" in r["content"][0]["text"]
            await (await c.post("/call", json={"name": "make_pdf", "arguments": {"html": "work/u.html", "name": "u"}})).json()
        assert built == [s.p / "outbox" / "u.pdf"], built

        # automations: schedule, due times, site diff, delivery outside the 24-hour window
        try:
            await wa.schedule(s, {"action": "add", "task": "x", "time": "25:00"})
            assert False, "bad time accepted"
        except ValueError:
            pass
        j = (await wa.schedule(s, {"action": "add", "task": "news", "time": "23:00", "days": ["Monday", "15"]}))["saved"]
        assert j["id"] == "j1" and j["days"] == ["mon", "15"] and "news" in wa.jobs_text(NUM)
        assert wa.due_at(j, D(2026, 10, 26, 23, 0, 30)) == D(2026, 10, 26, 23, 0)  # a Monday
        assert wa.due_at(j, D(2026, 10, 26, 22, 59)) is None  # Sunday the 25th isn't a slot
        pages = iter([{"a": "old"}, {"a": "old", "b": "New notice"}])
        wa.site_links = lambda url: asyncio.sleep(0, next(pages))
        await wa.watch(NUM, "j1", ["http://x"])
        text, _ = await wa.watch(NUM, "j1", ["http://x"])
        assert "New notice" in text and "old" not in text, text
        await wa.notify(NUM, "update 1")  # no message from them yet: it waits and the doorbell rings
        assert wa.SENT[-1]["type"] == "template" and wa._jread(NUM, "window.json", {})["waiting"][0]["text"] == "update 1"
        assert [u["text"] for u in await wa.window_opened(s)] == ["update 1"] and wa.SENT[-1]["text"] == "update 1"
        await wa.notify(NUM, "update 2")  # window open: straight away
        assert wa.SENT[-1]["text"] == "update 2" and [n.split("\n")[-1] for n in s.notes] == ["update 1", "update 2"]
        wa._jwrite(NUM, "window.json", {"last_in": 0})  # an update with a file waits too, then goes with its caption
        await wa.notify(NUM, "report", built)
        await wa.window_opened(s)
        assert wa.SENT[-1]["type"] == "document" and wa.SENT[-1]["caption"] == "report", wa.SENT[-1]

        # reminder calls: no answer -> message; a call they pick up opens the window and sends what waited
        c = (await wa.schedule(s, {"action": "add", "task": "DHO ko letter", "time": "10:00", "date": "2099-01-01",
                                   "call": True}))
        assert c["saved"]["call"] and c["call_permission"].startswith("ok") and "📞" in wa.jobs_text(NUM)

        async def no_answer(num, reminder):
            return False
        wa.CALLER = no_answer
        await wa.run_job(NUM, c["saved"])
        assert wa.SENT[-1]["text"].startswith("⏰ *Reminder:* DHO ko letter")
        assert "reminder they set: \"x\"" in wa.call_prompt(NUM, "x").read_text(encoding="utf-8")
        wa._jwrite(NUM, "window.json", {"last_in": 0, "waiting": ["late update"]})
        await wa.call_started(NUM)
        assert wa.window_open(NUM) and wa.SENT[-1]["text"] == "late update"
        await wa.call_ended(NUM, [("Raion", "नमस्ते"), ("Them", "ok")], "x")
        assert s.notes[-1].startswith("Phone call: you called them for a reminder (x)")
        await wa.handle_incoming({"from": NUM, "id": "b1", "type": "button", "button": {"text": "अपडेट दिखाओ"}})
        assert wa.SENT[-1]["text"].startswith("Abhi koi")  # doorbell tap with nothing waiting
        (wa._jdir(NUM) / "log.md").write_text("## 2026-10-22 23:00 · j1\nnews A\n\n## 2026-10-23 23:00 · j2\nother\n\n"
                                              "## 2026-10-23 23:00 · j1\n(nothing new: not sent)\n\n"
                                              "## 2026-10-24 23:00 · j1 · late\nnews B\n\n", encoding="utf-8")
        assert [p.split("\n")[1] for p in wa.last_sent(NUM, "j1")] == ["news A", "news B"]  # what the next run sees
        assert (await wa.schedule(s, {"action": "remove", "id": "j1"}))["removed"] == "j1"

        # photo cards: found when missing, one sheet for many, the model's answer parsed, done once
        from PIL import Image
        img = s.p / "files" / "2026-10-24" / "IMG-1.jpg"
        img.parent.mkdir(parents=True, exist_ok=True)
        Image.effect_noise((300, 300), 120).convert("RGB").save(img, quality=95)
        assert img in wa._card_todo(10**6) and wa._received(img) == "2026-10-24"
        wa._sheet([img, img], wa.RUNTIME / "cards" / "check.jpg")
        assert Image.open(wa.RUNTIME / "cards" / "check.jpg").size == (1280, 320)
        assert wa.parse_cards('ok:\n```json\n[{"n": 1, "card": "Kind: newspaper cutting"}, {"n": 2, "card": ""}]\n```') \
            == {1: "Kind: newspaper cutting"}
        wa.card_of(img).write_text("Kind: other\n", encoding="utf-8")
        assert img not in wa._card_todo(10**6)
        print("ok")
    finally:
        p.stdin.close()
        await p.wait()
        shutil.rmtree(s.p, ignore_errors=True)

asyncio.run(main())
