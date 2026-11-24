# Handbücher / Manuals

- `Handbuch-AssetLotse.pdf` – Handbuch (Deutsch) für Mitarbeitende, Verwalter und Administratoren.
- `Manual-AssetLotse.pdf` – manual (English) for staff, managers and administrators.

Die Hilfe im Programm verlinkt automatisch das Handbuch in der Sprache der Oberfläche (*Hilfe → Handbuch (PDF)*).
The in-app help links the manual in the language of the interface (*Help → Manual (PDF)*).

## Building / Neu erstellen

Sources are in `manual/`: `content.de.html`, `content.en.html` (text), `screenshots.py` (photographs the program with
example data into `manual/img/`, not in git) and `build.py` (cover, table of contents, PDF). Needs Playwright with
Chromium, pypdf and poppler (`pdftoppm`):

```sh
python3 docs/manual/screenshots.py
python3 docs/manual/build.py de
python3 docs/manual/build.py en
```

Update the text and rebuild whenever the visible program changes. The version on the cover comes from
`assetlotse/__init__.py`.
