"""File engine: HTML -> PDF/PNG (Edge headless), PDF previews, PPTX/DOCX/XLSX from JSON specs,
Office -> PDF (installed Office via COM, else LibreOffice), and text extraction so Claude can read Office files."""
import asyncio, json, re, shutil, subprocess, tempfile, threading
from pathlib import Path

import pymupdf

ROOT = Path(__file__).parent
EDGE = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if Path(p).exists()), "msedge")
EDGE_PROFILE = ROOT / ".runtime" / "edge"
# soffice.com waits for the conversion (soffice.exe can return before it's done)
SOFFICE = next((p for p in (r"C:\Program Files\LibreOffice\program\soffice.com",
                            r"C:\Program Files (x86)\LibreOffice\program\soffice.com") if Path(p).exists()), None)
LO_PROFILE = ROOT / ".runtime" / "libreoffice"
LO_LOCK = threading.Lock()  # one LibreOffice profile: one conversion at a time


NO_SCRIPTS = '<meta http-equiv="Content-Security-Policy" content="script-src \'none\'">'


# .lh = pre-printed text, .lh-u = pre-printed underline (e.g. "Date ____")
PAPER_CSS = "<style>.letterhead,.letterhead *,.lh{visibility:hidden!important}.lh-u{border-color:transparent!important}</style>"


def _include(m, global_dir: Path) -> str:
    """{{INCLUDE:MNA/templates/_letterhead.html}} -> that shared global file (so a design fix reaches every document)."""
    f = (global_dir / m.group(1)).resolve()
    return f.read_text(encoding="utf-8") if f.is_relative_to(global_dir.resolve()) and f.is_file() else ""


def _fill_placeholders(html: str, global_dir: Path, me_dir: Path, paper=False) -> str:
    html = re.sub(r"\{\{INCLUDE:([^}]+)\}\}", lambda m: _include(m, global_dir), html)
    html = html.replace("{{GLOBAL}}", global_dir.resolve().as_uri()).replace("{{ME}}", me_dir.resolve().as_uri())
    if paper:  # printing on pre-printed letterhead paper: keep the header's space, print nothing there
        html = html.replace("</head>", PAPER_CSS + "</head>", 1) if "</head>" in html else PAPER_CSS + html
    # scripts off: the HTML is written by the model from user-supplied content
    # (Edge's --blink-settings=scriptEnabled=false breaks --print-to-pdf, so use CSP instead)
    return NO_SCRIPTS + html if "<head" not in html else re.sub(r"(<head[^>]*>)", r"\1" + NO_SCRIPTS, html, count=1)


def _edge(args: list, timeout=60):
    EDGE_PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={EDGE_PROFILE}",
                    *args], capture_output=True, timeout=timeout)


def _staged(html_path: Path, global_dir: Path, me_dir: Path, paper=False) -> Path:
    """Copy next to the original (so relative links still work) with placeholders filled."""
    staged = html_path.with_name(f".render_{html_path.name}")
    staged.write_text(_fill_placeholders(html_path.read_text(encoding="utf-8"), global_dir, me_dir, paper),
                      encoding="utf-8")
    return staged


def has_letterhead(html_path: Path) -> bool:
    return bool(re.search(r"_letterhead\.html|class=[\"'][^\"']*\bletterhead\b", html_path.read_text(encoding="utf-8")))


def html_to_pdf(html_path: Path, out_pdf: Path, global_dir: Path, me_dir: Path, paper=False) -> int:
    staged = _staged(html_path, global_dir, me_dir, paper)
    try:
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        out_pdf.unlink(missing_ok=True)
        _edge(["--no-pdf-header-footer", f"--print-to-pdf={out_pdf}", staged.resolve().as_uri()])
    finally:
        staged.unlink(missing_ok=True)
    if not out_pdf.exists():
        raise RuntimeError("PDF render failed")
    return page_count(out_pdf)


def html_to_png(html_path: Path, out_png: Path, global_dir: Path, me_dir: Path, width=1080, height=1350):
    staged = _staged(html_path, global_dir, me_dir)
    try:
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.unlink(missing_ok=True)
        _edge(["--hide-scrollbars", f"--window-size={width},{height}", f"--screenshot={out_png}", staged.resolve().as_uri()])
    finally:
        staged.unlink(missing_ok=True)
    if not out_png.exists():
        raise RuntimeError("image render failed")


