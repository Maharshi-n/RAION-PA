"""Raion's memory logic, no asyncio (wa_agent.py calls it through asyncio.to_thread).

Stage 1 of docs (raion-memory-build-spec): the ledger (every message in and out, written by code at once, the
person's own words kept apart) and a search index over it and the memory files. Files stay the truth: the index,
.runtime/search.db, is disposable and rebuilt from them."""
import json, math, re, sqlite3, threading, unicodedata
from datetime import date, datetime
from pathlib import Path

# ---------------- ledger: memory/ledger/<yyyy-mm>/<number>.jsonl, append only ----------------
_last_id = 0
_id_lock = threading.Lock()


def ledger_file(mem: Path, num: str, at: datetime) -> Path:
    return mem / "ledger" / f"{at:%Y-%m}" / f"{num}.jsonl"


def ledger_add(mem: Path, num: str, kind: str, **fields) -> tuple[Path, dict]:
    """kind: arrived (raw, first), in (own = their exact words only), out, note (Raion's private note), system.
    Empty fields are left out. Returns the file and the event."""
    global _last_id
    at = datetime.now()
    with _id_lock:  # two events in one millisecond still get different ids
        _last_id = max(_last_id + 1, int(at.timestamp() * 1000))
        n = _last_id
    ev = {"id": f"{num}-{n}", "at": at.isoformat(timespec="seconds"), "kind": kind,
          **{k: v for k, v in fields.items() if v not in (None, "", [], False)}}
    f = ledger_file(mem, num, at)
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a", encoding="utf-8") as out:
        out.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return f, ev


def ledger_read(mem: Path, num: str, months: list[str]) -> list[dict]:
    out = []
    for m in months:
        f = mem / "ledger" / m / f"{num}.jsonl"
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


# ---------------- one-time import of the chats from before the ledger ----------------
OLD_HEAD = re.compile(r"(?:📩 [^\[\n]*)?\[(Text|Voice note \(transcribed\)) from ")
OLD_MSG = '\nMessage: "'


def legacy_events(lines: list[str]) -> list[dict]:
    """Old chat logs (people/<n>/sessions/*.jsonl) as ledger events. Their own words come only from the typed or
    spoken message, the 'Message: "..."' part that is always last; never from the inbox, notes or files shown with
    it, and nothing from a turn someone else's message started."""
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        role, t, at = e.get("role"), str(e.get("text") or ""), e.get("at")
        if not at:
            continue
        if role == "user":
            head, i = OLD_HEAD.match(t), t.rfind(OLD_MSG)
            if not head or i < 0 or not t.rstrip().endswith('"'):
                continue
            own, reply = t[i + len(OLD_MSG):].rstrip()[:-1], None
            if r := re.match(r'\[Replying to your message: "(.*?)"\] ', own, re.S):
                reply, own = r[1], own[r.end():]
            out.append({"at": at, "kind": "in", "own": own, "voice": head[1] != "Text", "reply_to": reply})
        elif role in ("assistant", "note") and t.strip():
            text, via = re.sub(r"\n\[sent [^\]\n]+\]", "", t), None
            if text.startswith("[automation] "):
                text, via = text[13:], "automation"
            out.append({"at": at, "kind": "out" if role == "assistant" else "note", "text": text,
                        "files": re.findall(r"\n\[sent ([^\]\n]+)\]", t), "via": via})
    return out


