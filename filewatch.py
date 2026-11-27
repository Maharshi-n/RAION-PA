"""The office PCs' work folders, kept up to date on Raion's PC. Nothing runs on the other PCs: their folders are
shared read-only on the LAN and this reads them. Every run, per PC: list the folders (names, sizes, dates; no file is
opened), compare with the index (new / changed / deleted / moved), copy the new and changed documents to mirror/
(the copy it replaces goes to versions/ first; photos, videos and everything else are only listed), read the words
inside each copied document into the index, and add short lines to the day's change report. No AI in here.

  pcfiles/files.db                 the index: every file seen on every PC (deleted ones too), with the words inside
  pcfiles/mirror/<PC>/<folder>/    the latest copy of each document, same layout as on the PC
  pcfiles/versions/<PC>/...        how a document was at the start of a day it changed (name.YYYYMMDD.ext)
  pcfiles/changes/<PC>/<date>.md   what changed, a few lines per run
  pcfiles/pcs/<PC>.md              the PC's folders in short

  python filewatch.py probe <folder>     only count and time a listing (try this first on a new PC)
  python filewatch.py scan               one run over every PC in .runtime/pcfiles.json
  python filewatch.py find <words>
  python filewatch.py test               the self-check on a made-up PC
"""
import collections, json, os, re, shutil, socket, sqlite3, stat, subprocess, sys, time, zipfile
from datetime import datetime
from html import unescape
from pathlib import Path

import mem

ROOT = Path(__file__).parent
HOME = Path(os.environ.get("PC_FILES_DIR") or ROOT / "pcfiles")  # big: outside memory/ and its backups, never in git
CONFIG = ROOT / ".runtime" / "pcfiles.json"  # the PCs, their folders and the read-only account: stays on this PC

DOCS = {".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".pdf", ".csv", ".txt", ".rtf", ".odt", ".ods"}
PHOTOS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".heic", ".psd", ".cdr", ".raw", ".nef"}
VIDEOS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".3gp", ".flv", ".mpg", ".mpeg", ".m4v", ".webm", ".vob"}
SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "appdata", "application data",
             "local settings", "system volume information", "recovery", "perflogs", "msocache", "windows.old",
             "temp", "tmp", "node_modules", "__pycache__", ".git", ".venv", "venv", ".cache"}
SKIP_FILES = {"thumbs.db", "desktop.ini", "ntuser.dat", ".ds_store"}
SKIP_ENDS = (".tmp", ".part", ".crdownload", ".lnk", ".lock", ".bak~")
COPY_MAX = 20 * 2**20  # a bigger document is only listed (scanned books and syllabi: most of a PC's gigabytes)
# What a PC already held on the day it was first read, untouched for longer than this: listed, its words read into
# the index over the LAN, no copy kept (fetch gets one when asked). Everything newer, and everything that turns up
# later, is copied and that copy is never dropped for growing old.
RECENT_DAYS = 365
FETCH_MAX, FETCH_DAYS = 100 * 2**20, 7  # one file fetched on request: up to WhatsApp's limit, kept a week
COPY_SECS = 600  # copying per PC per run; the rest waits for the next run (the first run of a PC has a lot)
TEXT_CHARS, TEXT_MAX, PDF_PAGES = 20000, 30 * 2**20, 10  # words kept per document; bigger files: name only
COLLAPSE = 15  # more files than this in one place: one line with counts instead of their names
# ponytail: every run lists every folder, until a PC's full listing takes longer than SLOW_SECS; from then on a run
# skips folders with no change in QUIET_DAYS whose own date is the same, and a full listing runs every FULL_HOURS
# (a new file deep inside a quiet folder waits for that one). Measure with `probe` before changing these.
SLOW_SECS, QUIET_DAYS, FULL_HOURS = 20, 14, 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, pc TEXT, root TEXT, dir TEXT, name TEXT, size INTEGER,
  mtime REAL, kind TEXT, first REAL, changed REAL, gone REAL, copied INTEGER DEFAULT 0, UNIQUE(pc, root, dir, name));
CREATE TABLE IF NOT EXISTS dirs (pc TEXT, root TEXT, dir TEXT, up TEXT, mtime REAL, changed REAL,
  PRIMARY KEY(pc, root, dir));
CREATE INDEX IF NOT EXISTS dirs_up ON dirs(pc, root, up);
CREATE TABLE IF NOT EXISTS roots (pc TEXT, root TEXT, full REAL, secs REAL, seen REAL, PRIMARY KEY(pc, root));
CREATE VIRTUAL TABLE IF NOT EXISTS words USING fts5(name, dir, body, tokenize="unicode61 categories 'L* N* Co Mn Mc'");
"""
# files.copied: 0 = a document still to copy (new, changed, or open in Excel last time), 1 = in mirror/, 2 = too big,
# 3 = old when the PC was first read: its words are in the index, no copy
# files.mtime: when the file was last saved, by its own PC (what find sorts and filters by)
# files.changed / first / gone: Raion's PC's clock (the other PCs' clocks can be wrong); mtime only tells a change


def db() -> sqlite3.Connection:
    HOME.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(HOME / "files.db", timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def pcs() -> list[dict]:
    """[{"name": "PC2", "roots": {"D": r"\\\\192.168.1.50\\D"}, "user": ..., "password": ...}]"""
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return "doc" if ext in DOCS else "photo" if ext in PHOTOS else "video" if ext in VIDEOS else "other"


def _long(p) -> str:
    """Windows' long form of a path: folders nested past 260 characters still open."""
    p = os.path.abspath(str(p))
    if os.name != "nt" or p.startswith("\\\\?\\"):
        return p
    return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p


def _at(base, d: str, name: str = "") -> str:
    return os.path.join(str(base), *(d.split("/") if d else ()), *((name,) if name else ()))


