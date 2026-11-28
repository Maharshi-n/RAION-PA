"""Raion's video helper, for any footage: events, performances, speeches, sports, montages. Needs ffmpeg on PATH.
Paths are real file paths (relative to the current folder or absolute). Every command prints JSON.

  probe FILE...                                   duration, size (as displayed), fps, rotation, audio
  activity FILE [--step 0.5]                      motion + loudness over time: where action and sound start/end,
                                                  quiet gaps, scene changes, strong hits (for cutting on the music)
  sheet FILE... [--from A --to B --every S | --at T,T,...] [--out x.jpg]
                                                  frames with timestamps in one image (Read it). Several files and
                                                  no times: one frame of each file
  sync A B                                        how much later the same sound happens in B than in A
  beats SONG                                      the song's tempo and the time of every beat
  render EDL.json OUT.mp4 [--preview]             clips and photos, exact cuts, transitions, text and logo on top,
                                                  one song under it; H.264 Main/AAC (WhatsApp, Instagram)
  selftest

This file comes with Raion's code (git): the copy in memory/global/tools/ is written over at every start.
"""
import argparse, hashlib, io, json, shutil, subprocess, sys, tempfile, time
from pathlib import Path

import numpy as np

FORMATS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
WA_VIDEO_MB = 16  # WhatsApp Cloud API: videos up to 16 MB, H.264 + AAC


def ff(*args) -> bytes:
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *map(str, args)], capture_output=True,
                       creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))  # chats stay quick meanwhile
    if r.returncode:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-1500:])
    return r.stdout


def probe(f) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(f)],
                       capture_output=True)
    if r.returncode:
        raise RuntimeError(f"can't read {f}: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    out = {"file": str(f), "duration": round(float(d["format"].get("duration") or 0), 3), "audio": bool(a),
           "size_mb": round(int(d["format"].get("size", 0)) / 2**20, 1)}
    if v:
        rot = v.get("tags", {}).get("rotate") or next((x["rotation"] for x in v.get("side_data_list", []) if "rotation" in x), 0)
        w, h = v["width"], v["height"]
        if int(float(rot)) % 180:
            w, h = h, w
        num, den = (int(x) for x in (v.get("avg_frame_rate") or "0/1").split("/"))
        out.update(width=w, height=h, rotation=int(float(rot)), fps=round(num / den, 2) if den else None,
                   codec=v["codec_name"], profile=v.get("profile"), b_frames=v.get("has_b_frames"))
    return out


def pcm(f, sr=8000, limit=None) -> np.ndarray:
    raw = ff(*(["-t", limit] if limit else []), "-i", f, "-vn", "-ac", 1, "-ar", sr, "-f", "s16le", "-")
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def onsets(x, sr=8000, hop=80) -> np.ndarray:
    """Rise in loudness every 10 ms (where sounds start: beats, claps, words)."""
    n = len(x) // hop
    e = np.log1p(100 * np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1)))
    return np.maximum(np.diff(e, prepend=e[:1]), 0)


