"""Renders the frames in docs/gifs.html into docs/<name>.gif with headless Edge.
Run: .venv\\Scripts\\python.exe docs\\render.py"""
import subprocess, tempfile
from pathlib import Path

from PIL import Image

DOCS = Path(__file__).parent
PAGE = (DOCS / "gifs.html").resolve().as_uri()
EDGE = next(p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if Path(p).exists())
TMP = Path(tempfile.mkdtemp())
GIFS = {  # name: (steps, how long each stays, ms)
    "call": ((0, 1, 2, 3, 5), (2200, 2200, 1600, 2600, 3400)),  # picked up, heard, decided, speaking, link on WhatsApp
    "errand": ((0, 1, 2, 3), (1800, 2600, 2800, 3200)),
}

for name, (steps, ms) in GIFS.items():
    frames = []
    for step in steps:
        out = TMP / f"{name}{step}.png"
        subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={TMP / 'edge'}",
                        "--force-device-scale-factor=2", "--window-size=452,614", f"--screenshot={out}",
                        f"{PAGE}?gif={name}&step={step}"], check=True, capture_output=True)
        frames.append(Image.open(out).convert("RGB").quantize(128, dither=Image.Dither.NONE))
    frames[0].save(DOCS / f"{name}.gif", save_all=True, append_images=frames[1:], loop=0, duration=list(ms))
    print(f"{name}.gif {(DOCS / f'{name}.gif').stat().st_size // 1024} KB")