def page_count(pdf: Path) -> int:
    with pymupdf.open(pdf) as d:
        return d.page_count


def pdf_previews(pdf: Path, out_dir: Path, max_pages=3, dpi=110) -> list[Path]:
    out = []
    out_dir.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf) as d:
        for i, page in enumerate(d):
            if i >= max_pages:
                break
            p = out_dir / f"{pdf.stem}_page{i + 1}.png"
            page.get_pixmap(dpi=dpi).save(p)
            out.append(p)
    return out


def image_to_pdf(img: Path, out_pdf: Path):
    """One image on one A4 page, scaled to fit (for printing photos)."""
    with pymupdf.open() as d:
        page = d.new_page(width=595, height=842)
        page.insert_image(page.rect + (20, 20, -20, -20), filename=str(img), keep_proportion=True)
        d.save(out_pdf)


# ---------------- Office files ----------------
_office_no_pdf = False  # set once Office failed to export (e.g. Office 2007, whose PDF add-in is no longer offered)


def office_to_pdf(src: Path, out_pdf: Path):
    """PDF of a Word/Excel/PowerPoint file: through the Office installed on this PC, else through LibreOffice."""
    global _office_no_pdf
    if src.suffix.lower() not in (".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls"):
        raise ValueError(f"can't convert {src.suffix}")
    if not (_office_no_pdf and SOFFICE):
        try:
            return _office_pdf(src, out_pdf)
        except Exception:
            if not SOFFICE:
                raise
            _office_no_pdf = True  # ponytail: one failure sends the rest of this run to LibreOffice
    _libreoffice_pdf(src, out_pdf)


def _office_pdf(src: Path, out_pdf: Path):
    import pythoncom, win32com.client
    pythoncom.CoInitialize()
    src, out_pdf, app = src.resolve(), out_pdf.resolve(), None
    try:
        ext = src.suffix.lower()
        # AutomationSecurity = 3 (force-disable): files come from WhatsApp, their macros must never run
        if ext in (".pptx", ".ppt"):
            app = win32com.client.DispatchEx("PowerPoint.Application")
            app.AutomationSecurity = 3
            pres = app.Presentations.Open(str(src), WithWindow=False)
            pres.SaveAs(str(out_pdf), 32)  # ppSaveAsPDF
            pres.Close()
        elif ext in (".docx", ".doc"):
            app = win32com.client.DispatchEx("Word.Application")
            app.Visible, app.DisplayAlerts, app.AutomationSecurity = False, 0, 3
            doc = app.Documents.Open(str(src), ReadOnly=True)
            doc.ExportAsFixedFormat(str(out_pdf), 17)  # wdExportFormatPDF
            doc.Close(False)
        else:
            app = win32com.client.DispatchEx("Excel.Application")
            app.Visible, app.DisplayAlerts, app.AutomationSecurity = False, False, 3
            wb = app.Workbooks.Open(str(src), ReadOnly=True)
            wb.ExportAsFixedFormat(0, str(out_pdf))  # xlTypePDF
            wb.Close(False)
    finally:
        if app is not None:  # also when the export failed: a leftover Office process would hold on to memory
            try:
                app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def _libreoffice_pdf(src: Path, out_pdf: Path):
    src, out_pdf = src.resolve(), out_pdf.resolve()
    with LO_LOCK, tempfile.TemporaryDirectory() as tmp:
        subprocess.run([SOFFICE, "--headless", "--norestore", "--nolockcheck",
                        f"-env:UserInstallation={LO_PROFILE.resolve().as_uri()}",
                        "--convert-to", "pdf", "--outdir", tmp, str(src)], capture_output=True, timeout=170)
        made = Path(tmp) / (src.stem + ".pdf")
        if not made.exists():
            raise RuntimeError("LibreOffice couldn't convert it")
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(made), out_pdf)


def _office(kind: str):
    import pythoncom, win32com.client
    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx(kind)
    app.Visible, app.DisplayAlerts, app.AutomationSecurity = False, False, 3  # macros never run
    return app


