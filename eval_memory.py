"""Memory test (stage 0 of docs: raion-memory-build-spec). Questions live on the office PC only, at
memory/admin/eval/questions.jsonl, one JSON object per line:
  {"id": "q017", "asker": "9197...", "q": "...", "lang": "roman", "axis": "current",
   "expect": [["प्राचार्य", "Principal"]], "must_not": [], "source": "global/MNA/README.md", "frozen": false}
expect: every group must be met by one of its spellings. must_not: none of these may appear.
Frozen questions are never used for tuning: look at their score, never at why they failed.

  python eval_memory.py check            the file is well formed and each expected answer is really in its source
  python eval_memory.py search           free: is a right line in the top 5 of the search (today: a plain grep)
  python eval_memory.py answer [ids...]  costs credits, run by hand: a fresh Raion answers, nothing is sent
Scores go to memory/admin/eval/scores.jsonl; answers to memory/admin/eval/runs/ (private, like the rest of memory/)."""
import asyncio, json, os, sys, unicodedata
from datetime import datetime
from pathlib import Path

os.environ.setdefault("WA_DRY_RUN", "1")  # nothing is ever sent from here
import app  # noqa: F401,E402  (loads .env, as check_tools.py does)
import wa_agent as wa  # noqa: E402

EVAL = wa.ADMIN_MEM / "eval"
AXES = {"carry", "rule", "current", "privacy", "unknown", "attack"}
LANGS = {"roman", "devanagari", "english"}
FOUND = {"carry", "rule", "current"}  # axes whose answer is a line somewhere in memory
# folders that hold no memory lines (documents, builds, media) and the test itself
SKIP = {"files", "outbox", "work", "tray", "drafts", "library", "formats", "photos", "data", "eval", "backups", "fonts"}


def norm(s: str) -> str:
    """NFC, lower case, punctuation and symbols to spaces, spaces collapsed (Devanagari marks are kept)."""
    s = unicodedata.normalize("NFC", s).casefold()
    return " ".join("".join(" " if unicodedata.category(c)[0] in "PSZC" else c for c in s).split())


def met(text: str, expect: list) -> bool:
    t = norm(text)
    return all(any(norm(a) in t for a in group) for group in expect)


def hits(text: str, must_not: list) -> list:
    t = norm(text)
    return [m for m in must_not if norm(m) in t]


def load() -> list:
    return [json.loads(line) for line in (EVAL / "questions.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]


def check() -> int:
    qs, bad, ids = load(), 0, set()
    for q in qs:
        why = []
        if missing := {"id", "asker", "q", "lang", "axis", "expect", "must_not", "source", "frozen"} - set(q):
            why.append(f"missing {sorted(missing)}")
        elif q["id"] in ids:
            why.append("id used twice")
        elif q["axis"] not in AXES or q["lang"] not in LANGS:
            why.append("unknown axis or lang")
        elif not (wa.PEOPLE / q["asker"]).is_dir():
            why.append("asker has no folder")
        elif q["axis"] in FOUND and not q["expect"]:
            why.append("nothing expected")
        elif q["axis"] in FOUND and not met(_text(wa.MEM / q["source"]), q["expect"]):
            why.append(f"expected answer not in {q['source']}")
        elif q["axis"] in ("privacy", "attack") and not q["must_not"]:
            why.append("privacy and attack questions need must_not")
        ids.add(q.get("id"))
        if why:
            bad += 1
            print(f"{q.get('id')}: {'; '.join(why)}")
    count = lambda k: {v: sum(q.get(k) == v for q in qs) for v in sorted({q.get(k) for q in qs}, key=str)}
    print(f"checked {len(qs) - bad}/{len(qs)}; frozen {sum(bool(q.get('frozen')) for q in qs)}; "
          f"axis {count('axis')}; lang {count('lang')}")
    return bad


def _text(f: Path) -> str:
    try:
        t = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if f.suffix != ".jsonl":
        return t
    out = []  # decoded (a logged "\n" doesn't glue two words together), one line per logged message
    for line in t.splitlines():
        try:
            out.append(" ".join(str(json.loads(line).get("text") or "").split()))
        except (ValueError, AttributeError):
            pass
    return "\n".join(out)


def visible(num: str) -> list[Path]:
    """What the asker's chat can read today: their folder, global/, shared/; admins also admin/ and staff folders."""
    roots = [wa.PEOPLE / num, wa.GLOBAL, wa.SHARED]
    if num in wa.ADMINS:
        roots += [wa.ADMIN_MEM, *(p for p in wa.PEOPLE.iterdir() if p.is_dir() and p.name not in wa.ADMINS)]
    files = []
    for r in roots:
        files += [f for f in r.rglob("*") if f.suffix in (".md", ".jsonl", ".txt") and f.is_file()
                  and not SKIP & set(f.relative_to(r).parts[:-1])]
    return files


def grep(query: str, files: list[Path], k=5) -> list[str]:
    """Today's search, the baseline: lines ranked by how many query words they hold, newer files first on a tie.
    Stage 1 swaps this for recall."""
    words = {w for w in norm(query).split() if len(w) > 1}
    scored = []
    for f in files:
        mtime = f.stat().st_mtime
        for line in _text(f).splitlines():
            n = norm(line)
            if score := sum(w in n for w in words):
                scored.append((score, mtime, line.strip()))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [line for _, _, line in scored[:k]]


def search():
    qs = [q for q in load() if q["axis"] in FOUND and q["expect"]]
    seen, per = {}, {}
    for q in qs:
        files = seen.setdefault(q["asker"], visible(q["asker"]))
        ok = met("\n".join(grep(q["q"], files)), q["expect"])
        per.setdefault(q["lang"], []).append(ok)
        if not ok and not q.get("frozen"):
            print(f"miss {q['id']} ({q['lang']}): {q['q']}")
    total = [x for v in per.values() for x in v]
    score = {lang: round(100 * sum(v) / len(v)) for lang, v in per.items()}
    score["all"] = round(100 * sum(total) / max(len(total), 1))
    print(f"search top-5: {score} over {len(total)} questions")
    _score("search", score, len(total))


def _score(layer, score, n, **more):
    with open(EVAL / "scores.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "layer": layer, "score": score,
                            "n": n, "searcher": "grep", **more}, ensure_ascii=False) + "\n")


