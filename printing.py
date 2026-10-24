"""Printers: list what Windows has installed (local + network), friendly names + default kept in settings,
and silent PDF printing through GDI (no external PDF app needed)."""
import json, re
from pathlib import Path

import pymupdf
import win32con, win32print, win32ui
from PIL import Image, ImageWin

ROOT = Path(__file__).parent
SETTINGS = ROOT / ".runtime" / "settings.json"
MAX_COPIES = 10


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(s: dict):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def list_printers() -> list[dict]:
    s = load_settings()
    aliases = s.get("aliases", {})
    default = s.get("default_printer") or _windows_default()
    out = []
    for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS, None, 2):
        name = p["pPrinterName"]
        if re.search(r"onenote|fax", name, re.I):  # not real printers
            continue
        offline = bool(p["Attributes"] & win32print.PRINTER_ATTRIBUTE_WORK_OFFLINE) or bool(
            p["Status"] & (win32print.PRINTER_STATUS_OFFLINE | win32print.PRINTER_STATUS_ERROR))
        status = "Offline" if offline else "Out of paper" if p["Status"] & win32print.PRINTER_STATUS_PAPER_OUT else "Ready"
        out.append({"name": name, "alias": aliases.get(name, ""), "status": status, "default": name == default,
                    "port": p["pPortName"]})
    return out


def _windows_default():
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        return None


def find_printer(want: str | None) -> str:
    """Match by friendly name or Windows name (case-insensitive, partial ok); empty = default."""
    printers = list_printers()
    if not want:
        d = next((p for p in printers if p["default"]), None)
        if not d:
            raise ValueError("koi default printer set nahi hai")
        return d["name"]
    w = want.lower().strip()
    for exact in (True, False):
        for p in printers:
            for n in (p["alias"], p["name"]):
                if n and ((n.lower() == w) if exact else (w in n.lower())):
                    return p["name"]
    raise ValueError(f"printer '{want}' nahi mila")


def print_pdf(pdf: Path, printer: str, copies: int = 1, output_file: str | None = None, dpi: int = 300):
    """Rasterise each page and draw it on the printer's page, scaled to fit the printable area.
    output_file is only for 'Microsoft Print to PDF' tests (avoids its save dialog)."""
    copies = max(1, min(int(copies), MAX_COPIES))
    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer)
    pw, ph = dc.GetDeviceCaps(win32con.HORZRES), dc.GetDeviceCaps(win32con.VERTRES)
    try:
        dc.StartDoc(pdf.name, output_file) if output_file else dc.StartDoc(pdf.name)
        with pymupdf.open(pdf) as doc:
            pages = [Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
                     for pm in (pg.get_pixmap(dpi=dpi) for pg in doc)]
        for _ in range(copies):
            for img in pages:
                dc.StartPage()
                scale = min(pw / img.width, ph / img.height)
                w, h = int(img.width * scale), int(img.height * scale)
                x, y = (pw - w) // 2, (ph - h) // 2
                ImageWin.Dib(img).draw(dc.GetHandleOutput(), (x, y, x + w, y + h))
                dc.EndPage()
        dc.EndDoc()
    finally:
        dc.DeleteDC()
    return copies


if __name__ == "__main__":
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "src.pdf"
    with pymupdf.open() as d:
        d.new_page().insert_text((72, 72), "Print test page", fontsize=20)
        d.save(src)
    print([(p["name"], p["status"], p["default"]) for p in list_printers()])
    name = find_printer("print to pdf")
    out = tmp / "printed.pdf"
    print_pdf(src, name, 1, output_file=str(out), dpi=100)
    assert out.exists() and out.stat().st_size > 1000, "nothing printed"
    try:
        find_printer("accounts")
        raise AssertionError("should not match")
    except ValueError:
        pass
    print("printing ok:", out)