def fill_xlsx(src: Path, out: Path, cells: dict | None = None, tables: list | None = None, sheets: list | None = None):
    """Copy an existing Excel format and fill it through Excel itself, so merges, borders, logos and print setup stay.
    cells: {"Sheet!B3": value}; tables: [{"sheet","start":"A5","rows":[[...],...]}]; sheets: keep only these."""
    import pythoncom
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)
    app = _office("Excel.Application")
    try:
        wb = app.Workbooks.Open(str(out.resolve()), UpdateLinks=0)
        names = [ws.Name for ws in wb.Worksheets]

        def sheet(n):
            if n not in names:
                raise ValueError(f"no sheet '{n}' (sheets: {', '.join(names)})")
            return wb.Worksheets(n)
        for ref, v in (cells or {}).items():
            sh, _, addr = ref.rpartition("!")
            (sheet(sh) if sh else wb.Worksheets(1)).Range(addr).Value = v
        for t in tables or []:
            rows = [list(r) for r in t["rows"]]
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                ws = sheet(t["sheet"]) if t.get("sheet") else wb.Worksheets(1)
                ws.Range(t["start"]).Resize(len(rows), width).Value = rows
        if sheets:
            for n in sheets:
                sheet(n)
            for n in names:
                if n not in sheets:
                    wb.Worksheets(n).Delete()
        wb.Save()
        wb.Close(False)
    finally:
        app.Quit()
        pythoncom.CoUninitialize()


def fill_docx(src: Path, out: Path, replace: dict):
    """Copy an existing Word format and replace text in it through Word (formatting stays as it is)."""
    import pythoncom
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)
    app = _office("Word.Application")
    missing = []
    try:
        doc = app.Documents.Open(str(out.resolve()))
        for old, new in replace.items():
            found = False
            for story in doc.StoryRanges:  # body, headers, footers, text boxes
                while story is not None:
                    r = story.Duplicate
                    # Word's Replace takes at most 255 chars: find each hit and set its text directly
                    while r.Find.Execute(FindText=str(old)[:255], MatchCase=True, Forward=True, Wrap=0):
                        r.Text = str(new)
                        found = True
                        r.Collapse(0)  # wdCollapseEnd: carry on after the new text
                    story = story.NextStoryRange
            if not found:
                missing.append(old)
        doc.Save()
        doc.Close(False)
    finally:
        app.Quit()
        pythoncom.CoUninitialize()
    return missing