def band_onsets(x, sr=8000, hop=80, win=256, nb=8) -> np.ndarray:
    """onsets() per frequency band (frames x bands): beats look alike, melody and words don't."""
    n = max(0, (len(x) - win) // hop + 1)
    edges = np.unique(np.geomspace(2, win // 2, nb + 1).astype(int))
    w, parts = np.hanning(win).astype(np.float32), []
    for s in range(0, n, 4000):
        idx = (np.arange(s, min(n, s + 4000)) * hop)[:, None] + np.arange(win)[None, :]
        spec = np.abs(np.fft.rfft(x[idx] * w, axis=1)) ** 2
        parts.append(np.stack([spec[:, a:b].sum(1) for a, b in zip(edges, edges[1:])], 1))
    e = np.log1p(1000 * np.concatenate(parts)) if parts else np.zeros((2, len(edges) - 1))
    return np.maximum(np.diff(e, axis=0, prepend=e[:1]), 0)


def spans(on, step, min_len, gap) -> list:
    out = []
    start = None
    for i, x in enumerate(np.r_[on, False]):
        if x and start is None:
            start = i
        elif not x and start is not None:
            if out and start * step - out[-1][1] <= gap:
                out[-1][1] = i * step
            else:
                out.append([start * step, i * step])
            start = None
    return [[round(s, 2), round(e, 2)] for s, e in out if e - s >= min_len]


def activity(f, step=0.5) -> dict:
    p = probe(f)
    res = {"file": str(f), "duration": p["duration"], "step": step}
    rows = int(np.ceil(p["duration"] / step))
    motion = db = None
    if "width" in p:
        fps = 4
        fr = np.frombuffer(ff("-i", f, "-vf", f"fps={fps},scale=64:36,format=gray", "-f", "rawvideo", "-"), np.uint8)
        fr = fr.reshape(-1, 36, 64).astype(np.float32)
        diff = np.r_[0.0, np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))]
        hist = np.stack([np.histogram(x, 16, (0, 256))[0] for x in fr]) / fr[0].size
        res["scene_changes"] = [round(i / fps, 2) for i in range(1, len(fr)) if np.abs(hist[i] - hist[i - 1]).sum() > 0.6]
        per = max(1, round(step * fps))
        motion = np.array([diff[i * per:(i + 1) * per].mean() if i * per < len(diff) else 0 for i in range(rows)])
        thr = max(0.3 * np.percentile(motion, 95), 1.0)  # floor: camera/compression noise on a still shot
        res["action"] = spans(motion > thr, step, 1.5, 0.75)
    if p["audio"]:
        sr = 8000
        x = pcm(f, sr)
        n = int(step * sr)
        db = np.array([20 * np.log10(np.sqrt((x[i * n:(i + 1) * n] ** 2).mean()) + 1e-5) if i * n < len(x) else -100
                       for i in range(rows)])
        loud = db > max(np.percentile(db, 10) + 12, -50)
        res["sound"] = spans(loud, step, 0.5, 0.5)
        res["quiet_gaps"] = [[a[1], b[0]] for a, b in zip(res["sound"], res["sound"][1:]) if b[0] - a[1] >= 1]
        o = onsets(x, sr)
        peaks = [i for i in range(1, len(o) - 1) if o[i] >= o[i - 1] and o[i] > o[i + 1] and o[i] > o.mean() + 2.5 * o.std()]
        hits = []
        for i in sorted(peaks, key=lambda i: -o[i]):  # strongest first, at least 0.25 s apart
            if all(abs(i / 100 - h) >= 0.25 for h in hits):
                hits.append(i / 100)
        res["hits"] = sorted(round(h, 2) for h in hits)[:150]
    # a short readable timeline (at most ~120 rows)
    g = max(1, int(np.ceil(rows / 120)))
    top = np.percentile(motion, 95) + 1e-6 if motion is not None else 1
    res["timeline"] = [f"{i * step:7.1f}s  " + (f"motion {'#' * int(min(10, 10 * motion[i:i + g].mean() / top)):<10}  "
                                                if motion is not None else "")
                       + (f"{db[i:i + g].max():5.0f} dB" if db is not None else "") for i in range(0, rows, g)]
    return res


def sheet(files, at=None, start=None, end=None, every=0.5, out=None) -> dict:
    from PIL import Image, ImageDraw, ImageFont
    if at:
        shots = [(files[0], t, f"{t:.2f}s") for t in at]
    elif start is not None:
        end = probe(files[0])["duration"] if end is None else end
        shots = [(files[0], t, f"{t:.2f}s") for t in np.arange(start, end + 1e-6, every)]
    else:
        shots = []
        for f in files:
            d = probe(f)["duration"]
            shots.append((f, d * 0.4, f"{Path(f).name[:30]} ({d:.0f}s)"))
    shots = shots[:48]
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    thumbs = []
    for f, t, label in shots:
        png = ff("-ss", f"{max(t, 0):.3f}", "-i", f, "-frames:v", 1, "-f", "image2pipe", "-c:v", "png", "-")
        if not png:  # past the end
            continue
        im = Image.open(io.BytesIO(png)).convert("RGB")
        im.thumbnail((360, 360))
        d = ImageDraw.Draw(im)
        box = d.textbbox((4, 4), label, font=font)
        d.rectangle((box[0] - 3, box[1] - 3, box[2] + 3, box[3] + 3), fill="black")
        d.text((4, 4), label, fill="yellow", font=font)
        thumbs.append(im)
    if not thumbs:
        raise ValueError("no frames at those times")
    cols = 8 if len(thumbs) > 24 else 6 if len(thumbs) > 12 else 4 if len(thumbs) > 4 else len(thumbs)
    w, h = max(t.width for t in thumbs), max(t.height for t in thumbs)
    grid = Image.new("RGB", (cols * w + (cols - 1) * 4, -(-len(thumbs) // cols) * (h + 4) - 4), "white")
    for i, t in enumerate(thumbs):
        grid.paste(t, ((i % cols) * (w + 4), (i // cols) * (h + 4)))
    if not out:
        tag = f"{start:g}-{end:g}s" if start is not None else "at" if at else "clips"
        out = Path("work") / "sheets" / f"{Path(files[0]).stem}_{tag}.jpg"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    grid.save(out, quality=85)
    return {"sheet": str(out), "frames": len(thumbs)}


def sync(a, b) -> dict:
    ea, eb = (band_onsets(pcm(f, limit=600)) for f in (a, b))
    ea, eb = ea - ea.mean(0), eb - eb.mean(0)
    n = 1 << int(np.ceil(np.log2(len(ea) + len(eb))))
    c = np.fft.irfft(np.conj(np.fft.rfft(ea, n, axis=0)) * np.fft.rfft(eb, n, axis=0), n, axis=0).sum(1)  # c[k] = sum ea[t]*eb[t+k]
    lags = np.where(np.arange(n) > n // 2, np.arange(n) - n, np.arange(n)) / 100
    k = int(np.argmax(c))
    z = (c - c.mean()) / (c.std() + 1e-9)
    other = np.where(np.abs(lags - lags[k]) > 0.15, z, -np.inf)  # next best, away from the winner
    k2 = int(np.argmax(other))
    close = other[k2] > 0.85 * z[k]
    res = {"b_minus_a": round(float(lags[k]), 2), "meaning": f"a sound at A time T is at B time T{lags[k]:+.2f}",
           "confidence": "good" if z[k] > 8 and not close else "weak: confirm on a visible/audible moment",
           "score": round(float(z[k]), 1)}
    if close:  # repeating music can match a beat off
        res["also_possible"] = round(float(lags[k2]), 2)
    return res


def beats(f) -> dict:
    """The song's beat: tempo from how the loudness rises repeat, then each beat followed to the nearest real hit."""
    o = onsets(pcm(f, limit=600))
    o = np.convolve(o, [0.25, 0.5, 0.25], "same")
    if len(o) < 400:
        raise ValueError("too short to find a beat")
    c = o - o.mean()
    n = 1 << int(np.ceil(np.log2(2 * len(c))))
    ac = np.fft.irfft(np.abs(np.fft.rfft(c, n)) ** 2, n)
    lag = max(range(30, 101), key=lambda l: ac[l] + 0.5 * ac[2 * l])  # 60-200 beats a minute
    t = max(range(lag), key=lambda p: o[p::lag].sum())
    strong, out = o.mean() + o.std(), []
    while t < len(o):
        lo = max(t - 3, 0)
        w = o[lo:t + 4]
        if w.max() > strong:  # a real hit near where the beat should be: follow it (keeps a long song from drifting)
            t = lo + int(np.argmax(w))
        out.append(t / 100)
        t += lag
    gap = float(np.median(np.diff(out))) if len(out) > 1 else lag / 100
    return {"file": str(f), "bpm": round(60 / gap), "beat": round(gap, 3), "beats": [round(b, 2) for b in out]}


IMAGES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
LOOKS = {"none": "", "clean": "eq=contrast=1.06:saturation=1.12:brightness=0.01,",
         "warm": "eq=contrast=1.06:saturation=1.15,colorbalance=rm=0.04:bm=-0.04:rh=0.03:bh=-0.03,",
         "vivid": "eq=contrast=1.1:saturation=1.3,",
         "film": "eq=contrast=1.08:saturation=0.9,colorbalance=rs=0.03:bs=0.04,vignette=PI/5,",
         "bw": "hue=s=0,eq=contrast=1.1,"}
MOVES = ("in", "out", "left", "right", "up", "down")
ZOOM = 0.12   # how far a photo moves over its time on screen
GROUP = 10    # clips joined in one ffmpeg run: more than this goes in parts (an 8 GB office PC)
MID = ["-c:v", "libx264", "-preset", "veryfast", "-crf", 16, "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-ar", 48000,
       "-ac", 2]  # parts on the way to the final file: near-lossless picture, uncompressed sound (.mkv)


def render_lock():
    """One render at a time on this computer; freed by itself when the process ends."""
    f = open(Path(tempfile.gettempdir()) / "raion_video.lock", "w")
    if sys.platform == "win32":
        import msvcrt
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return f
            except OSError:
                time.sleep(2)
    import fcntl
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def lost(w, h, W, H) -> float:
    """Share of the picture a fill-the-frame crop would cut away."""
    r = (w / h) / (W / H)
    return 1 - min(r, 1 / r)


def canvas(src, W, H, fit, focus, out):
    """A photo as one frame-shaped picture: turned upright, cropped or set whole on its own blurred copy."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    im = ImageOps.exif_transpose(Image.open(src))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        im = Image.alpha_composite(Image.new("RGBA", im.size, "black"), im)
    im = im.convert("RGB")
    if fit == "auto":
        fit = "blur" if lost(im.width, im.height, W, H) > 0.4 else "crop"
    if fit == "crop":
        res = ImageOps.fit(im, (W, H), Image.LANCZOS, centering=(focus, 0.5))
    else:
        res = Image.new("RGB", (W, H), "black")
        if fit == "blur":
            small = ImageOps.fit(im, (max(W // 12, 2), max(H // 12, 2))).filter(ImageFilter.GaussianBlur(4))
            res = ImageEnhance.Brightness(small.resize((W, H), Image.BICUBIC)).enhance(0.55)
        fg = ImageOps.contain(im, (W, H), Image.LANCZOS)
        res.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    res.save(out, quality=95)


def segment(c, info, W, H, fps, d, out, i=0):
    """One clip or photo as its own file, already in the output's size, shape, speed and look."""
    N = round(d * fps)
    look = LOOKS[c.get("look", "none")]
    fit, x = c.get("fit", "auto"), float(c.get("focus", 0.5))
    quiet = ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    if c["src"].lower().endswith(IMAGES):
        move = c.get("move", "auto")
        move = MOVES[i % 4] if move == "auto" else move
        jpg = Path(out).with_suffix(".jpg")
        if move == "none":
            canvas(c["src"], W, H, fit, x, jpg)
            ff("-loop", 1, "-framerate", fps, "-t", f"{d:.3f}", "-i", jpg, *quiet, "-filter_complex",
               f"[0:v]{look}setsar=1,format=yuv420p[v]", "-map", "[v]", "-map", "1:a", "-frames:v", N, *MID, out)
        else:
            canvas(c["src"], W * 3, H * 3, fit, x, jpg)  # 3x the frame: zoompan moves in whole pixels, this keeps it smooth
            p, z = f"(on/{N})", 1 + ZOOM
            zoom = {"in": f"1+{ZOOM}*{p}", "out": f"{z}-{ZOOM}*{p}"}.get(move, str(z))
            cx, cy = "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"
            px, py = {"left": (f"(iw-iw/zoom)*(1-{p})", cy), "right": (f"(iw-iw/zoom)*{p}", cy),
                      "up": (cx, f"(ih-ih/zoom)*(1-{p})"), "down": (cx, f"(ih-ih/zoom)*{p}")}.get(move, (cx, cy))
            ff("-i", jpg, *quiet, "-filter_complex",
               f"[0:v]zoompan=z='{zoom}':x='{px}':y='{py}':d={N}:s={W}x{H}:fps={fps},{look}setsar=1,format=yuv420p[v]",
               "-map", "[v]", "-map", "1:a", "-frames:v", N, *MID, out)
        jpg.unlink(missing_ok=True)
        return
    sp = float(c.get("speed", 1))
    if fit == "auto":
        fit = "blur" if lost(info["width"], info["height"], W, H) > 0.4 else "crop"
    w4, h4 = W // 8 * 2, H // 8 * 2
    frame = {"crop": f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}:(iw-{W})*{x}:(ih-{H})/2",
             "pad": f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2",
             "blur": f"split[b][f];[b]scale={w4}:{h4}:force_original_aspect_ratio=increase,crop={w4}:{h4},boxblur=6:2,"
                     f"scale={W}:{H},eq=brightness=-0.12[bg];[f]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
                     f"[bg][fg]overlay=(W-w)/2:(H-h)/2"}[fit]
    fl = [f"[0:v]setpts=(PTS-STARTPTS)/{sp},fps={fps},{frame},{look}setsar=1,format=yuv420p,trim=duration={d:.3f},"
          f"setpts=PTS-STARTPTS[v]"]
    tempo = f"atempo={sp ** 0.5:.4f},atempo={sp ** 0.5:.4f}," if sp != 1 else ""
    src = f"[0:a]aresample=48000,{tempo}" if info["audio"] else "anullsrc=r=48000:cl=stereo,"
    fl.append(f"{src}aformat=sample_fmts=fltp:channel_layouts=stereo,volume={float(c.get('volume', 1))},apad,"
              f"atrim=0:{d:.3f},afade=t=in:d=0.03,afade=t=out:st={max(d - 0.03, 0):.3f}:d=0.03,asetpts=PTS-STARTPTS[a]")
    t0 = float(c["in"])  # re-encoded, so the cut is exact
    ff("-ss", f"{t0:.3f}", "-t", f"{d * sp + 0.2:.3f}", "-i", c["src"], "-filter_complex", ";".join(fl),
       "-map", "[v]", "-map", "[a]", "-frames:v", N, *MID, out)


def timeline(durs, trans):
    """Total length and where each change of clip happens (the middle of its transition)."""
    T, cuts = durs[0], []
    for d, (_, D) in zip(durs[1:], trans[1:]):
        cuts.append(round(T - D / 2, 2))
        T += d - D
    return T, cuts


def join_graph(durs, trans):
    """ffmpeg filters that put inputs 0..n-1 one after another with their transitions: ends in [v] and [a]."""
    n = len(durs)
    fl = [f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[v{i}];[{i}:a]asetpts=PTS-STARTPTS[a{i}]" for i in range(n)]
    v, a, T = "v0", "a0", durs[0]
    for i in range(1, n):
        name, D = trans[i]
        if D:
            fl.append(f"[{v}][v{i}]xfade=transition={name}:duration={D:.3f}:offset={T - D:.3f}[x{i}];"
                      f"[{a}][a{i}]acrossfade=d={D:.3f}:c1=tri:c2=tri[y{i}]")
        else:
            fl.append(f"[{v}][v{i}]concat=n=2:v=1:a=0,settb=AVTB[x{i}];[{a}][a{i}]concat=n=2:v=0:a=1[y{i}]")
        v, a, T = f"x{i}", f"y{i}", T + durs[i] - D
    fl.append(f"[{v}]null[v];[{a}]anull[a]")
    return fl


def render(edl_path, out, preview=False) -> dict:
    e = json.loads(Path(edl_path).read_text(encoding="utf-8"))
    clips, fps = e["clips"], int(e.get("fps", 30))
    still = [c["src"].lower().endswith(IMAGES) for c in clips]
    infos = [None if s else probe(c["src"]) for c, s in zip(clips, still)]
    fmt = e.get("format", "9:16")
    first = next((p for p in infos if p), None)
    W, H = (FORMATS.get(fmt) or (tuple(map(int, fmt.split("x"))) if "x" in fmt else None)
            or ((first["width"], first["height"]) if first else FORMATS["9:16"]))
    if preview:
        W, H = W * 2 // 3, H * 2 // 3
    W, H = W // 2 * 2, H // 2 * 2
    durs = []
    for i, (c, info) in enumerate(zip(clips, infos)):
        c.setdefault("look", e.get("look", "none"))
        c.setdefault("fit", e.get("fit", "auto"))
        if c["look"] not in LOOKS:
            raise ValueError(f"clip {i + 1}: look must be one of {', '.join(LOOKS)}")
        if info is None:
            durs.append(float(c.get("dur", e.get("photo_dur", 3))))
            continue
        t0, t1, sp = float(c["in"]), float(c["out"]), float(c.get("speed", 1))
        if not 0 <= t0 < t1 <= info["duration"] + 0.05:
            raise ValueError(f"clip {i + 1} ({c['src']}): in/out must be within 0-{info['duration']}s")
        if not 0.25 <= sp <= 4:
            raise ValueError(f"clip {i + 1}: speed must be between 0.25 and 4")
        durs.append((t1 - t0) / sp)
    n = len(clips)
    trans = [("", 0.0)]  # trans[i]: how clip i comes in over clip i-1
    for i in range(1, n):
        name = clips[i].get("transition", e.get("transition", "cut"))
        D = 0.0 if name == "cut" else float(clips[i].get("transition_dur", e.get("transition_dur", 0.5)))
        trans.append((name, round(min(D, 0.4 * durs[i - 1], 0.4 * durs[i]) * fps) / fps))
    music = e.get("audio", "clips")
    snapped = 0
    if isinstance(music, dict) and music.get("snap"):  # each change of clip moves to the song's nearest beat
        b = beats(music["src"])
        grid, start, pos = np.array(b["beats"]), float(music.get("start", 0)), 0.0
        for i in range(n - 1):
            D = trans[i + 1][1]
            cut = pos + durs[i] - D / 2 + start
            room = (infos[i]["duration"] - float(clips[i]["out"])) / float(clips[i].get("speed", 1)) if infos[i] else 9
            for t in sorted(grid, key=lambda t: abs(t - cut))[:3]:
                if abs(t - cut) <= b["beat"] and t - cut <= room and durs[i] + t - cut >= max(0.8, 2.5 * D):
                    durs[i] += float(t - cut)
                    snapped += 1
                    break
            pos += durs[i] - D
    durs = [max(round(d * fps), 2) / fps for d in durs]
    lock = render_lock()
    cache = Path(edl_path).resolve().parent / f".cache_{Path(edl_path).stem}"
    cache.mkdir(parents=True, exist_ok=True)
    segs = []
    for i, (c, info, d) in enumerate(zip(clips, infos, durs)):
        st = Path(c["src"]).stat()
        want = {k: v for k, v in c.items() if not k.startswith("transition")}  # a new transition: nothing rendered again
        move = {"move": MOVES[i % 4]} if still[i] and c.get("move", "auto") == "auto" else {}
        key = json.dumps([want, move, W, H, fps, round(d, 3), st.st_size, st.st_mtime_ns], sort_keys=True)
        seg = cache / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.mkv"
        if not seg.exists():  # an edit list changed in one place renders only that place again
            tmp = seg.with_name("tmp_" + seg.name)
            segment(c, info, W, H, fps, d, tmp, i)
            tmp.replace(seg)
        segs.append(seg)
    for f in cache.iterdir():
        if f not in segs:
            f.unlink()
    T, cuts = timeline(durs, trans)
    files, fd, ft = list(segs), list(durs), list(trans)
    while len(files) > GROUP + 2:  # long edits: joined in parts first, so no run holds many clips open
        nf, nd, nt = [], [], []
        for g in range(0, len(files), GROUP):
            part, pd, pt = files[g:g + GROUP], fd[g:g + GROUP], [("", 0.0)] + ft[g + 1:g + GROUP]
            if len(part) > 1:
                dst = cache / f"part_{len(files)}_{g}.mkv"
                ff(*[x for p in part for x in ("-i", p)], "-filter_complex", ";".join(join_graph(pd, pt)),
                   "-map", "[v]", "-map", "[a]", *MID, dst)
                part, pd = [dst], [timeline(pd, pt)[0]]
            nf += part
            nd += pd
            nt.append(ft[g])
        files, fd, ft = nf, nd, nt
    args = [x for p in files for x in ("-i", p)]
    fl, v, a, k = join_graph(fd, ft), "[v]", "[a]", len(files)
    for j, o in enumerate(e.get("overlays", [])):  # full-frame pictures with see-through parts: text, names, a logo
        t0, t1 = float(o.get("from", 0)), min(float(o.get("to", T)), T)
        f = min(float(o.get("fade", 0.3)), (t1 - t0) / 2)
        args += ["-loop", 1, "-framerate", fps, "-t", f"{t1 - t0:.3f}", "-i", o["src"]]
        fade = f",fade=t=in:st=0:d={f:.3f}:alpha=1,fade=t=out:st={t1 - t0 - f:.3f}:d={f:.3f}:alpha=1" if f else ""
        s = f"(1-min((t-{t0:.3f})/{max(f, 0.01):.3f},1))^2*{H // 20}"  # slides the last bit into place as it fades in
        x, y = {"up": ("0", s), "down": ("0", f"-{s}"), "left": (s, "0"), "right": (f"-{s}", "0")}.get(o.get("move"), ("0", "0"))
        fl.append(f"[{k}:v]scale={W}:{H},format=rgba{fade},setpts=PTS+{t0:.3f}/TB[o{j}];"
                  f"{v}[o{j}]overlay=x='{x}':y='{y}':eof_action=pass:format=auto[w{j}]")
        v, k = f"[w{j}]", k + 1
    if isinstance(music, dict):  # one song under the whole edit
        start = float(music.get("start", 0))
        args += (["-ss", f"{start:.3f}"] if start > 0 else []) + ["-i", music["src"]]
        delay = f"adelay={int(-start * 1000)}:all=1," if start < 0 else ""
        fi = float(music.get("fade_in", 0))
        fl.append(f"[{k}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,{delay}"
                  f"volume={float(music.get('volume', 1))},{f'afade=t=in:d={fi},' if fi else ''}apad,atrim=0:{T:.3f},"
                  f"asetpts=PTS-STARTPTS[m]")
        keep = float(music.get("clips_volume", 0))
        fl.append(f"{a}volume={keep}[ak];[ak][m]amix=inputs=2:normalize=0:duration=first[am]" if keep
                  else f"{a}anullsink")
        a = "[am]" if keep else "[m]"
    if e.get("loudness", True):  # every video goes out at the same level (what Instagram and YouTube play at)
        fl.append(f"{a}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[an]")
        a = "[an]"
    fi, fo = float(e.get("fade_in", 0)), float(e.get("fade_out", 0))
    if fi:
        fl.append(f"{v}fade=t=in:st=0:d={fi}[vi]")
        v = "[vi]"
    if fo:
        fl += [f"{v}fade=t=out:st={T - fo:.3f}:d={fo}[vf]", f"{a}afade=t=out:st={T - fo:.3f}:d={fo}[af]"]
        v, a = "[vf]", "[af]"
    enc = ["-c:v", "libx264", "-profile:v", "main", "-bf", 0, "-pix_fmt", "yuv420p", "-r", fps, "-preset", "medium"]
    if preview:  # fits WhatsApp's 16 MB video limit
        kbps = max(300, min(2500, int(WA_VIDEO_MB * 0.9 * 8192 / T) - 128))
        enc += ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k"]
    else:
        enc += ["-crf", 19, "-maxrate", "12M", "-bufsize", "24M"]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    ff(*args, "-filter_complex", ";".join(fl), "-map", v, "-map", a, "-t", f"{T:.3f}", *enc,
       "-c:a", "aac", "-b:a", "160k", "-ar", 48000, "-movflags", "+faststart", out)
    for f in cache.glob("part_*.mkv"):
        f.unlink()
    lock.close()
    p = probe(out)
    check = [0.05]
    for c, (_, D) in zip(cuts, trans[1:]):
        check += [c - D / 2 - 0.1, c, c + D / 2 + 0.05] if D else [c - 0.1, c + 0.05]
    check.append(max(p["duration"] - 0.1, 0))
    res = {"out": str(out), "duration": p["duration"], "width": p["width"], "height": p["height"],
           "size_mb": p["size_mb"], "fits_whatsapp_video": p["size_mb"] <= WA_VIDEO_MB, "cuts_at": cuts,
           "check_at": ",".join(f"{t:.2f}" for t in check)}
    if isinstance(music, dict) and music.get("snap"):
        res["cuts_on_the_beat"] = f"{snapped} of {n - 1}"
    from PIL import Image
    small = [c["src"] for c, s in zip(clips, still) if s and max(Image.open(c["src"]).size) < 700]
    if small:
        res["small_photos"] = small  # will look soft blown up to full screen: leave out or ask for the original
    return res


def selftest():
    """Synthetic clips: 4 s still lead-in then action, keyframes 3 s apart (a keyframe-snapping cut would show the
    lead-in), sound from 3 s (two beat patterns, like a beat under a melody); B has other pictures and the same
    sound 1.7 s later. Then photos, transitions, text on top, a song with a beat, and a long edit joined in parts."""
    with tempfile.TemporaryDirectory() as tmp:
        A, B = Path(tmp, "a.mp4"), Path(tmp, "b.mp4")
        beep = ("aevalsrc='if(gte(t,3),0.4*sin(2*PI*440*t)*lt(mod(t,0.5),0.1)+0.4*sin(2*PI*1500*t)*lt(mod(t,0.7),0.1),0)'"
                ":s=48000:d=12")
        ff("-f", "lavfi", "-i", "color=c=gray:s=640x360:r=30:d=4", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=8",
           "-f", "lavfi", "-i", beep, "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
           "-c:v", "libx264", "-g", 90, "-c:a", "aac", A)
        ff("-f", "lavfi", "-i", "testsrc=s=640x360:r=30:d=12", "-i", A, "-filter_complex", "[1:a]adelay=1700:all=1[a]",
           "-map", "0:v", "-map", "[a]", "-t", 12, "-c:v", "libx264", "-c:a", "aac", B)
        act = activity(A)
        assert abs(act["action"][0][0] - 4.0) <= 0.5 and abs(act["sound"][0][0] - 3.0) <= 0.5, act
        s = sync(A, B)
        assert abs(s["b_minus_a"] - 1.7) <= 0.05, s
        edl = Path(tmp, "e.json")
        edl.write_text(json.dumps({"format": "9:16", "fit": "crop", "clips": [{"src": str(A), "in": 4.0, "out": 9.0},
                                                                             {"src": str(B), "in": 2.0, "out": 5.0}]}))
        r = render(edl, Path(tmp, "out.mp4"))
        p = probe(r["out"])
        assert abs(r["duration"] - 8.0) < 0.1 and (p["width"], p["height"]) == (1080, 1920) and r["cuts_at"] == [5.0], r
        assert p["profile"] == "Main" and not p["b_frames"], p

        def gray(f, t, size="64:64"):
            return np.frombuffer(ff("-ss", t, "-i", f, "-frames:v", 1, "-vf", f"scale={size},format=gray", "-f", "rawvideo",
                                    "-"), np.uint8).astype(float)
        assert gray(r["out"], 0).std() > 20, "first frame is the still lead-in: cut not frame-accurate"
        edl.write_text(json.dumps({"clips": [{"src": str(A), "in": 4.0, "out": 9.0}],
                                   "audio": {"src": str(B), "start": 4.7}, "fade_out": 0.5}))
        r = render(edl, Path(tmp, "prev.mp4"), preview=True)
        assert r["fits_whatsapp_video"] and (r["width"], r["height"]) == (720, 1280), r
        assert sheet([str(A)], start=3, end=5, every=0.5, out=Path(tmp, "s.jpg"))["frames"] == 5

        from PIL import Image
        song, P, Q, text = Path(tmp, "song.wav"), Path(tmp, "p.jpg"), Path(tmp, "q.png"), Path(tmp, "t.png")
        ff("-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*220*t)*lt(mod(t,0.5),0.05)':s=48000:d=20", song)
        bt = beats(song)
        assert bt["bpm"] == 120 and all(abs(x % 0.5 - 0.25) > 0.2 for x in bt["beats"]), bt
        Image.new("RGB", (800, 450), "white").save(P)  # wide photo: shown whole on its blurred copy, not cut
        Image.new("RGB", (450, 800), (20, 20, 160)).save(Q)
        t = Image.new("RGBA", (360, 640), (0, 0, 0, 0))
        t.paste((255, 0, 0, 255), (0, 560, 360, 640))  # a band at the bottom, the rest see-through
        t.save(text)
        edl.write_text(json.dumps({
            "format": "360x640", "transition": "fade", "look": "clean",
            "clips": [{"src": str(P), "dur": 2.2}, {"src": str(Q), "dur": 1.9, "transition": "slideleft"},
                      {"src": str(A), "in": 4.0, "out": 6.3, "speed": 2, "transition": "cut"}, {"src": str(P), "move": "none"}],
            "overlays": [{"src": str(text), "from": 1.0, "to": 3.0, "move": "up"}],
            "audio": {"src": str(song), "snap": True, "fade_in": 0.3}, "fade_in": 0.3, "fade_out": 0.5}))
        r = render(edl, Path(tmp, "mix.mp4"))
        assert r["cuts_on_the_beat"] == "3 of 3" and all(abs(c % 0.5 - 0.25) > 0.2 for c in r["cuts_at"]), r
        assert (r["width"], r["height"]) == (360, 640) and probe(r["out"])["audio"], r
        f = gray(r["out"], 1.2, "36:64").reshape(64, 36)
        assert f[32].mean() > 200 and f[4].mean() < 150, "wide photo: whole in the middle, its blurred copy above"
        assert abs(gray(r["out"], 2.0, "36:64").reshape(64, 36)[60].mean() - 76) < 25, "text band not on the picture"
        assert gray(r["out"], 0.6, "36:64").reshape(64, 36)[60].mean() > 100, "text band shown before its time"
        made = {x.name for x in Path(tmp, ".cache_e").iterdir()}
        e2 = json.loads(edl.read_text())
        e2["clips"][1]["transition"] = "circleopen"
        edl.write_text(json.dumps(e2))
        render(edl, Path(tmp, "mix2.mp4"))
        assert {x.name for x in Path(tmp, ".cache_e").iterdir()} == made, "a changed transition rendered clips again"
        many = [{"src": str(P if i % 2 else Q), "dur": 0.6, "move": "none"} for i in range(GROUP + 5)]
        edl.write_text(json.dumps({"format": "180x320", "fps": 10, "transition": "fade", "transition_dur": 0.2,
                                   "clips": many}))
        r = render(edl, Path(tmp, "long.mp4"))
        assert abs(r["duration"] - (len(many) * 0.6 - (len(many) - 1) * 0.2)) < 0.15 and len(r["cuts_at"]) == len(many) - 1, r
        print("ok")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe").add_argument("files", nargs="+")
    x = sub.add_parser("activity")
    x.add_argument("file")
    x.add_argument("--step", type=float, default=0.5)
    x = sub.add_parser("sheet")
    x.add_argument("files", nargs="+")
    x.add_argument("--from", dest="start", type=float)
    x.add_argument("--to", dest="end", type=float)
    x.add_argument("--every", type=float, default=0.5)
    x.add_argument("--at", type=lambda s: [float(t) for t in s.split(",") if t.strip()])
    x.add_argument("--out")
    x = sub.add_parser("sync")
    x.add_argument("a")
    x.add_argument("b")
    sub.add_parser("beats").add_argument("file")
    x = sub.add_parser("render")
    x.add_argument("edl")
    x.add_argument("out")
    x.add_argument("--preview", action="store_true")
    sub.add_parser("selftest")
    o = ap.parse_args()
    if o.cmd == "selftest":
        selftest()
        sys.exit()
    res = {"probe": lambda: [probe(f) for f in o.files], "activity": lambda: activity(o.file, o.step),
           "sheet": lambda: sheet(o.files, o.at, o.start, o.end, o.every, o.out), "sync": lambda: sync(o.a, o.b),
           "beats": lambda: beats(o.file), "render": lambda: render(o.edl, o.out, o.preview)}[o.cmd]()
    print(json.dumps(res, ensure_ascii=False, indent=1))
