"""Builds the PDF manuals: content.<lang>.html + cover + table of contents → PDF (two passes, so the page
numbers in the table of contents are right). Screenshots come from screenshots.py.

    python3 docs/manual/screenshots.py              # img/de/*.png and img/en/*.png (needs Playwright/Chromium)
    python3 docs/manual/build.py de                  # → docs/Handbuch-AssetLotse.pdf
    python3 docs/manual/build.py en                  # → docs/Manual-AssetLotse.pdf

Needs pypdf and Playwright with Chromium. The version comes from assetlotse/__init__.py.
"""
from __future__ import annotations

import html
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

from pypdf import PdfReader, PdfWriter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FONTS = ROOT / "assetlotse" / "static" / "fonts"
LANG = sys.argv[1] if len(sys.argv) > 1 else "de"
if LANG not in ("de", "en"):
    raise SystemExit("usage: build.py de|en")
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "assetlotse" / "__init__.py").read_text()).group(1)
MONTHS = {"de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
                 "November", "Dezember"),
          "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December")}
DATE = f"{MONTHS[LANG][date.today().month - 1]} {date.today().year}"
T = {
    "de": {"out": "Handbuch-AssetLotse.pdf", "title": "AssetLotse – Handbuch", "head": "AssetLotse · Handbuch",
           "eyebrow": "Handbuch", "toc": "Inhalt", "page": "Seite", "chapter": "Kapitel", "as_of": "Stand",
           "lead": "Arbeitsmittel ausgeben, zurücknehmen und im Blick behalten – für Mitarbeitende, Verwalter und "
                   "Administratoren.", "subject": "Handbuch zu AssetLotse", "keywords": "AssetLotse, Handbuch, Inventar"},
    "en": {"out": "Manual-AssetLotse.pdf", "title": "AssetLotse – Manual", "head": "AssetLotse · Manual",
           "eyebrow": "Manual", "toc": "Contents", "page": "Page", "chapter": "Chapter", "as_of": "As of",
           "lead": "Issue equipment, check it back in and keep track of it – for staff, managers and administrators.",
           "subject": "Manual for AssetLotse", "keywords": "AssetLotse, manual, inventory"},
}[LANG]
OUT = ROOT / "docs" / T["out"]
IMG = HERE / "img" / LANG