def _size(n) -> str:
    n = n or 0
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{max(1, n // 1024)} KB"


def _list(path: str) -> tuple[dict, dict]:
    """One folder -> ({file: (size, mtime)}, {folder: mtime}). OSError unless all of it could be read: a half-read
    folder must never look like deleted files."""
    files, dirs = {}, {}
    with os.scandir(_long(path)) as it:
        for e in it:
            st = e.stat(follow_symlinks=False)  # on Windows this comes with the listing: no extra trip per file
            at, low = getattr(st, "st_file_attributes", 0), e.name.lower()
            if stat.S_ISDIR(st.st_mode):  # 0x400: a link to another folder (loops), 0x4: Windows' own
                if not (at & 0x404 or low in SKIP_DIRS or low.startswith("$")):
                    dirs[e.name] = st.st_mtime
            elif not (at & 0x4 or low in SKIP_FILES or low.startswith("~$") or low.endswith(SKIP_ENDS)):
                files[e.name] = (st.st_size, st.st_mtime)
    return files, dirs


def _under(d: str) -> tuple:
    """SQL arguments for "this folder or anything below it" (no LIKE: names are full of _ and %)."""
    return (d, d + "/", d + "0") if d else ("", "", "\U0010ffff")


UNDER = "(dir = ? OR (dir > ? AND dir < ?))"


def scan_root(con, pc: str, label: str, path: str, now: float, quick=False) -> dict:
    """Lists one shared folder and says what differs from the index. Nothing is written here."""
    ev = {"new": [], "changed": [], "gone": [], "newdirs": set(), "lostdirs": [], "listed": {}, "errors": 0}
    ev["first"] = first = not con.execute("SELECT 1 FROM dirs WHERE pc=? AND root=? AND dir=''", (pc, label)).fetchone()
    stack = [("", 0.0)]
    while stack:
        d, mt = stack.pop()
        try:
            files, subs = _list(_at(path, d))
        except OSError:
            ev["errors"] += 1  # locked, no rights, or the PC went off: this folder is left exactly as it was
            continue
        ev["listed"][d] = mt
        known = {r[0]: r[1:] for r in con.execute(
            "SELECT name, size, mtime FROM files WHERE pc=? AND root=? AND dir=? AND gone IS NULL", (pc, label, d))}
        for n, (size, m) in files.items():
            k = known.pop(n, None)
            if k is None:
                ev["new"].append((label, d, n, size, m))
            elif k[0] != size or abs(k[1] - m) > 2:
                ev["changed"].append((label, d, n, size, m))
        ev["gone"] += [(label, d, n, *known[n]) for n in known]
        kd = {r[0]: r[1:] for r in con.execute(
            "SELECT dir, mtime, changed FROM dirs WHERE pc=? AND root=? AND up=?", (pc, label, d))}
        for n, m in subs.items():
            sd = f"{d}/{n}" if d else n
            k = kd.pop(sd, None)
            if k is None and not first:
                ev["newdirs"].add(sd)
            if quick and k and abs(k[0] - m) <= 2 and k[1] < now - QUIET_DAYS * 86400:
                continue
            stack.append((sd, m))
        for sd in kd:  # a folder that is gone, with everything that was in it
            ev["lostdirs"].append(sd)
            ev["gone"] += [(label, *r) for r in con.execute(
                f"SELECT dir, name, size, mtime FROM files WHERE pc=? AND root=? AND gone IS NULL AND {UNDER}",
                (pc, label, *_under(sd)))]
    return ev


def _pairs(new: list, gone: list) -> list:
    """A file that left one place and turned up in another with the same name, size and date was moved (or its
    folder renamed). Only when exactly one of each fits. -> [(gone, new)]"""
    def by(items):
        out = collections.defaultdict(list)
        for x in items:
            out[(x[2].lower(), x[3], round(x[4]))].append(x)
        return out
    a, b = by(gone), by(new)
    return [(a[k][0], b[k][0]) for k in a if len(a[k]) == 1 and len(b.get(k, ())) == 1]


def _name_words(name: str) -> str:
    """The name, and again with letters and digits apart: "fees2026.xlsx" is found by "fees" and by "2026"."""
    return name + " " + re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", os.path.splitext(name)[0])


def _put(con, pc, label, d, n, size, m, now) -> int:
    con.execute("INSERT INTO files(pc, root, dir, name, size, mtime, kind, first, changed) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(pc, root, dir, name) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
                "changed=excluded.changed, gone=NULL, copied=0", (pc, label, d, n, size, m, kind(n), now, now))
    i = con.execute("SELECT id FROM files WHERE pc=? AND root=? AND dir=? AND name=?", (pc, label, d, n)).fetchone()[0]
    con.execute("DELETE FROM words WHERE rowid=?", (i,))
    con.execute("INSERT INTO words(rowid, name, dir, body) VALUES(?,?,?,'')", (i, _name_words(n), d))
    return i


def _mirror(pc, label, d, n="") -> str:
    return _at(HOME / "mirror" / pc / label, d, n)


def _keep(dest: str, now: float):
    """The copy about to be replaced goes to versions/, once a day (how the document was that morning), and it is the
    only one kept: a spare for the day a file is saved wrong, not a history."""
    rel = os.path.relpath(dest, HOME / "mirror")
    stem, ext = os.path.splitext(rel)
    ver = _long(HOME / "versions" / f"{stem}.{datetime.fromtimestamp(now):%Y%m%d}{ext}")
    if not os.path.exists(ver):
        os.makedirs(os.path.dirname(ver), exist_ok=True)
        os.replace(_long(dest), ver)
        for old in _versions(dest):
            if _long(old) != ver:
                os.remove(_long(old))


def _versions(dest: str) -> list[str]:
    """The kept earlier copies of one document (name.YYYYMMDD.ext)."""
    stem, ext = os.path.splitext(os.path.relpath(dest, HOME / "mirror"))
    folder, base = os.path.split(str(HOME / "versions" / stem))
    try:
        return [os.path.join(folder, f) for f in os.listdir(_long(folder))
                if f.startswith(base + ".") and f.endswith(ext) and re.fullmatch(r"\.\d{8}", f[len(base):len(f) - len(ext)])]
    except OSError:
        return []


def _copy(src: str, dest: str, size: int, m: float, now: float) -> bool:
    """One document into mirror/. False (try again next run) when it changed while being copied. OSError: open in
    Excel or Word, or the PC went off."""
    d = _long(dest)
    if os.path.exists(d):
        st = os.stat(d)
        if st.st_size == size and abs(st.st_mtime - m) <= 2:
            return True  # already there (the PC was away and came back)
    os.makedirs(os.path.dirname(d), exist_ok=True)
    part = d + ".part"  # a copy cut short never stands in for the real one
    shutil.copyfile(_long(src), part)
    st = os.stat(_long(src))
    if st.st_size != size or abs(st.st_mtime - m) > 2 or os.path.getsize(part) != size:
        os.remove(part)
        return False
    if os.path.exists(d):
        _keep(dest, now)
    os.replace(part, d)
    try:
        os.utime(d, (m, m))
    except OSError:  # a date Windows can't store (a PC with a broken clock)
        pass
    return True


# ---------------- the words inside a document (code only: what is in the index is what is in the file) ----------------
def _tags(xml: bytes) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", xml.decode("utf-8", "replace")))


def text_of(p, limit=TEXT_CHARS) -> str:
    """A document's text for the search index, at most `limit` characters; "" when there is none to read (a scan,
    a password, a broken file). ponytail: .doc/.ppt are read roughly (runs of text in the bytes): enough to find a
    file by a word in it, not to read it."""
    p, out = Path(p), []
    ext = p.suffix.lower()
    try:
        if os.path.getsize(_long(p)) > TEXT_MAX:
            return ""
        if ext in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(_long(p), data_only=True, read_only=True)
            try:
                left = limit
                for ws in wb.worksheets:
                    out.append(f"Sheet {ws.title}:")
                    for row in ws.iter_rows(values_only=True):
                        line = " ".join(str(v) for v in row if v is not None)
                        out.append(line)
                        left -= len(line) + 1
                        if left < 0:
                            break
                    if left < 0:
                        break
            finally:
                wb.close()
        elif ext == ".xls":
            import xlrd
            wb = xlrd.open_workbook(_long(p), on_demand=True)
            left = limit
            for sh in wb.sheets():
                out.append(f"Sheet {sh.name}:")
                for r in range(sh.nrows):
                    line = " ".join(str(int(v) if isinstance(v, float) and v == int(v) else v)
                                    for v in sh.row_values(r) if v not in ("", None))
                    out.append(line)
                    left -= len(line) + 1
                    if left < 0:
                        break
                if left < 0:
                    break
        elif ext in (".docx", ".pptx", ".odt", ".ods"):
            with zipfile.ZipFile(_long(p)) as z:
                want = {".docx": r"word/(document|header\d*|footer\d*)\.xml", ".pptx": r"ppt/slides/slide\d+\.xml"}
                for n in z.namelist():
                    if re.fullmatch(want.get(ext, r"content\.xml"), n):
                        out.append(_tags(re.sub(rb"</(w:p|a:p|text:p)>", b"\n", z.read(n))))
        elif ext == ".pdf":
            import pymupdf
            with pymupdf.open(_long(p)) as doc:
                out += [doc[i].get_text() for i in range(min(len(doc), PDF_PAGES))]
        elif ext in (".csv", ".txt"):
            with open(_long(p), "rb") as f:
                out.append(f.read(limit * 4).decode("utf-8", "replace"))
        elif ext in (".doc", ".ppt", ".rtf"):
            with open(_long(p), "rb") as f:
                data = f.read(5 * 2**20)
            if ext == ".rtf":
                out.append(re.sub(r"\\[a-z]+-?\d* ?|[{}]|\\'[0-9a-f]{2}", " ", data.decode("latin-1")))
            else:  # two-byte text (English and Devanagari), then plain runs
                out += [m.decode("utf-16-le", "replace")
                        for m in re.findall(rb"(?:[\x20-\x7e]\x00|[\x00-\x7f]\x09){4,}", data)]
                out += [m.decode("latin-1") for m in re.findall(rb"[\x20-\x7e]{6,}", data)]
    except Exception:  # anything a broken or locked-with-a-password file throws: it is found by its name
        return ""
    return re.sub(r"[ \t]+", " ", re.sub(r"\s*\n\s*", "\n", "\n".join(out))).strip()[:limit]


# ---------------- one run ----------------
def _born(con, pc: str) -> dict:
    """{folder: when it was first read}: every file found that day carries the same time."""
    return dict(con.execute("SELECT root, min(first) FROM files WHERE pc=? GROUP BY root", (pc,)))


def _old(seen: float, m: float, born: float | None) -> bool:
    """Was it there on the first day, and by then untouched for RECENT_DAYS? Judged against that day, not today: a
    copied file never turns old."""
    return born is not None and seen == born and m < born - RECENT_DAYS * 86400


def trim(con) -> int:
    """Copies that the rules no longer keep are removed (the limits were wider at first): documents over COPY_MAX,
    and those old when their PC was first read. Their words stay in the index. Nothing else is ever deleted here."""
    n = 0
    for pc in {r[0] for r in con.execute("SELECT DISTINCT pc FROM files WHERE copied=1")}:
        born = _born(con, pc)
        for i, label, d, f, size, m, seen in con.execute(
                "SELECT id, root, dir, name, size, mtime, first FROM files WHERE pc=? AND copied=1", (pc,)).fetchall():
            state = 2 if size > COPY_MAX else 3 if _old(seen, m, born.get(label)) else 0
            if state:
                dest = _mirror(pc, label, d, f)
                for p in (dest, *_versions(dest)):
                    try:
                        os.remove(_long(p))
                    except OSError:
                        pass
                con.execute("UPDATE files SET copied=? WHERE id=?", (state, i))
                n += 1
    con.commit()
    return n


def fetch(path: str) -> str:
    """One file straight from its PC, for a file with no copy here (old, big, a photo). -> where it now is."""
    path = str(path).strip()
    for pc in pcs():
        for label, root in pc["roots"].items():
            base = root.rstrip("\\/")
            if path.lower().startswith(base.lower() + os.sep):
                # only a file the index knows, rebuilt from its own folder and name: a path with ".." in it, or one
                # made up, can neither read outside the shared folder nor write outside fetched/
                parts = re.split(r"[\\/]", path[len(base) + 1:])
                con = db()
                try:
                    row = None if any(p in ("", ".", "..") for p in parts) else con.execute(
                        "SELECT dir, name FROM files WHERE pc=? AND root=? AND dir=? COLLATE NOCASE AND name=? COLLATE NOCASE "
                        "AND gone IS NULL", (pc["name"], label, "/".join(parts[:-1]), parts[-1])).fetchone()
                finally:
                    con.close()
                if not row:
                    raise ValueError("path: a file's path on its PC, as find gives it (not deleted)")
                if not _reach(pc, root):
                    raise ConnectionError(f"{pc['name']} is off or its folder doesn't open right now: the file is at {path}")
                src, dest = _at(root, *row), HOME / "fetched" / pc["name"] / label / _at("", *row)
                if os.path.getsize(_long(src)) > FETCH_MAX:
                    raise ValueError(f"over {FETCH_MAX // 2**20} MB: too big to send; it is at {path}")
                os.makedirs(os.path.dirname(_long(dest)), exist_ok=True)
                shutil.copyfile(_long(src), _long(dest) + ".part")
                os.replace(_long(dest) + ".part", _long(dest))
                return str(dest)
    raise ValueError("path: a file's path on its PC, as find gives it")


def _sweep(now: float):
    """Fetched files are a week's convenience, not a record."""
    for folder, _, files in os.walk(_long(HOME / "fetched")):
        for f in files:
            p = os.path.join(folder, f)
            try:
                if os.path.getmtime(p) < now - FETCH_DAYS * 86400:
                    os.remove(p)
            except OSError:
                pass



def _reach(pc: dict, path: str) -> bool:
    """Is the folder there right now? A PC that is off answers in 2 seconds, not Windows' half minute."""
    if path.startswith("\\\\") and not path.startswith("\\\\?\\"):
        host, share = (path[2:].split("\\") + [""])[:2]
        try:
            socket.create_connection((host, 445), 2).close()
        except OSError:
            return False
        if not os.path.isdir(path) and pc.get("user"):  # sign in to the share with the read-only account
            subprocess.run(["net", "use", f"\\\\{host}\\{share}", pc.get("password") or "", f"/user:{pc['user']}"],
                           capture_output=True, timeout=30)
    return os.path.isdir(path)


def _where(label, d) -> str:
    return f"{label}/{d}" if d else label


def _line(where: str, what: str, items: list) -> str:
    """One report line for the files of one place: their names, or past COLLAPSE only counts by type, the first and
    last name, and the documents among them by name (so 1,000 photos never hide the one Excel sheet)."""
    names = sorted(n for n, _ in items)
    if len(items) <= COLLAPSE:
        return f"- {where}: {what} " + ", ".join(f"{n} ({_size(s)})" for n, s in sorted(items))
    exts = collections.Counter(os.path.splitext(n)[1].lower().lstrip(".") or "no type" for n in names).most_common()
    s = (f"- {where}: {what} {len(items):,} files, {_size(sum(x for _, x in items))}: "
         + ", ".join(f"{c:,} {e}" for e, c in exts[:6]) + (f", {sum(c for _, c in exts[6:]):,} other" if exts[6:] else "")
         + f"; names {names[0]} … {names[-1]}")
    docs = [n for n in names if kind(n) == "doc"]
    if docs and len(docs) < len(items):
        s += "; documents: " + ", ".join(docs[:10]) + (f" and {len(docs) - 10:,} more" if len(docs) > 10 else "")
    return s


def _report(evs: dict, moved: list) -> list[str]:
    lines, new, was = [], collections.defaultdict(list), {id(g) for g, _ in moved}
    here = {id(n) for _, n in moved}
    for label, ev in evs.items():
        for x in ev["new"]:
            if id(x) in here:
                continue
            d, top = x[1], None
            parts = d.split("/") if d else []
            for i in range(1, len(parts) + 1):  # the top-most folder that is itself new holds the whole batch
                if "/".join(parts[:i]) in ev["newdirs"]:
                    top = "/".join(parts[:i])
                    break
            new[(_where(label, top) + "/ (new folder)" if top else _where(label, d))].append((x[2], x[3]))
    lines += [_line(w, "new" if len(v) <= COLLAPSE else "+", v) for w, v in sorted(new.items())]
    for what, key, skip in (("changed", "changed", ()), ("deleted", "gone", was)):
        group = collections.defaultdict(list)
        for label, ev in evs.items():
            for x in ev[key]:
                if id(x) not in skip:
                    group[_where(label, x[1])].append((x[2], x[3]))
        lines += [_line(w, what, v) for w, v in sorted(group.items())]
    group = collections.defaultdict(list)
    for g, n in moved:
        group[(_where(n[0], n[1]), _where(g[0], g[1]))].append((n[2], n[3]))
    lines += [_line(to, f"moved here from {frm}:", v) for (to, frm), v in sorted(group.items())]
    lines += [f"- {label}: {ev['errors']} folders could not be read this time (left as they were)"
              for label, ev in evs.items() if ev["errors"]]
    return lines


def run_pc(con, pc: dict, now: float | None = None, budget: float = COPY_SECS) -> dict:
    """One PC, once: list, compare, copy, read the words, report. A PC that is off is left exactly as it was."""
    now, name, roots, evs = now or time.time(), pc["name"], pc["roots"], {}
    for label, path in roots.items():
        if not _reach(pc, path):
            continue
        r = con.execute("SELECT full, secs FROM roots WHERE pc=? AND root=?", (name, label)).fetchone()
        quick = bool(r and r[1] > SLOW_SECS and now - r[0] < FULL_HOURS * 3600)
        t = time.monotonic()
        ev = scan_root(con, name, label, path, now, quick)
        if "" not in ev["listed"]:  # the shared folder itself didn't open: off, or no rights
            continue
        ev["quick"], ev["secs"] = quick, time.monotonic() - t
        evs[label] = ev
    if not evs:
        return {"pc": name, "on": False}
    moved = _pairs([x for ev in evs.values() for x in ev["new"]], [x for ev in evs.values() for x in ev["gone"]])
    carried, first = {id(n): g for g, n in moved}, all(ev["first"] for ev in evs.values())
    for label, ev in evs.items():
        hot = set()
        for x in ev["gone"]:
            con.execute("UPDATE files SET gone=? WHERE pc=? AND root=? AND dir=? AND name=?", (now, name, label, *x[1:3]))
        for x in ev["changed"]:
            con.execute("UPDATE files SET size=?, mtime=?, changed=?, copied=0 WHERE pc=? AND root=? AND dir=? AND name=?",
                        (x[3], x[4], now, name, label, x[1], x[2]))
        for x in ev["new"]:
            i = _put(con, name, *x, now)
            if g := carried.get(id(x)):  # moved: its copy and its words move too, nothing is fetched again
                old = con.execute("SELECT id, copied FROM files WHERE pc=? AND root=? AND dir=? AND name=?",
                                  (name, *g[:3])).fetchone()
                src, dest = _long(_mirror(name, *g[:3])), _long(_mirror(name, *x[:3]))
                if old and old[1] == 1 and os.path.exists(src) and not os.path.exists(dest):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    os.replace(src, dest)
                    con.execute("UPDATE files SET copied=1 WHERE id=?", (i,))
                    con.execute("UPDATE words SET body=(SELECT body FROM words WHERE rowid=?) WHERE rowid=?", (old[0], i))
        for sd in ev["lostdirs"]:
            con.execute(f"DELETE FROM dirs WHERE pc=? AND root=? AND {UNDER}", (name, label, *_under(sd)))
        for d, mt in ev["listed"].items():
            con.execute("INSERT INTO dirs(pc, root, dir, up, mtime, changed) VALUES(?,?,?,?,?,?) ON CONFLICT(pc, root, dir) "
                        "DO UPDATE SET mtime=excluded.mtime",
                        (name, label, d, d.rpartition("/")[0] if d else None, mt, now if d in ev["newdirs"] else 0))
        for x in ev["new"] + ev["changed"] + ev["gone"]:  # these folders, and those above them, are in use
            d = x[1]  # the first look: only where a file's own date is recent
            while d not in hot and not (ev["first"] and x[4] < now - QUIET_DAYS * 86400):
                hot.add(d)
                d = d.rpartition("/")[0]
        con.executemany("UPDATE dirs SET changed=? WHERE pc=? AND root=? AND dir=?", [(now, name, label, d) for d in hot])
        con.execute("INSERT INTO roots(pc, root, full, secs, seen) VALUES(?,?,?,?,?) ON CONFLICT(pc, root) DO UPDATE SET "
                    "seen=excluded.seen" + ("" if ev["quick"] else ", full=excluded.full, secs=excluded.secs"),
                    (name, label, now, ev["secs"], now))
        con.commit()

    done = held = 0
    end = time.monotonic() + budget
    born = _born(con, name)
    todo = con.execute("SELECT id, root, dir, name, size, mtime, first FROM files WHERE pc=? AND kind='doc' AND gone IS NULL "
                       "AND copied=0 ORDER BY changed DESC, mtime DESC, id", (name,)).fetchall()  # the newest work first
    for n, (i, label, d, f, size, m, seen) in enumerate(todo):
        if time.monotonic() > end:
            break
        if label not in evs:
            continue
        if size > COPY_MAX:
            con.execute("UPDATE files SET copied=2 WHERE id=?", (i,))
            continue
        src = _at(roots[label], d, f)
        if _old(seen, m, born.get(label)) and not os.path.exists(_long(_mirror(name, label, d, f))):
            try:  # old when the PC was first read: only its words, read over the LAN; no copy kept
                with open(_long(src), "rb"):
                    pass
            except OSError:
                held += 1
                continue
            con.execute("UPDATE files SET copied=3 WHERE id=?", (i,))
            con.execute("UPDATE words SET body=? WHERE rowid=?", (text_of(src), i))
            continue
        try:
            ok = _copy(src, _mirror(name, label, d, f), size, m, now)
        except OSError:
            ok = False
        if ok:
            con.execute("UPDATE files SET copied=1 WHERE id=?", (i,))
            con.execute("UPDATE words SET body=? WHERE rowid=?", (text_of(_mirror(name, label, d, f)), i))
            done += 1
        else:
            held += 1
        if n % 50 == 49:
            con.commit()
    con.commit()
    left = con.execute("SELECT count(*) FROM files WHERE pc=? AND kind='doc' AND gone IS NULL AND copied=0",
                       (name,)).fetchone()[0]

    counts = {k: sum(len(ev[k]) for ev in evs.values()) for k in ("new", "changed", "gone")}
    if first:  # the first look at a PC: everything is "new", so one line, not lakhs
        by = collections.Counter(kind(x[2]) for ev in evs.values() for x in ev["new"])
        lines = [f"- first look: {counts['new']:,} files ({by['doc']:,} documents, {by['photo']:,} photos, "
                 f"{by['video']:,} videos, {by['other']:,} other) in {', '.join(evs)}; see pcs/{name}.md"]
    else:
        lines = _report(evs, moved)
    if left:
        lines.append(f"- {left:,} documents still to copy (open on the PC, or the first copy is still going)")
    if lines:
        f = HOME / "changes" / name / f"{datetime.fromtimestamp(now):%Y-%m-%d}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as out:
            out.write(f"\n## {datetime.fromtimestamp(now):%H:%M}\n" + "\n".join(lines) + "\n")
    if first or any(ev["newdirs"] or ev["lostdirs"] for ev in evs.values()) or not (HOME / "pcs" / f"{name}.md").exists():
        write_pc(con, pc, now)
    return {"pc": name, "on": True, **counts, "moved": len(moved), "copied": done, "waiting": left,
            "secs": round(sum(ev["secs"] for ev in evs.values()), 1), "lines": lines}


def write_pc(con, pc: dict, now: float):
    """pcs/<PC>.md: the PC's folders two levels down, with counts and when each last changed."""
    name, out = pc["name"], [f"# {pc['name']}", f"As of {datetime.fromtimestamp(now):%d/%m/%Y %H:%M}. "
                             "Copies of the documents: mirror/" + pc["name"] + "/<folder>/..."]
    for label, path in pc["roots"].items():
        tree = collections.defaultdict(lambda: [0, 0, 0, 0.0])
        for d, n, size, docs, ch in con.execute(
                "SELECT dir, count(*), sum(size), sum(kind='doc'), max(changed) FROM files WHERE pc=? AND root=? AND "
                "gone IS NULL GROUP BY dir", (name, label)):
            t = tree["/".join(d.split("/")[:2]) if d else "(top)"]
            t[0], t[1], t[2], t[3] = t[0] + n, t[1] + (size or 0), t[2] + (docs or 0), max(t[3], ch or 0)
        out.append(f"\n## {label}  ({path})\n{sum(t[0] for t in tree.values()):,} files, "
                   f"{_size(sum(t[1] for t in tree.values()))}")
        rows = sorted(tree.items(), key=lambda kv: -kv[1][3])
        out += [f"- {d}: {t[0]:,} files ({t[2]:,} documents), {_size(t[1])}, last change "
                f"{datetime.fromtimestamp(t[3]):%d/%m/%Y}" for d, t in rows[:60]]
        if rows[60:]:
            out.append(f"- and {len(rows) - 60:,} more folders with nothing recent")
    f = HOME / "pcs" / f"{name}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(out) + "\n", encoding="utf-8")


def run_all() -> list[dict]:
    out, con = [], db()
    try:
        trim(con)
        _sweep(time.time())
        for pc in pcs():
            try:
                out.append(run_pc(con, pc))
            except Exception as e:  # one PC's trouble never stops the others
                con.rollback()
                out.append({"pc": pc.get("name"), "error": repr(e)})
                print("pc files:", pc.get("name"), repr(e))
    finally:
        con.close()
    return out


# ---------------- what Raion asks ----------------
def _full(roots: dict, label, d, n) -> str:
    return _at(roots.get(label, label), d, n)


def _day(s, end=False) -> float | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], "%d/%m/%Y").timestamp() + (86400 if end else 0)
    except ValueError:
        raise ValueError("since / until: dd/mm/yyyy")