def extract_text(src: Path) -> str | None:
    """Plain text of Office/CSV files (Claude reads PDFs and images natively)."""
    ext = src.suffix.lower()
    if ext == ".docx":
        import docx
        from docx.oxml.ns import qn
        d = docx.Document(src)

        def txt(el):  # all text inside, text boxes included
            return "".join(t.text or "" for t in el.iter(qn("w:t")))
        parts = []
        for el in d.element.body:  # document order: paragraphs and tables interleaved
            if el.tag == qn("w:p"):
                parts.append(txt(el))
            elif el.tag == qn("w:tbl"):
                parts += [" | ".join(txt(c) for c in row.iter(qn("w:tc"))) for row in el.iter(qn("w:tr"))]
        head = "\n".join(p.text for s in d.sections[:1] for p in s.header.paragraphs if p.text.strip())
        return (f"[header] {head}\n" if head else "") + "\n".join(parts)
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"## Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                while cells and not cells[-1].strip():
                    cells.pop()
                if cells:
                    out.append(" | ".join(cells))
        return "\n".join(out)
    if ext == ".pptx":
        import pptx
        out = []
        for i, s in enumerate(pptx.Presentation(src).slides, 1):
            out.append(f"## Slide {i}")
            out += [sh.text_frame.text for sh in s.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
        return "\n".join(out)
    if ext in (".csv", ".txt", ".md", ".json", ".html"):
        return src.read_text(encoding="utf-8", errors="replace")
    return None


# ---------------- builders from JSON specs ----------------
def _hex(c, default="1A1A1A"):
    c = (c or default).lstrip("#")
    return c if re.fullmatch(r"[0-9a-fA-F]{6}", c) else default


def make_pptx(spec: dict, resolve, out: Path):
    """spec: {"theme":{"bg","accent","text","font"}, "slides":[{"layout": title|bullets|image|images|text_image,
    "title","subtitle","bullets":[...],"images":[path...],"captions":[...],"notes"}]}"""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Pt
    th = spec.get("theme") or {}
    bg, accent, fg = (RGBColor.from_string(_hex(th.get(k), d)) for k, d in
                      (("bg", "FFFFFF"), ("accent", "8A1C1C"), ("text", "1A1A1A")))
    font = th.get("font") or "Nirmala UI"
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)  # 16:9
    W, H = prs.slide_width, prs.slide_height
    blank = prs.slide_layouts[6]

    def text(slide, s, x, y, w, h, size, bold=False, color=fg, align=None, bullets=False):
        tb = slide.shapes.add_textbox(x, y, w, h)
        tf = tb.text_frame
        tf.word_wrap = True
        items = s if isinstance(s, list) else [s]
        for i, line in enumerate(items):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = ("•  " if bullets else "") + str(line)
            if align is not None:
                p.alignment = align
            for r in p.runs:
                r.font.size, r.font.bold, r.font.name = Pt(size), bold, font
                r.font.color.rgb = color
            p.space_after = Pt(8 if bullets else 0)
        return tb

    def picture(slide, path, x, y, w, h):
        from PIL import Image
        with Image.open(path) as im:
            iw, ih = im.size
        scale = min(w / iw, h / ih)
        pw, ph = int(iw * scale), int(ih * scale)
        slide.shapes.add_picture(str(path), x + (w - pw) // 2, y + (h - ph) // 2, pw, ph)

    from pptx.enum.text import PP_ALIGN
    m = Emu(457200)  # 0.5 inch margin
    for sd in spec.get("slides", []):
        s = prs.slides.add_slide(blank)
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = bg
        bar = s.shapes.add_shape(1, 0, 0, W, Emu(91440))  # thin accent bar on top
        bar.fill.solid(); bar.fill.fore_color.rgb = accent; bar.line.fill.background()
        lay = sd.get("layout", "bullets")
        imgs = [resolve(p) for p in sd.get("images", [])]
        caps = sd.get("captions", [])
        if lay == "title":
            text(s, sd.get("title", ""), m, int(H * .32), W - 2 * m, Emu(1200000), 44, True, accent, PP_ALIGN.CENTER)
            if sd.get("subtitle"):
                text(s, sd["subtitle"], m, int(H * .52), W - 2 * m, Emu(800000), 22, False, fg, PP_ALIGN.CENTER)
        else:
            text(s, sd.get("title", ""), m, Emu(300000), W - 2 * m, Emu(800000), 30, True, accent)
            top, avail_h = Emu(1250000), H - Emu(1250000) - m
            if lay == "bullets":
                text(s, sd.get("bullets", []), m, top, W - 2 * m, avail_h, 20, bullets=True)
            elif lay == "text_image" and imgs:
                half = (W - 3 * m) // 2
                text(s, sd.get("bullets", []), m, top, half, avail_h, 20, bullets=True)
                picture(s, imgs[0], 2 * m + half, top, half, avail_h)
            elif imgs:  # image / images grid with captions under each
                n = len(imgs)
                cols = 1 if n == 1 else 2 if n in (2, 4) else 3
                rows = -(-n // cols)
                cw, ch = (W - (cols + 1) * m) // cols, (avail_h - (rows - 1) * m // 2) // rows
                cap_h = Emu(450000) if caps else 0
                for i, img in enumerate(imgs):
                    x = m + (i % cols) * (cw + m)
                    y = top + (i // cols) * (ch + m // 2)
                    picture(s, img, x, y, cw, ch - cap_h)
                    if i < len(caps) and caps[i]:
                        text(s, caps[i], x, y + ch - cap_h, cw, cap_h, 14, True, fg, PP_ALIGN.CENTER)
        if sd.get("notes"):
            s.notes_slide.notes_text_frame.text = sd["notes"]
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out)


def make_docx(spec: dict, resolve, out: Path):
    """spec: {"blocks":[{"type":"heading","text","level"},{"type":"para","text","align"},{"type":"bullets","items"},
    {"type":"table","rows":[[...]],"header":true},{"type":"image","path","width_cm"},{"type":"pagebreak"}]}"""
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt
    d = docx.Document()
    st = d.styles["Normal"]
    st.font.name, st.font.size = spec.get("font", "Nirmala UI"), Pt(12)
    st.element.rPr.rFonts.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}cs", st.font.name)
    align = {"center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}
    for b in spec.get("blocks", []):
        t = b.get("type")
        if t == "heading":
            d.add_heading(b.get("text", ""), level=int(b.get("level", 1)))
        elif t == "para":
            p = d.add_paragraph(b.get("text", ""))
            if b.get("align") in align:
                p.alignment = align[b["align"]]
            if b.get("bold"):
                for r in p.runs:
                    r.bold = True
        elif t == "bullets":
            for it in b.get("items", []):
                d.add_paragraph(str(it), style="List Bullet")
        elif t == "table" and b.get("rows"):
            rows = b["rows"]
            tbl = d.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            tbl.style = "Table Grid"
            for i, r in enumerate(rows):
                for j, v in enumerate(r):
                    tbl.cell(i, j).text = str(v)
                    if i == 0 and b.get("header", True):
                        for run in tbl.cell(i, j).paragraphs[0].runs:
                            run.bold = True
        elif t == "image":
            d.add_picture(str(resolve(b["path"])), width=Cm(float(b.get("width_cm", 12))))
        elif t == "pagebreak":
            d.add_page_break()
    out.parent.mkdir(parents=True, exist_ok=True)
    d.save(out)


def make_xlsx(spec: dict, out: Path):
    """spec: {"sheets":[{"name","rows":[[...]],"header":true,"col_widths":[...]}]}"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sh in spec.get("sheets", []) or [{"name": "Sheet1", "rows": []}]:
        ws = wb.create_sheet(str(sh.get("name", "Sheet"))[:31])
        for r in sh.get("rows", []):
            ws.append(r)
        if sh.get("header", True) and ws.max_row:
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="8A1C1C")
        for i, w in enumerate(sh.get("col_widths", []), 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        ws.freeze_panes = "A2" if sh.get("header", True) else None
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)


async def run(fn, *a, timeout=120):
    """Run a blocking builder in a thread with a time limit."""
    return await asyncio.wait_for(asyncio.to_thread(fn, *a), timeout)


if __name__ == "__main__":
    # self-check: fill the notice template, render PDF + preview, build pptx/docx/xlsx, read them back
    tmp = Path(tempfile.mkdtemp())
    G = ROOT / "memory" / "global"
    html = (G / "templates" / "notice.html").read_text(encoding="utf-8")
    html = html.replace("<!-- DATE -->", "23/10/2026").replace(
        "<!-- BODY: <p> paragraphs, lists or a <table> -->",
        "<p>सभी छात्रों को सूचित किया जाता है कि दिनांक 25/10/2026 को संस्था में अवकाश रहेगा।</p>")
    (tmp / "n.html").write_text(html, encoding="utf-8")
    pages = html_to_pdf(tmp / "n.html", tmp / "n.pdf", G, tmp)
    prev = pdf_previews(tmp / "n.pdf", tmp)
    assert pages == 1 and prev and prev[0].stat().st_size > 5000, (pages, prev)
    assert "अवकाश" in pymupdf.open(tmp / "n.pdf")[0].get_text(), "hindi text missing from pdf"
    html_to_png(tmp / "n.html", tmp / "n.png", G, tmp, 800, 1000)
    shutil.copy(prev[0], tmp / "img.png")
    make_pptx({"slides": [{"layout": "title", "title": "स्टाफ परिचय", "subtitle": "MNA"},
                          {"layout": "images", "title": "हमारी टीम", "images": ["img.png"] * 3, "captions": ["A", "B", "C"]},
                          {"layout": "text_image", "title": "Notice", "bullets": ["एक", "दो"], "images": ["img.png"]}]},
              lambda p: tmp / p, tmp / "t.pptx")
    make_docx({"blocks": [{"type": "heading", "text": "सूचना"}, {"type": "para", "text": "नमस्ते"},
                          {"type": "table", "rows": [["नाम", "पद"], ["डॉ. शर्मा", "निदेशक"]]}]}, lambda p: tmp / p, tmp / "t.docx")
    make_xlsx({"sheets": [{"name": "Staff", "rows": [["Name", "Post"], ["Dr. Sharma", "Director"]]}]}, tmp / "t.xlsx")
    for f in ("t.pptx", "t.docx", "t.xlsx"):
        txt = extract_text(tmp / f)
        assert txt and ("शर्मा" in txt or "Sharma" in txt or "स्टाफ" in txt), (f, txt)
    image_to_pdf(tmp / "img.png", tmp / "img.pdf")
    office_to_pdf(tmp / "t.pptx", tmp / "t_pptx.pdf")
    assert page_count(tmp / "t_pptx.pdf") == 3
    print("docs ok:", tmp)