CSS = f"""
@font-face {{ font-family: Inter; font-weight: 100 900; src: url('file://{FONTS}/inter-latin-wght-normal.woff2') format('woff2');
  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD; }}
@font-face {{ font-family: Inter; font-weight: 100 900; src: url('file://{FONTS}/inter-latin-ext-wght-normal.woff2') format('woff2');
  unicode-range: U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF; }}
:root {{ --ink: #1f2933; --ink2: #3e4c59; --muted: #66737f; --line: #d9e0e6; --accent: #0b7285; --accent-soft: #e3f2f4;
  --warn: #9a6200; --warn-soft: #fdf2dc; --danger: #b42318; --danger-soft: #fde8e6; --ok: #1e7a4c; --ok-soft: #e1f3e8;
  --side: #1f2933; --paper2: #f3f5f7; }}
* {{ box-sizing: border-box; }}
html {{ font-family: Inter, Helvetica, Arial, sans-serif; color: var(--ink); font-size: 10pt; line-height: 1.5;
  font-feature-settings: "cv11", "ss01"; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ margin: 0; }}
p {{ margin: 0 0 7pt; orphans: 3; widows: 3; }}
a {{ color: var(--accent); text-decoration: none; }}
b {{ font-weight: 650; }}
code, pre, .mono-first td:first-child {{ font-family: "DejaVu Sans Mono", Menlo, Consolas, monospace; font-size: 8.6pt; }}
code {{ background: var(--paper2); border-radius: 3px; padding: 0.5pt 3pt; color: #243b53; }}
pre {{ background: #1f2933; color: #e6eaee; border-radius: 5px; padding: 8pt 10pt; margin: 4pt 0 9pt; white-space: pre-wrap;
  break-inside: avoid; line-height: 1.45; }}
ul, ol {{ margin: 0 0 8pt; padding-left: 15pt; }}
li {{ margin-bottom: 3pt; }}
.ui {{ font-weight: 600; color: #102a43; background: #eef1f4; border: 0.5pt solid #d3dae0; border-radius: 3px; padding: 0 3pt;
  white-space: nowrap; font-size: 9.3pt; }}
.kbd {{ font-family: "DejaVu Sans Mono", monospace; font-size: 8.4pt; border: 0.6pt solid #b8c2cc; border-bottom-width: 1.4pt;
  border-radius: 3px; padding: 0 3pt; background: #fff; }}
.badge {{ display: inline-block; font-size: 8.2pt; font-weight: 650; padding: 0.5pt 5pt; border-radius: 3px; background: var(--paper2); }}
.badge.ok {{ background: var(--ok-soft); color: var(--ok); }} .badge.warn {{ background: var(--warn-soft); color: var(--warn); }}
.badge.danger {{ background: var(--danger-soft); color: var(--danger); }} .badge.accent {{ background: var(--accent-soft); color: var(--accent); }}

h1.part {{ break-before: page; margin: 0 0 10pt; padding: 26mm 0 10pt; font-size: 26pt; line-height: 1.15; font-weight: 750;
  letter-spacing: -0.02em; border-bottom: 2.5pt solid var(--accent); }}
h1.part .part-no {{ display: block; font-size: 10pt; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--accent); margin-bottom: 6pt; }}
.part-lead {{ font-size: 11.5pt; color: var(--ink2); max-width: 150mm; margin-bottom: 16pt; }}
h2 {{ font-size: 15.5pt; font-weight: 720; letter-spacing: -0.01em; margin: 20pt 0 8pt; padding-top: 4pt; break-after: avoid;
  line-height: 1.25; }}
h2 .num {{ color: var(--accent); margin-right: 8pt; font-variant-numeric: tabular-nums; }}
h1.part + .part-lead + h2 {{ margin-top: 8pt; }}
h3 {{ font-size: 11.5pt; font-weight: 700; margin: 13pt 0 5pt; break-after: avoid; color: #102a43; }}
table {{ border-collapse: collapse; width: 100%; margin: 4pt 0 10pt; font-size: 9.2pt; }}
table.grid th, table.grid td {{ border-bottom: 0.6pt solid var(--line); padding: 4.5pt 6pt; text-align: left; vertical-align: top; }}
table.grid th {{ font-size: 7.8pt; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 650;
  border-bottom: 1pt solid #b8c2cc; }}
table.grid tr {{ break-inside: avoid; }}
table.small {{ font-size: 8.6pt; }}
.mono-first td:first-child {{ white-space: nowrap; color: #243b53; }}
figure {{ margin: 6pt 0 12pt; break-inside: avoid; }}
figure img {{ width: 100%; display: block; border: 0.6pt solid #c9d2da; border-radius: 4px; box-shadow: 0 1pt 3pt rgba(16,24,40,.08); }}
figure img.framed {{ padding: 4pt; background: #fff; }}
figcaption {{ font-size: 8.3pt; color: var(--muted); margin-top: 4pt; }}
figure.half-right {{ float: right; width: 47%; margin: 2pt 0 8pt 12pt; }}
figure.half-right.narrow {{ width: 40%; }}
.clear {{ clear: both; }}
h2, h1.part {{ clear: both; }}
.note, .warn, .tip {{ border-left: 3pt solid var(--accent); background: var(--accent-soft); padding: 7pt 10pt; margin: 6pt 0 11pt;
  border-radius: 0 4px 4px 0; break-inside: avoid; font-size: 9.4pt; }}
.warn {{ border-color: #d08a00; background: var(--warn-soft); }}
.tip {{ border-color: var(--ok); background: var(--ok-soft); }}
ol.steps {{ counter-reset: s; list-style: none; padding-left: 0; }}
ol.steps > li {{ counter-increment: s; position: relative; padding-left: 21pt; margin-bottom: 5pt; }}
ol.steps > li::before {{ content: counter(s); position: absolute; left: 0; top: 0.5pt; width: 14pt; height: 14pt; border-radius: 50%;
  background: var(--accent); color: #fff; font-size: 8pt; font-weight: 700; text-align: center; line-height: 14pt; }}
dl.faq dt {{ font-weight: 700; margin-top: 8pt; break-after: avoid; color: #102a43; }}
dl.faq dd {{ margin: 2pt 0 6pt 0; padding-left: 10pt; border-left: 1.5pt solid var(--line); }}

/* table of contents */
.toc-title {{ font-size: 22pt; font-weight: 750; letter-spacing: -0.02em; margin: 6mm 0 12pt; padding-bottom: 8pt;
  border-bottom: 2.5pt solid var(--accent); }}
.toc {{ list-style: none; padding: 0; margin: 0; }}
.toc li {{ display: flex; align-items: baseline; margin: 0; }}
.toc li a {{ color: var(--ink); display: flex; width: 100%; align-items: baseline; }}
.toc li .t {{ flex: none; }}
.toc li .dots {{ flex: 1; border-bottom: 0.8pt dotted #a5b0ba; margin: 0 5pt; transform: translateY(-3pt); }}
.toc li .p {{ flex: none; font-variant-numeric: tabular-nums; color: var(--ink2); min-width: 14pt; text-align: right; }}
.toc li.part {{ font-weight: 750; font-size: 10.5pt; margin-top: 8pt; padding-bottom: 2pt; }}
.toc li.part .t span {{ color: var(--accent); margin-right: 6pt; }}
.toc li.ch {{ padding: 1.7pt 0 1.7pt 0; font-size: 9.6pt; }}
.toc li.ch .n {{ display: inline-block; width: 20pt; color: var(--accent); font-weight: 650; font-variant-numeric: tabular-nums; }}
.toc-end {{ break-after: page; }}
"""