def find(q: str, pc=None, kind_=None, deleted=False, limit=10, order="best", since=None, until=None,
         now: float | None = None) -> list[dict]:
    """Files by the words in their name, folder or (documents) inside them. All the words, else any.
    order: best = the closest match, a file saved lately ahead of an equal one from years back (today counts double,
    a year old 1.5, five years 1.2); newest / oldest = by the date the file was last saved. since / until narrow by
    that date."""
    toks = mem.words(q)
    if not toks:
        return []
    paths, now = {p["name"]: p["roots"] for p in pcs()}, now or time.time()
    lo, hi = _day(since), _day(until, True)
    by = {"newest": "f.mtime DESC", "oldest": "f.mtime ASC"}.get(
        order, "bm25(words, 8.0, 3.0, 1.0) * (1.0 + 1.0 / (1.0 + max(0.0, ? - f.mtime) / 31536000.0))")
    sql = ("SELECT f.pc, f.root, f.dir, f.name, f.size, f.kind, f.mtime, f.changed, f.gone, f.copied, "
           "snippet(words, 2, '[', ']', ' … ', 14) FROM words JOIN files f ON f.id = words.rowid WHERE words MATCH ? "
           + ("AND f.pc = ? " if pc else "") + ("AND f.kind = ? " if kind_ else "")
           + ("AND f.mtime >= ? " if lo else "") + ("AND f.mtime < ? " if hi else "")
           + ("" if deleted else "AND f.gone IS NULL ") + f"ORDER BY {by} LIMIT ?")
    con = db()
    try:
        for glue in (" ", " OR "):
            rows = con.execute(sql, (glue.join(f'"{t}"*' for t in toks), *((pc,) if pc else ()),
                                     *((kind_,) if kind_ else ()), *((lo,) if lo else ()), *((hi,) if hi else ()),
                                     *((now,) if "bm25" in by else ()), max(1, min(int(limit or 10), 50)))).fetchall()
            if rows:
                break
    finally:
        con.close()
    day = lambda t: f"{datetime.fromtimestamp(t):%d/%m/%Y %H:%M}"
    return [{"pc": p, "path": _full(paths.get(p, {}), label, d, n), "kind": k, "size": _size(size),
             "saved": day(m), **({"deleted": day(gone)} if gone else {}),
             **({"copy": _mirror(p, label, d, n)} if copied == 1 and os.path.exists(_long(_mirror(p, label, d, n)))
                else {"copy": None, "why": {0: "not copied yet", 2: f"over {COPY_MAX // 2**20} MB", 3: "old"}.get(
                    copied if k == "doc" else -1, "photos, videos and the rest are only listed")
                    + ("" if gone else ": fetch gets it from the PC while that PC is on")}),
             **({"inside": snip} if snip.strip() else {})}
            for p, label, d, n, size, k, m, ch, gone, copied, snip in rows]