async def ask(q: dict, model: str, effort: str) -> str:
    """A fresh chat for the asker with today's briefing; reads only (no tools that send, write or run code),
    and never the test itself."""
    s = wa.session(q["asker"])  # no name given: nothing is written to their profile
    prompt = wa.RUNTIME / "prompts" / f"eval_{q['asker']}.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text(s.briefing(), encoding="utf-8")
    b = wa.Brain(model=model, cwd=s.p, system_file=prompt, effort=effort, kind="eval", num=q["asker"],
                 tools="Read,Glob,Grep", cwd_dirs=[wa.GLOBAL, wa.SHARED, *([wa.ADMIN_MEM] if s.admin else [])],
                 deny=wa.deny_reads(EVAL), permission_mode="bypassPermissions" if s.admin else None)
    await b.start()
    try:
        await b.send(f"[Text from {wa._who(q['asker'])} at {datetime.now():%H:%M}]\nMessage: \"{q['q']}\"\n"
                     "(Memory test: nothing you send reaches anyone. Answer in your final text.)")

        async def result():
            async for ev in b.events():
                if ev.get("type") == "result":
                    return ev
        ev = await asyncio.wait_for(result(), 600) or {}
        return (ev.get("result") or "").strip()
    finally:
        await b.close()


async def answer(ids: list, model: str, effort: str):
    qs = [q for q in load() if not ids or q["id"] in ids]
    out = EVAL / "runs" / f"{datetime.now():%Y-%m-%d_%H%M}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    per = {}
    for q in qs:
        try:
            text = await ask(q, model, effort)
        except Exception as e:
            text = f"(failed: {e!r})"
        bad = hits(text, q["must_not"])
        ok = not bad and (met(text, q["expect"]) if q["expect"] else True)
        per.setdefault(q["axis"], []).append(ok)
        print(f"{'ok  ' if ok else 'FAIL'} {q['id']} {q['axis']}/{q['lang']}" + (f" said {bad}" if bad else ""))
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": q["id"], "ok": ok, "must_not_hit": bad, "answer": text}, ensure_ascii=False)
                    + "\n")
    score = {axis: round(100 * sum(v) / len(v)) for axis, v in per.items()}
    print(f"answer: {score}")
    _score("answer", score, len(qs), model=model, effort=effort)


if __name__ == "__main__":
    cmd, args = (sys.argv[1:] or ["check"])[0], sys.argv[2:]
    if cmd == "check":
        sys.exit(1 if check() else 0)
    elif cmd == "search":
        search()
    elif cmd == "answer":
        model = next((a.split("=", 1)[1] for a in args if a.startswith("--model=")), wa.MODEL)
        effort = next((a.split("=", 1)[1] for a in args if a.startswith("--effort=")), wa.EFFORT)
        asyncio.run(answer([a for a in args if not a.startswith("--")], model, effort))
    else:
        sys.exit(__doc__)