COVER_CSS = """
@page { size: A4; margin: 0; }
html, body { margin: 0; height: 100%; }
.cover { position: relative; width: 210mm; height: 297mm; background: #1f2933; color: #fff; overflow: hidden; }
.band { position: absolute; left: 0; right: 0; bottom: 0; height: 92mm; background: #0b7285; }
.shelf { position: absolute; left: 22mm; bottom: 92mm; display: flex; align-items: flex-end; gap: 3.2mm; }
.shelf div { width: 9mm; border-radius: 1.5mm 1.5mm 0 0; background: rgba(255,255,255,.12); }
.brand { position: absolute; top: 24mm; left: 22mm; display: flex; align-items: center; gap: 4mm; font-size: 13pt; font-weight: 700; }
.mark { width: 12mm; height: 12mm; border-radius: 3mm; background: #0b7285; display: flex; align-items: center; justify-content: center; }
.title { position: absolute; top: 88mm; left: 22mm; right: 22mm; }
.title .eyebrow { font-size: 10.5pt; letter-spacing: .18em; text-transform: uppercase; color: #8fd3dd; font-weight: 700; }
.title h1 { font-size: 52pt; line-height: 1; margin: 6mm 0 7mm; font-weight: 780; letter-spacing: -0.03em; }
.title p { font-size: 14pt; line-height: 1.45; color: #d4dbe1; max-width: 140mm; margin: 0; }
.meta { position: absolute; left: 22mm; bottom: 22mm; right: 22mm; display: flex; justify-content: space-between;
  font-size: 10pt; color: #e3f2f4; }
.meta b { display: block; font-size: 12pt; color: #fff; margin-bottom: 1mm; }
"""



