"""Self-check for the Claude usage-limit pause with a fake meter: no model, nothing sent.
Run: .venv\\Scripts\\python.exe check_limit.py"""
import asyncio, json, os, tempfile, time
from pathlib import Path

NUM = "919000000099"
os.environ.update({"WA_DRY_RUN": "1", "ALLOWED_NUMBERS": NUM, "ADMIN_NUMBERS": ""})
import app  # noqa: F401  (loads .env)
import brain
import wa_agent as wa


def meter(five, week, five_back=3600, week_back=86400, at=None):
    """A reading as the CLI prints it: percent left of each limit, seconds until each comes back."""
    brain.save_usage({"rate_limit_info": {"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 1 - five / 100, "resetsAt": int(time.time() + five_back)},
        "seven_day": {"utilization": 1 - week / 100, "resetsAt": int(time.time() + week_back)}}}})
    if at is not None:
        u = json.loads(brain.USAGE.read_text(encoding="utf-8"))
        brain.USAGE.write_text(json.dumps({**u, "at": at}), encoding="utf-8")


class FakeBrain:
    alive = True

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


def due_now():  # every task put aside: its limit came back 3 minutes ago
    wa.LIMIT_WAIT.write_text(json.dumps([{**w, "after": time.time() - 180} for w in wa._waits()]), encoding="utf-8")


async def main():
    tmp = Path(tempfile.mkdtemp())
    brain.USAGE, wa.LIMIT_WAIT = tmp / "usage.json", tmp / "wait.json"
    s = wa.session(NUM, "check")
    started, rung, probes = [], [], []

    async def nothing():
        pass

    async def fake_probe():
        probes.append(1)

    async def fake_doorbell(num):
        rung.append(num)

    wa.handle_message = lambda sess, text, voice, *a, **k: started.append((sess.num, text)) or nothing()
    wa.doorbell, wa.probe_usage = fake_doorbell, fake_probe

    assert wa.usage_line() == "Claude limit: not known yet"
    meter(69, 68)
    assert wa.limit_low() is None, wa.limit_low()
    assert "5-hour 69% left" in wa.usage_line() and "weekly 68% left" in wa.usage_line(), wa.usage_line()
    try:
        await wa.run_tool(s, "wait_for_limit", {"note": "x"})
        raise AssertionError("put aside while the limit is fine")
    except ValueError as e:
        assert "not low" in str(e)

    # low: a big task is put aside until the 5-hour limit is back
    meter(8, 40)
    assert wa.limit_low()[0] == "five_hour" and "keep the rest" in wa.usage_line(), wa.usage_line()
    res, _ = await wa.run_tool(s, "wait_for_limit", {"note": "fee report: page 1 done, page 2 left"})
    assert res["saved"] and [w["num"] for w in wa._waits()] == [NUM]

    # mid-task: told once per window, not every minute
    s.brain, s.busy = FakeBrain(), True
    await wa.limit_watch()
    await wa.limit_watch()
    assert len(s.brain.sent) == 1 and "finish the step" in s.brain.sent[0], s.brain.sent
    # a stale reading while a chat is busy: a fresh one is asked for
    meter(50, 50, at=time.time() - 600)
    await wa.limit_watch()
    await asyncio.sleep(0.01)
    assert probes == [1], probes
    s.brain, s.busy = None, False

    # not due yet: nothing wakes
    await wa.limit_wake()
    assert not started and len(wa._waits()) == 1
    # due, but the weekly limit is low now: it waits for that one too
    due_now()
    meter(100, 5, five_back=-10)
    await wa.limit_wake()
    assert not started and wa._waits()[0]["after"] > time.time() + 3600, wa._waits()
    # back, chat window closed: the doorbell rings once, the task waits for their reply
    due_now()
    meter(90, 60, five_back=-10)
    wa.window_open = lambda num: False
    await wa.limit_wake()
    await wa.limit_wake()
    assert rung == [NUM] and not started and wa._waits()[0]["rang"], (rung, wa._waits())
    # they replied: the task goes back to their chat with its note, and leaves the list
    wa.window_open = lambda num: True
    await wa.limit_wake()
    await asyncio.sleep(0.01)
    assert len(started) == 1 and "page 2 left" in started[0][1] and "limit is back" in started[0][1], started
    assert wa._waits() == []
    print("check_limit: all good")


if __name__ == "__main__":
    asyncio.run(main())