def backfill(mem: Path, go: bool) -> list[str]:
    """Plan (go=False: nothing written) or do the one-time import: for each person, the old chats from before their
    first ledger event, into the ledger with their old times and "legacy": true. Old files are only read."""
    plan = []
    for p in sorted(x for x in (mem / "people").glob("*") if x.is_dir()):
        num = p.name
        start = min((e["at"] for e in ledger_read(mem, num, [f.parent.name for f in mem.glob(f"ledger/*/{num}.jsonl")])),
                    default="9999")  # where the live ledger begins: older lines only, so nothing is there twice
        evs = sorted((e for f in sorted(p.glob("sessions/*.jsonl"))
                      for e in legacy_events(f.read_text(encoding="utf-8", errors="replace").splitlines())
                      if e["at"] < start), key=lambda e: e["at"])
        if not evs:
            continue
        n = {k: sum(e["kind"] == k for e in evs) for k in ("in", "out", "note")}
        plan.append(f"- +{num}: {n['in']} messages from them, {n['out']} replies, {n['note']} notes, "
                    f"{evs[0]['at'][:10]} to {evs[-1]['at'][:10]}")
        plan += [f"  - their words, e.g.: {e['own'][:80]!r}" for e in [e for e in evs if e["kind"] == "in"][:2]]
        if go:
            last, files = 0, {}
            for e in evs:
                last = max(last + 1, int(datetime.fromisoformat(e["at"]).timestamp() * 1000))
                ev = {"id": f"{num}-{last}", "at": e["at"], "kind": e["kind"], "legacy": True,
                      **{k: v for k, v in e.items() if k not in ("at", "kind") and v not in (None, "", [], False)}}
                files.setdefault(ledger_file(mem, num, datetime.fromisoformat(e["at"])), []).append(ev)
            for f, batch in files.items():
                f.parent.mkdir(parents=True, exist_ok=True)
                with open(f, "a", encoding="utf-8") as out:
                    out.writelines(json.dumps(ev, ensure_ascii=False) + "\n" for ev in batch)
    return plan


# ---------------- words and sounds ----------------
WORD_CATS = ("L", "N", "Co", "Mn", "Mc")  # same as the index's tokenizer: Hindi vowel signs stay inside words


def words(text: str) -> list[str]:
    t = unicodedata.normalize("NFC", text).casefold()
    return "".join(c if unicodedata.category(c).startswith(WORD_CATS) else " " for c in t).split()


SOUNDS = {"क": "k", "ख": "k", "ग": "g", "घ": "g", "च": "c", "छ": "c", "ज": "j", "झ": "j", "ट": "t", "ठ": "t", "ड": "d",
          "ढ": "d", "ण": "n", "त": "t", "थ": "t", "द": "d", "ध": "d", "न": "n", "प": "p", "फ": "f", "ब": "b", "भ": "b",
          "म": "m", "र": "r", "ल": "l", "व": "v", "श": "s", "ष": "s", "स": "s", "ह": "h"}


def skeleton(word: str) -> str:
    """Consonants only, sounds people spell differently merged, so "pariksha" and "परीक्षा" meet (prks).
    ponytail: crude on purpose (from raion-research/experiments/fts5_hindi.py: 41/42 Roman spellings found, ~15% of
    returned lines wrong), so it is only a fallback."""
    w = unicodedata.normalize("NFC", word.lower())
    if re.search("[ऀ-ॿ]", w):
        w = re.sub(r"(.)्\1", r"\1", w).replace("क्ष", "ks").replace("ज्ञ", "g")
        return "".join(SOUNDS.get(ch, ch if "a" <= ch <= "z" else "") for ch in w)
    w = re.sub(r"(.)\1+", r"\1", w).replace("sch", "sk")
    for a, b in (("chh", "C"), ("ch", "C"), ("sh", "s"), ("ph", "f"), ("kh", "k"), ("gh", "g"), ("th", "t"), ("dh", "d"),
                 ("bh", "b"), ("jh", "j"), ("w", "v"), ("q", "k"), ("x", "ks"), ("z", "j")):
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w).replace("c", "k").replace("C", "c")
    w = re.sub(r"g(?=[ei])", "j", w)
    return re.sub(r"[aeiouy0-9]", "", w)


def sound(text: str) -> str:
    return " ".join(s for s in (skeleton(w) for w in words(text)) if s)