def changes(pc=None, since: str | None = None, room=8000) -> str:
    """The change reports (and the day's note) from a date on (dd/mm/yyyy; today when not given), newest kept."""
    try:
        start = datetime.strptime(since.strip()[:10], "%d/%m/%Y").strftime("%Y-%m-%d") if since else None
    except ValueError:
        raise ValueError("since: dd/mm/yyyy")
    start, out = start or datetime.now().strftime("%Y-%m-%d"), []
    for f in sorted((HOME / "notes").glob("*.md")):
        if f.stem >= start:
            out.append(f"# Note for {f.stem}\n{f.read_text(encoding='utf-8').strip()}")
    for f in sorted((HOME / "changes").glob("*/*.md"), key=lambda f: (f.stem, f.parent.name)):
        if f.stem >= start and pc in (None, f.parent.name):
            out.append(f"# {f.parent.name} · {f.stem}\n{f.read_text(encoding='utf-8').strip()}")
    text = "\n\n".join(out)
    return text[-room:] if text else "no changes seen since " + start


def day_brief(now: float | None = None, most=30) -> str | None:
    """What the evening note is written from: today's change reports, and the first words of the documents that
    are new or changed today (the latest `most`). None on a day with no change."""
    now = now or time.time()
    day = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    rep = [f"# {f.parent.name}\n{f.read_text(encoding='utf-8').strip()}"
           for f in sorted((HOME / "changes").glob(f"*/{day:%Y-%m-%d}.md"))]
    if not rep:
        return None
    con = db()
    try:
        rows = con.execute("SELECT f.pc, f.root, f.dir, f.name, w.body FROM files f JOIN words w ON w.rowid = f.id WHERE "
                           "f.kind='doc' AND f.gone IS NULL AND f.copied=1 AND f.changed >= ? ORDER BY f.changed DESC "
                           "LIMIT ?", (day.timestamp(), most)).fetchall()
    finally:
        con.close()
    return ("\n\n".join(rep)[-12000:] + "\n\n# The first words of today's new and changed documents\n"
            + "\n".join(f"- {p} {_where(label, d)}/{n}: {' '.join(body.split())[:300] or '(no text: a scan?)'}"
                        for p, label, d, n, body in rows))