def strip_tags(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


class _Groups:
    """A match of either heading alternative, seen as groups 1 and 2."""

    def __init__(self, m):
        self._m = m

    def group(self, i):
        m = self._m
        if i == 0:
            return m.group(0)
        return m.group(i) if m.group(1) is not None else m.group(i + 2)


def prepare(content: str):
    """Number the chapters, collect the entries for the table of contents."""
    entries, n = [], 0

    def h1(m):
        entries.append(("part", m.group(1), strip_tags(m.group(2)), m.group(2)))
        return m.group(0)

    def h2(m):
        nonlocal n
        n += 1
        entries.append(("ch", m.group(1), strip_tags(m.group(2)), str(n)))
        return f'<h2 id="{m.group(1)}"><span class="num">{n}</span>{m.group(2)}</h2>'

    def heading(m):                        # one pass, so the entries stay in document order
        return h1(m) if m.group(0).startswith("<h1") else h2(m)
    content = re.sub(r'<h1 id="([^"]+)" class="part">(.*?)</h1>|<h2 id="([^"]+)">(.*?)</h2>',
                     lambda m: heading(_Groups(m)), content, flags=re.S)
    # references "Kapitel <a href="#x">…</a>" / "Chapter …" get the right number
    nums = {e[1]: e[3] for e in entries if e[0] == "ch"}
    word = T["chapter"]
    content = re.sub(rf'<a href="#([^"]+)">{word} \d+</a>', lambda m: f'<a href="#{m.group(1)}">{word} {nums[m.group(1)]}</a>',
                     content)
    content = content.replace('src="img/', f'src="file://{IMG}/')
    return content, entries


def toc_html(entries, pages):
    rows = []
    for kind, ident, title, extra in entries:
        p = pages.get(ident, "00")
        if kind == "part":
            m = re.match(r'<span class="part-no">(.*?)</span>(.*)', extra, re.S)
            t = f"<span>{m.group(1)}</span>{m.group(2)}" if m else title
            rows.append(f'<li class="part"><a href="#{ident}"><span class="t">{t}</span><span class="dots"></span>'
                        f'<span class="p">{p}</span></a></li>')
        else:
            rows.append(f'<li class="ch"><a href="#{ident}"><span class="t"><span class="n">{extra}</span>'
                        f'{html.escape(title)}</span><span class="dots"></span><span class="p">{p}</span></a></li>')
    return (f'<div class="toc-title">{T["toc"]}</div><ul class="toc">' + "\n".join(rows) + '</ul><div class="toc-end"></div>')


def page_html(body: str, title: str) -> str:
    return (f'<!doctype html><html lang="{LANG}"><head><meta charset="utf-8"><title>{title}</title>'
            f'<style>{CSS}</style></head><body>{body}</body></html>')


def cover_html() -> str:
    heights = [34, 52, 44, 60, 38, 48, 66, 42, 56, 36, 50, 62, 40]
    shelf = "".join(f'<div style="height:{h}mm"></div>' for h in heights)
    compass = ('<svg width="7mm" height="7mm" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8">'
               '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5Z"/></svg>')
    return (f'<!doctype html><html lang="{LANG}"><head><meta charset="utf-8"><style>{CSS}{COVER_CSS}</style></head><body>'
            f'<div class="cover"><div class="brand"><span class="mark">{compass}</span>AssetLotse</div>'
            f'<div class="title"><div class="eyebrow">{T["eyebrow"]}</div><h1>AssetLotse</h1>'
            f'<p>{T["lead"]}</p></div>'
            f'<div class="shelf">{shelf}</div><div class="band"></div>'
            f'<div class="meta"><div><b>Version {VERSION}</b>{T["as_of"]}: {DATE}</div>'
            f'<div style="text-align:right"><b>AGPL-3.0-or-later</b>helfi.space</div></div></div></body></html>')


def render(page, html_path: Path, pdf_path: Path, header: bool) -> None:
    page.goto(html_path.as_uri(), wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    style = ("font-family: Inter, Helvetica, Arial, sans-serif; font-size: 7.5pt; color: #66737f; width: 100%; "
             "padding: 0 18mm; display: flex; justify-content: space-between; -webkit-print-color-adjust: exact;")
    opts = {"path": str(pdf_path), "format": "A4", "print_background": True, "outline": True, "tagged": True}
    if header:
        opts.update(display_header_footer=True,
                    margin={"top": "20mm", "bottom": "18mm", "left": "18mm", "right": "18mm"},
                    header_template=f'<div style="{style}"><span>{T["head"]}</span><span>Version {VERSION}</span></div>',
                    footer_template=f'<div style="{style}"><span></span><span>{T["page"]} '
                                    f'<span class="pageNumber"></span></span></div>')
    else:
        opts.update(margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
    page.pdf(**opts)


def norm(text: str) -> str:
    return "".join(text.split()).casefold()


def outline_pages(pdf_path: Path) -> list[tuple[str, int]]:
    reader = PdfReader(str(pdf_path))
    flat = []

    def walk(items):
        for it in items:
            if isinstance(it, list):
                walk(it)
            else:
                flat.append((" ".join(str(it.title).split()), reader.get_destination_page_number(it) + 1))
    walk(reader.outline)
    return flat


def main() -> None:
    from playwright.sync_api import sync_playwright
    missing = sorted({m for m in re.findall(r'src="img/([^"]+)"', (HERE / f"content.{LANG}.html").read_text(encoding="utf-8"))
                      if not (IMG / m).exists()})
    if missing:
        raise SystemExit(f"Screenshots missing in {IMG}: {missing} – run screenshots.py first")
    content, entries = prepare((HERE / f"content.{LANG}.html").read_text(encoding="utf-8"))
    work = Path(tempfile.mkdtemp(prefix=f"assetlotse-manual-{LANG}-"))
    pages: dict[str, int] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for _pass in range(3):
            (work / "body.html").write_text(page_html(toc_html(entries, pages) + content, T["title"]), encoding="utf-8")
            render(page, work / "body.html", work / "body.pdf", header=True)
            flat = outline_pages(work / "body.pdf")
            new, pos = {}, 0
            for _kind, ident, title, _extra in entries:
                for i in range(pos, len(flat)):
                    a, b = norm(title), norm(flat[i][0])   # the outline drops spaces and keeps CSS upper case
                    if a and a in b:
                        new[ident] = flat[i][1] + 1   # +1: the cover comes first
                        pos = i + 1
                        break
            lost = [e[1] for e in entries if e[1] not in new]
            if lost:
                raise SystemExit(f"Not found in the PDF outline: {lost}")
            if new == pages:
                break
            pages = new
        (work / "cover.html").write_text(cover_html(), encoding="utf-8")
        render(page, work / "cover.html", work / "cover.pdf", header=False)
        browser.close()
    writer = PdfWriter()
    writer.append(str(work / "cover.pdf"), import_outline=False)
    writer.append(str(work / "body.pdf"))
    writer.add_metadata({"/Title": T["title"], "/Author": "helfi.space", "/Subject": f"{T['subject']} {VERSION}",
                         "/Keywords": T["keywords"]})
    writer.page_mode = "/UseOutlines"
    with open(OUT, "wb") as f:
        writer.write(f)
    print(json.dumps({"file": str(OUT.relative_to(ROOT)), "pages": len(writer.pages), "toc": pages}, ensure_ascii=False))


if __name__ == "__main__":
    main()