# words that say nothing about what is wanted ("MNA ka school code kya hai?"): left out of the all-words search
STOP = set("""kya hai hain ka ki ke ko se me mein par aur ya bhi tha thi the kaun kab kahan kaise kitna kitne kitni
batao bata bataiye do de dena hu hoon ho kar karo mujhe mera meri mere hum hamara ji abhi wala wali wale
the a an is are was were of to in on for and or what who when where which how does do did my me i you your it this
that with tell send please
का की के को से में पर और या भी था थी थे है हैं क्या कौन कब कहाँ कहां कैसे कितना कितने कितनी बताओ बताइए दो मुझे मेरा
मेरी मेरे हम जी अभी वाला वाली वाले""".split())


# ---------------- the search index: .runtime/search.db, disposable ----------------
SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS doc USING fts5(text, keys, sound,
  scope UNINDEXED, origin UNINDEXED, at UNINDEXED, kind UNINDEXED, source UNINDEXED, ref UNINDEXED, who UNINDEXED,
  tokenize="unicode61 categories 'L* N* Co Mn Mc'");
CREATE TABLE IF NOT EXISTS file (path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
"""
DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
TAG = re.compile(r"\s*<!--(m\d+)-->\s*$")
HALF_LIFE = 30  # days


def sources(mem: Path) -> list[tuple[Path, str, str]]:
    """Every file the index holds, with its scope and kind. ponytail: a fixed list (the spec's first version, plus
    TODO.md and ARCHIVE.md: rules the size cap moved out are still things people said); .txt copies and photo cards
    are left out until their size and speed are measured."""
    g, sh, ad, pe = mem / "global", mem / "shared", mem / "admin", mem / "people"
    out = [(f, "org", "fact") for f in [g / "INSTITUTE.md", *g.glob("*/*.md"), *g.glob("*/notes/*.md")]]
    out += [(g / "RULES.md", "org", "rule"), (sh / "EVENTS.md", "org", "date"), (sh / "QUESTIONS.md", "org", "doubt")]
    out += [(f, "org", "date") for f in sh.glob("past/*.md")] + [(f, "org", "topic") for f in sh.glob("topics/**/*.md")]
    out += [(ad / "EVENTS.md", "admins", "date")] + [(f, "admins", "date") for f in ad.glob("past/*.md")]
    out += [(f, "admins", "topic") for f in ad.glob("topics/**/*.md")]
    out += [(f, "admins", "contact") for f in (mem / "contacts").glob("*.md")]
    for p in (pe.iterdir() if pe.is_dir() else []):
        if p.is_dir():
            sc = f"person:{p.name}"
            out += [(p / n, sc, k) for n, k in (("MEMORY.md", "rule"), ("ARCHIVE.md", "rule"), ("PROFILE.md", "profile"),
                                                 ("TODO.md", "card"), ("TODO.old.md", "card"))]
            out += [(f, sc, "topic") for f in p.glob("topics/**/*.md")] + [(f, sc, "summary") for f in p.glob("sessions/*.md")]
    out += [(f, f"person:{f.stem}", "chat") for f in mem.glob("ledger/*/*.jsonl")]
    return [x for x in out if x[0].is_file()]


def md_rows(text: str) -> list[tuple[int, str, str]]:
    """(line number, text, heading) per logical line: a bullet with its wrapped lines, a table row, a plain line."""
    rows, head = [], ""
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or re.fullmatch(r"[|:\-\s]+", s) or s.startswith("<!--"):
            continue
        if s.startswith("#"):
            head = s.lstrip("#").strip()
        elif rows and line[:1] in " \t" and not s.startswith(("-", "*", "|")) and rows[-1][0] > 0:
            n, t, h = rows[-1]
            rows[-1] = (n, f"{t} {s}", h)
        else:
            rows.append((i, s, head))
    return rows


def _iso(d: str | None, fallback: str) -> str:
    m = DATE.search(d or "")
    if m:
        try:
            return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        except ValueError:
            pass
    return fallback


class Index:
    """One connection for the whole server, used from worker threads one at a time (asyncio.to_thread)."""

    def __init__(self, path, mem: Path, admins=()):
        self.mem, self.admins = Path(mem), set(admins)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        if str(path) != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA)

    def close(self):
        with self.lock:
            self.db.close()

    # ---- writing ----
    def refresh(self, only=None) -> int:
        """Indexes what changed since last time (by size and time; a ledger file only from where it was read to).
        only: just these files. Returns how many files were (re)read."""
        with self.lock, self.db:
            want = sources(self.mem)
            if only:
                keep = {Path(p).resolve() for p in only}
                want = [x for x in want if x[0].resolve() in keep]
            else:  # files that are gone leave the index too
                have = {r[0] for r in self.db.execute("SELECT path FROM file")}
                for gone in have - {self._rel(f) for f, _, _ in want}:
                    self._drop(gone)
            n = 0
            for f, scope, kind in want:
                rel, st = self._rel(f), f.stat()
                old = self.db.execute("SELECT mtime, size FROM file WHERE path=?", (rel,)).fetchone()
                if old and old[0] == st.st_mtime and old[1] == st.st_size:
                    continue
                n += 1
                if kind == "chat" and old and st.st_size > old[1]:  # the ledger only grows: read the new part
                    self._chat(f, scope, rel, old[1])
                else:
                    self._drop(rel)
                    (self._chat(f, scope, rel, 0) if kind == "chat" else self._md(f, scope, kind, rel))
                self.db.execute("INSERT OR REPLACE INTO file VALUES (?,?,?)", (rel, st.st_mtime, st.st_size))
            return n

    def _rel(self, f: Path) -> str:
        return f.resolve().relative_to(self.mem.resolve()).as_posix()

    def _drop(self, rel):
        self.db.execute("DELETE FROM doc WHERE source=?", (rel,))
        self.db.execute("DELETE FROM file WHERE path=?", (rel,))

    def _add(self, text, keys, scope, origin, at, kind, source, ref, who=""):
        self.db.execute("INSERT INTO doc VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (text, keys, sound(f"{text} {keys}"), scope, origin, at, kind, source, ref, who))

    def _md(self, f: Path, scope, kind, rel):
        mtime = datetime.fromtimestamp(f.stat().st_mtime).date().isoformat()
        for n, text, head in md_rows(f.read_text(encoding="utf-8", errors="replace")):
            tag = TAG.search(text)
            text = TAG.sub("", text)
            at = _iso(text, mtime) if kind == "date" else mtime
            self._add(text, head, scope, "legacy", at, kind, rel, tag[1] if tag else f"{rel}:{n}")

    def _chat(self, f: Path, scope, rel, start: int):
        """Their own words and Raion's replies only: inbox quotes, notes and tool lines are not searchable until
        stage 4 knows who said them."""
        num = scope.split(":", 1)[1]
        with open(f, "rb") as fh:
            fh.seek(start)
            data = fh.read().decode("utf-8", "replace")
        for line in data.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            k = ev.get("kind")
            old = ev.get("legacy")  # imported from the logs before the ledger: trusted as old notes are
            if k == "in" and ev.get("own"):
                self._add(ev["own"], "", scope, "legacy" if old else "admin" if num in self.admins else "staff",
                          ev["at"], "chat", rel, ev["id"], num)
            elif k == "out" and ev.get("text"):
                self._add(ev["text"], "", scope, "system" if ev.get("via") else "legacy" if old else "assistant",
                          ev["at"], "chat", rel, ev["id"], "Raion")

    # ---- reading ----
    def search(self, query: str, scopes, since=None, until=None, kind=None, limit=8, today=None) -> list[dict]:
        """All words first, then any word; under 3 hits, also by sound (marked ~sound). Scope is in the SQL."""
        since = _iso(since, since) if since else None  # dd/mm/yyyy or yyyy-mm-dd; until takes in that whole day
        until = _iso(until, until) + "~" if until else None
        ws = [w for w in dict.fromkeys(words(query)) if len(w) > 1 or w.isdigit()]
        key = [w for w in ws if w not in STOP] or ws
        if not key:
            return []
        args = (scopes, since, until, kind)
        exact = {w: _quote(w) + ("*" if len(w) >= 4 else "") for w in key}  # "number" also finds "numbers"
        sk = {w: _quote(s) + ("*" if len(s) >= 3 else "") for w in key if len(s := skeleton(w)) >= 2}
        # all words: each one by its spelling, or by its sound (so "प्रिया" meets "Priya" when every other word fits)
        both = [f"({{text keys}} : {exact[w]} OR sound : {sk[w]})" if w in sk else f"{{text keys}} : {exact[w]}"
                for w in key]
        tiers = [self._match(" AND ".join(both), *args)]
        if len(tiers[0]) < limit and len(key) > 1:  # then any word, by spelling; a word found nowhere goes by sound
            terms = [f"{{text keys}} : {exact[w]}" if self._has(exact[w]) else f"sound : {sk[w]}"
                     for w in key if w in sk or self._has(exact[w])]
            tiers.append(self._match(" OR ".join(terms), *args) if terms else [])
        if sum(map(len, tiers)) < 3 and sk:  # the spec's fallback: anything that sounds like a query word
            tiers.append(self._match("sound : (" + " OR ".join(dict.fromkeys(sk.values())) + ")", *args))
        today, hits, seen = today or date.today(), [], set()
        for tier, found in enumerate(tiers):  # all-words hits always above any-word hits
            ranked = []
            for h in found:
                if h["ref"] in seen:
                    continue
                seen.add(h["ref"])
                got = words(f"{h['text']} {h['keys']}")
                h["sound"] = not any(g.startswith(w) for w in key for g in got)  # found only by sound: weaker, said so
                try:
                    d = date.fromisoformat(h["at"][:10])
                    age = abs((d - today).days) if h["kind"] == "date" else max((today - d).days, 0)
                except ValueError:
                    age = 0
                h["score"] = -h["bm25"] * math.pow(0.5, age / HALF_LIFE) * (0.5 if h["sound"] else 1)
                ranked.append(h)
            hits += sorted(ranked, key=lambda h: -h["score"])
        return hits[:limit]

    def _match(self, expr, scopes, since, until, kind):
        sql = ("SELECT text, keys, scope, origin, at, kind, source, ref, who, bm25(doc) FROM doc WHERE doc MATCH ? "
               "AND scope IN"
               f" ({','.join('?' * len(scopes))})")
        args = [expr, *scopes]
        for cond, v in (("at >= ?", since), ("at <= ?", until), ("kind = ?", kind)):
            if v:
                sql += f" AND {cond}"
                args.append(v)
        with self.lock:
            try:
                rows = self.db.execute(sql + " ORDER BY bm25(doc) LIMIT 200", args).fetchall()
            except sqlite3.OperationalError:  # a query the parser still refuses: no hits, never an error
                return []
        return [dict(zip(("text", "keys", "scope", "origin", "at", "kind", "source", "ref", "who", "bm25"), r))
                for r in rows]

    def _has(self, term: str) -> bool:
        with self.lock:
            try:
                return bool(self.db.execute("SELECT 1 FROM doc WHERE doc MATCH ? LIMIT 1",
                                            (f"{{text keys}} : {term}",)).fetchone())
            except sqlite3.OperationalError:
                return False


def _quote(w: str) -> str:
    return '"' + w.replace('"', '""') + '"'


def show(h: dict, names=None) -> str:
    """[ref] date · who · text (source)"""
    try:
        d = datetime.fromisoformat(h["at"]).strftime("%d/%m/%Y" + (" %H:%M" if "T" in h["at"] else ""))
    except ValueError:
        d = h["at"]
    who = (names or {}).get(h["who"], h["who"]) or {"legacy": "notes"}.get(h["origin"], h["origin"])
    text = h["text"] if len(h["text"]) <= 300 else h["text"][:300] + "…"
    return f"[{h['ref']}] {d} · {who} · {text} ({h['source']}){' ~sound' if h.get('sound') else ''}"