def status() -> list[dict]:
    con = db()
    try:
        out = []
        for p in pcs():
            n, docs, wait = con.execute("SELECT count(*), sum(kind='doc'), sum(kind='doc' AND copied=0) FROM files "
                                        "WHERE pc=? AND gone IS NULL", (p["name"],)).fetchone()
            seen = con.execute("SELECT max(seen) FROM roots WHERE pc=?", (p["name"],)).fetchone()[0]
            out.append({"pc": p["name"], "folders": p["roots"], "files": n, "documents": docs or 0,
                        "to_copy": wait or 0, "last_seen_on": f"{datetime.fromtimestamp(seen):%d/%m/%Y %H:%M}"
                        if seen else "never (off, or the share doesn't open)"})
        return out
    finally:
        con.close()


def probe(path: str, pc: dict | None = None) -> dict:
    """Only counts: is the folder reachable, how many files of what kind, and how long one full listing takes."""
    if not _reach(pc or {}, path):
        return {"folder": path, "reachable": False}
    t, n, size, dirs, errors, exts, kinds = time.monotonic(), 0, 0, 0, 0, collections.Counter(), collections.Counter()
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            files, subs = _list(d)
        except OSError:
            errors += 1
            continue
        dirs += 1
        stack += [os.path.join(d, s) for s in subs]
        for f, (s, _) in files.items():
            n, size = n + 1, size + s
            exts[os.path.splitext(f)[1].lower() or "(none)"] += 1
            kinds[kind(f)] += 1
    return {"folder": path, "reachable": True, "files": n, "size": _size(size), "folders": dirs, "unreadable": errors,
            "listing_seconds": round(time.monotonic() - t, 1), "by_kind": dict(kinds), "top_types": dict(exts.most_common(12))}


def _label(path: str, taken) -> str:
    base = re.sub(r"[^\w .()-]", "", path.rstrip("\\/").replace(":", "").rpartition("\\")[2]) or "folder"
    return next(x for x in (base, *(f"{base} {i}" for i in range(2, 99))) if x not in taken)


def tool(a: dict) -> dict:
    """Raion's pc_files tool (admins only; wa_agent checks that)."""
    act, name = str(a.get("action") or ""), re.sub(r"[^\w -]", "", str(a.get("pc") or "")).strip()
    if act == "find":
        return {"found": find(str(a.get("query") or ""), name or None, a.get("kind"), bool(a.get("deleted")),
                              a.get("limit") or 10, a.get("order") or "best", a.get("since"), a.get("until"))}
    if act == "fetch":
        return {"copy": fetch(str(a.get("path") or "")), "kept": f"{FETCH_DAYS} days"}
    if act == "rename":  # only the label: the index, the copies and the reports go with it, nothing is read again
        new, all_ = re.sub(r"[^\w -]", "", str(a.get("to") or "")).strip(), pcs()
        pc = next((p for p in all_ if p["name"] == name), None)
        if not pc or not new or any(p["name"].lower() == new.lower() for p in all_ if p is not pc):
            raise ValueError("rename: pc = a PC's name now, to = its new name (letters, digits, spaces; not one in use)")
        moved = []
        try:
            for sub in ("mirror", "versions", "changes", "fetched"):
                if (HOME / sub / name).is_dir():
                    os.rename(HOME / sub / name, HOME / sub / new)
                    moved.append(sub)
        except OSError as e:  # a file of it is open somewhere: put back what moved, change nothing
            for sub in moved:
                os.rename(HOME / sub / new, HOME / sub / name)
            raise ValueError(f"couldn't rename now ({e.strerror}); try again in a few minutes")
        con = db()
        try:
            for t in ("files", "dirs", "roots"):
                con.execute(f"UPDATE {t} SET pc=? WHERE pc=?", (new, name))
            con.commit()
        finally:
            con.close()
        (HOME / "pcs" / f"{name}.md").unlink(missing_ok=True)  # written again under the new name at the next run
        pc["name"] = new
        CONFIG.write_text(json.dumps(all_, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"renamed": name, "to": new}
    if act == "changes":
        return {"changes": changes(name or None, a.get("since"))}
    if act == "pcs":
        return {"pcs": status(), "kept_in": str(HOME)}
    if act == "read":  # a document's words, from its copy: Excel and Word don't open with Read
        p = Path(str(a.get("path") or "")).resolve()
        if not p.is_relative_to(HOME.resolve()) or not p.is_file():
            raise ValueError("path: a copy under " + str(HOME / "mirror") + " (find gives it)")
        return {"text": text_of(p) or "(no text in it: a scan, a picture or a password)"}
    if act == "probe":
        pc = next((p for p in pcs() if p["name"] == name), None)
        if a.get("folder"):
            return probe(str(a["folder"]), pc or {"user": a.get("user"), "password": a.get("password")})
        if not pc:
            raise ValueError("probe: a folder (\\\\192.168.1.50\\D), or the name of a PC already added")
        return {"probes": [probe(path, pc) for path in pc["roots"].values()]}
    if act in ("add", "remove"):
        if not name:
            raise ValueError("pc: a short name for the PC (letters, digits, spaces)")
        all_ = pcs()
        pc = next((p for p in all_ if p["name"] == name), None)
        if act == "remove":
            if not pc:
                raise ValueError(f"no PC named '{name}'")
            all_.remove(pc)
            note = "no longer read; its index, copies and reports are kept"
        else:
            pc = pc or all_.append({"name": name, "roots": {}}) or all_[-1]
            for path in a.get("folders") or []:
                path = str(path).strip().replace("/", "\\")
                if path and path not in pc["roots"].values():
                    pc["roots"][_label(path, pc["roots"])] = path
            for k in ("user", "password"):
                if a.get(k):
                    pc[k] = str(a[k])
            if not pc["roots"]:
                raise ValueError("folders: the shared folders to read, like \\\\192.168.1.50\\D")
            note = {path: "opens" if _reach(pc, path) else "does not open (PC off, share name, or the account)"
                    for path in pc["roots"].values()}
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(all_, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"saved": name, "folders": note}
    raise ValueError("action: find, read, fetch, changes, pcs, probe, add, rename, remove or scan")


# ---------------- the self-check: a made-up messy PC ----------------
def _test():
    import tempfile
    import openpyxl
    global HOME, CONFIG, SLOW_SECS
    tmp = Path(tempfile.mkdtemp(prefix="pcfiles_"))
    real = (HOME, CONFIG, SLOW_SECS)
    HOME, CONFIG = tmp / "raion", tmp / "pcs.json"
    D, T = tmp / "pc" / "D", 1_800_000_000.0  # T: the made-up "now"; each run is 15 minutes on

    def put(rel, data=b"x", age=0):
        f = D / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data) if isinstance(data, bytes) else data(f)
        os.utime(f, (T - age, T - age))
        return f

    def xlsx(rows):
        def make(f):
            wb = openpyxl.Workbook()
            wb.active.title = "Fee 2026"
            for r in rows:
                wb.active.append(r)
            wb.save(f)
        return make

    def docx(text):
        def make(f):
            with zipfile.ZipFile(f, "w") as z:
                z.writestr("word/document.xml", f"<w:document><w:p><w:t>{text}</w:t></w:p></w:document>")
        return make

    try:
        put("Desktop/New folder/Book1.xlsx", xlsx([["Roll No", "Name", "Tuition", "Dues"], [2601, "Rahul Sharma", 12000, 500]]))
        put("Accounts/letter.docx", docx("Transfer certificate for Priya Verma"))
        put("Accounts/पत्र सूची.txt", "छात्रवृत्ति की सूची".encode("utf-8"))
        put("Accounts/~$letter.docx"), put("Accounts/Thumbs.db"), put("AppData/Local/cache.txt"), put("setup.exe", b"MZ" * 50)
        put("Old/2019/result.csv", b"name,marks\nAman,91\n", age=400 * 86400)
        pc = {"name": "PC2", "roots": {"D": str(D)}}
        CONFIG.write_text(json.dumps([pc]), encoding="utf-8")
        con = db()
        mirror = HOME / "mirror" / "PC2" / "D"

        put("Scholarship/list 2026.txt", b"scholarship list of students"), put("Scholarship/list 2024.txt", b"scholarship list of students", age=700 * 86400)
        r = run_pc(con, pc, T)  # 1. the first look: recent documents copied, junk never seen, a program only listed
        assert r["new"] == 7 and r["copied"] == 4 and r["waiting"] == 0 and "first look" in r["lines"][0], r
        assert (mirror / "Desktop/New folder/Book1.xlsx").exists() and not (mirror / "setup.exe").exists()
        # untouched for over a year on that day: no copy, but found by its words, and fetched from the PC when asked
        old = find("aman marks")[0]
        assert old["copy"] is None and "old: fetch" in old["why"] and not list(mirror.rglob("result.csv")), old
        assert Path(fetch(old["path"])).read_bytes().startswith(b"name,marks") and Path(fetch(str(D / "setup.exe"))).exists()
        (tmp / "pc" / "secret.txt").write_text("outside the shared folder")
        for bad in (str(D / ".." / "secret.txt"), str(D / "Accounts" / ".." / ".." / "secret.txt"), str(D / "never seen.txt")):
            try:  # fetch takes only files the index knows: no way out of the shared folder, or of fetched/
                fetch(bad)
                raise AssertionError(f"fetched {bad}")
            except ValueError:
                pass
        assert not list(HOME.rglob("secret.txt"))
        # the same words in a new file and an old one: the new one first, unless asked for the oldest; dates narrow
        got = lambda **k: [Path(x["path"]).name for x in find("scholarship list", now=T, **k)]
        assert got() == ["list 2026.txt", "list 2024.txt"] and got(order="oldest")[0] == "list 2024.txt", got()
        assert got(since=f"{datetime.fromtimestamp(T - 30 * 86400):%d/%m/%Y}") == ["list 2026.txt"]
        assert got(until=f"{datetime.fromtimestamp(T - 30 * 86400):%d/%m/%Y}") == ["list 2024.txt"]
        assert not list(mirror.rglob("cache.txt")) and not list(mirror.rglob("~$*"))
        assert find("tuition dues")[0]["path"].endswith("Book1.xlsx"), find("tuition dues")  # by what's inside
        assert find("rahul 2601")[0]["copy"] and find("priya certificate")[0]["path"].endswith("letter.docx")
        assert find("छात्रवृत्ति")[0]["path"].endswith("पत्र सूची.txt") and find("setup")[0]["copy"] is None
        assert run_pc(con, pc, T + 60)["lines"] == []  # nothing different: nothing written

        T2 = T + 900  # 2. a document changes: the morning's copy is kept, twice in a day keeps the first
        put("Accounts/letter.docx", docx("Bonafide certificate for Priya Verma"), age=-900)
        r = run_pc(con, pc, T2)
        assert r["changed"] == 1 and "changed letter.docx" in r["lines"][0], r
        ver = list((HOME / "versions" / "PC2" / "D" / "Accounts").glob("letter.*.docx"))
        assert len(ver) == 1 and "Transfer" in text_of(ver[0]) and "Bonafide" in text_of(mirror / "Accounts/letter.docx")
        assert find("bonafide") and not find("transfer")
        put("Accounts/letter.docx", docx("Third wording"), age=-1000)
        run_pc(con, pc, T2 + 100)
        assert len(list(ver[0].parent.glob("letter.*.docx"))) == 1 and "Transfer" in text_of(ver[0])

        T3 = T2 + 900  # 3. deleted on the PC: marked, the copy stays, still found when asked for deleted ones
        (D / "Accounts/पत्र सूची.txt").unlink()
        r = run_pc(con, pc, T3)
        assert r["gone"] == 1 and "deleted" in r["lines"][0] and (mirror / "Accounts/पत्र सूची.txt").exists(), r
        assert not find("छात्रवृत्ति") and find("छात्रवृत्ति", deleted=True)[0]["deleted"]

        T4 = T3 + 900  # 4. a folder renamed: moved, the copies move with it, nothing fetched again
        (D / "Desktop/New folder").rename(D / "Desktop/Fees 2026")
        calls, real_copy = [], shutil.copyfile
        shutil.copyfile = lambda *a, **k: calls.append(a) or real_copy(*a, **k)
        r = run_pc(con, pc, T4)
        assert r["moved"] == 1 and not calls and "moved here from D/Desktop/New folder" in r["lines"][0], r
        assert (mirror / "Desktop/Fees 2026/Book1.xlsx").exists() and not (mirror / "Desktop/New folder/Book1.xlsx").exists()
        assert find("tuition")[0]["path"].endswith(os.path.join("Fees 2026", "Book1.xlsx"))

        T5 = T4 + 900  # 5. a dump of 1,000 photos with one sheet among them: one line, the sheet named and copied
        for i in range(1000):
            put(f"Admissions/Photos 2026/set{i % 4}/{2601 + i}.jpg", b"j" * 2048, age=-3000)
        put("Admissions/Photos 2026/student_list.xlsx", xlsx([["Name"], ["Sana Khan"]]), age=-3000)
        calls.clear()
        r = run_pc(con, pc, T5)
        assert len(r["lines"]) == 1 and "1,000 jpg" in r["lines"][0] and "student_list.xlsx" in r["lines"][0], r["lines"]
        assert "D/Admissions/ (new folder)" in r["lines"][0] and len(calls) == 1 and not list(mirror.rglob("*.jpg"))
        assert find("sana khan")[0]["copy"] and find("2700", kind_="photo")[0]["copy"] is None

        T6 = T5 + 900  # 6. open in Excel (locked): waits, and goes at the next run; the old copy stays meanwhile
        put("Accounts/letter.docx", docx("Fourth wording"), age=-5000)

        def locked(*a, **k):
            raise PermissionError("in use")
        shutil.copyfile = locked
        r = run_pc(con, pc, T6)
        assert r["waiting"] == 1 and "Third" in text_of(mirror / "Accounts/letter.docx") and "still to copy" in r["lines"][-1]
        shutil.copyfile = real_copy
        assert run_pc(con, pc, T6 + 60)["copied"] == 1 and "Fourth" in text_of(mirror / "Accounts/letter.docx")

        T7 = T6 + 900  # 7. a folder that can't be read, then the whole PC off: nothing is marked deleted
        real_list, n0 = _list, con.execute("SELECT count(*) FROM files WHERE gone IS NULL").fetchone()[0]

        def flaky(path):
            if path.endswith("Accounts"):
                raise OSError("network name no longer available")
            return real_list(path)
        globals()["_list"] = flaky
        r = run_pc(con, pc, T7)
        assert r["gone"] == 0 and "could not be read" in r["lines"][-1], r
        globals()["_list"] = real_list
        assert run_pc(con, {"name": "PC2", "roots": {"D": str(tmp / "nowhere")}}, T7 + 60) == {"pc": "PC2", "on": False}
        assert con.execute("SELECT count(*) FROM files WHERE gone IS NULL").fetchone()[0] == n0

        T8 = T7 + 900  # 8. a slow PC: quiet folders are skipped, new and busy ones still seen; the full run finds the rest
        con.execute("UPDATE roots SET secs=999, full=?", (T8,))
        put("Old/2019/deep/late.txt", b"deep", age=-7000)  # deep in a quiet folder: Old/'s own date doesn't change
        os.utime(D / "Old", (T - 400 * 86400,) * 2), os.utime(D / "Old/2019", (T - 400 * 86400,) * 2)
        con.execute("UPDATE dirs SET mtime=? WHERE dir IN ('Old', 'Old/2019')", (T - 400 * 86400,))
        put("Accounts/new_today.txt", b"today", age=500 * 86400), put("Brand new/first.txt", b"hello", age=-7000)
        r = run_pc(con, pc, T8 + 60)
        assert r["new"] == 2 and not find("late"), r
        assert find("today")[0]["copy"]  # an old date, but it turned up after the first day: someone's work now, copied
        r = run_pc(con, pc, T8 + (FULL_HOURS + 1) * 3600)
        assert r["new"] == 1 and find("late")[0]["copy"], r

        put("Accounts/letter.docx", docx("Fifth wording"), age=-90000)  # 9. another day: still one earlier copy, the newer
        run_pc(con, pc, T + 2 * 86400)
        ver = list((HOME / "versions" / "PC2" / "D" / "Accounts").glob("letter.*.docx"))
        assert len(ver) == 1 and "Fourth" in text_of(ver[0]) and "Fifth" in text_of(mirror / "Accounts/letter.docx"), ver

        assert "PC2 · " in changes(since=f"{datetime.fromtimestamp(T):%d/%m/%Y}") and "Fees 2026" in (HOME / "pcs/PC2.md").read_text("utf-8")
        brief = day_brief(T8 + 60)
        assert "new_today.txt" in brief and "first.txt: hello" in brief and day_brief(T - 5 * 86400) is None, brief
        assert tool({"action": "pcs"})["pcs"][0]["documents"] >= 5
        assert "Fifth" in tool({"action": "read", "path": str(mirror / "Accounts/letter.docx")})["text"]
        try:
            tool({"action": "read", "path": str(CONFIG)})
            raise AssertionError("read outside the copies")
        except ValueError:
            pass
        assert tool({"action": "add", "pc": "PC3", "folders": [str(D)]})["folders"][str(D)] == "opens" and len(pcs()) == 2
        assert probe(str(D))["by_kind"]["photo"] == 1000

        # 10. the limits were wider once: copies over the size limit, and of files old on the first day, are removed
        # (their earlier copy too); the words stay, so they are still found
        con.execute("UPDATE files SET copied=1 WHERE name='list 2024.txt'")
        (mirror / "Scholarship/list 2024.txt").write_bytes(b"scholarship list of students")
        globals()["COPY_MAX"] = 50  # bytes: letter.docx is over it, the small text files are not
        try:
            assert trim(con) >= 2 and not (mirror / "Scholarship/list 2024.txt").exists()
            assert not (mirror / "Accounts/letter.docx").exists() and not list(ver[0].parent.glob("letter.*.docx"))
            assert (mirror / "Scholarship/list 2026.txt").exists() and trim(con) == 0
            hit = find("fifth wording")[0]
            assert hit["copy"] is None and "over 0 MB" in hit["why"] and "old" in find("scholarship", order="oldest")[0]["why"]
        finally:
            globals()["COPY_MAX"] = 20 * 2**20

        # 11. a PC renamed: the same index, copies and reports under the new name; nothing read or copied again
        n0 = con.execute("SELECT count(*) FROM files").fetchone()[0]
        con.commit()
        assert tool({"action": "rename", "pc": "PC2", "to": "Office 1"}) == {"renamed": "PC2", "to": "Office 1"}
        hit = find("scholarship list", order="newest")[0]
        assert hit["pc"] == "Office 1" and os.sep + "Office 1" + os.sep in hit["copy"] and Path(hit["copy"]).is_file(), hit
        assert con.execute("SELECT count(*) FROM files WHERE pc='Office 1'").fetchone()[0] == n0 - 0 and not (HOME / "mirror/PC2").exists()
        assert [p["name"] for p in pcs()] == ["Office 1", "PC3"] and (HOME / "changes" / "Office 1").is_dir()
        r = run_pc(con, pcs()[0], T + 3 * 86400)
        assert r["new"] == r["changed"] == r["gone"] == r["copied"] == 0, r
        for bad in ({"pc": "Office 1", "to": "PC3"}, {"pc": "nobody", "to": "X"}, {"pc": "Office 1", "to": "../x"[:0]}):
            try:
                tool({"action": "rename", **bad})
                raise AssertionError(bad)
            except ValueError:
                pass
        con.close()
        print("ok")
    finally:
        HOME, CONFIG, SLOW_SECS = real
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    cmd, rest = (sys.argv[1:2] or [""])[0], sys.argv[2:]
    if cmd == "test":
        _test()
    elif cmd == "probe" and rest:
        print(json.dumps(probe(rest[0], {"user": rest[1], "password": rest[2]} if len(rest) > 2 else None),
                         ensure_ascii=False, indent=1))
    elif cmd == "scan":
        print(json.dumps([{k: v for k, v in r.items() if k != "lines"} for r in run_all()], ensure_ascii=False, indent=1))
    elif cmd == "find" and rest:
        print(json.dumps(find(" ".join(rest)), ensure_ascii=False, indent=1))
    else:
        print(__doc__)
