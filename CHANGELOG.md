# Änderungen an AssetLotse

Die aktuelle Version steht nach der Anmeldung unten links in der Seitenleiste.

## 2.0.1 – 2026-11-01

**Korrigiert**
- **AD-Anmeldung mit falschem Passwort:** Statt „Benutzername oder Passwort ist falsch.“ erschien „Anmeldung derzeit nicht
  möglich“, und im Protokoll stand jedes Mal ein Programmfehler. Ebenso endete „Verbindung testen“ mit einem falschen
  Passwort des Dienstkontos in einem Fehler statt mit der Meldung des Domänencontrollers. Gefunden von der neuen
  Typprüfung.

**Intern**
- **Typprüfung mit mypy**, schrittweise eingeführt: Sicherung, Netz- und Proxy-Erkennung, Anmeldesperre und die
  Build-Skripte werden streng geprüft – ein Fund dort lässt die Tests scheitern, auch im Prüflauf auf GitHub. Alle
  anderen Module werden mitgeprüft, ihre Funde aber vorerst nur aufgelistet. Weitere Module kommen nach und nach dazu.
  Das Docker-Image enthält mypy nicht.

## 2.0.0 – 2026-10-29

Umstellung auf den Docker-Standard: Das Programm steckt komplett im Image. Auf dem Server liegen nur noch
`docker-compose.yml`, `.env` und der Ordner `data`.

**Neu**
- **Installation:** `docker-compose.yml` und `env.example` (als `.env`) aus dem Release, `docker compose up -d` –
  kein Zip, kein Programmordner, nichts wird auf dem Server gebaut.
- **Updates wie bei jedem Docker-Programm:** mit Dockhand, Watchtower, Tugtainer & Co. oder
  `docker compose pull && docker compose up -d`. Die `docker-compose.yml` folgt `latest`.
- **Images für amd64 und arm64** (z. B. Raspberry Pi, ARM-NAS) – jede Architektur auf eigener Hardware gebaut und
  getestet, beide geprüft.
- **Ältere Version auf neuerer Datenbank:** AssetLotse startet dann nicht und nennt die Sicherung, die
  zurückzuspielen ist – statt mit Folgefehlern weiterzulaufen. Zurückspielen geht auch, wenn das Programm nicht
  startet: `docker compose run --rm assetlotse backups` / `restore NAME`.
- **Sicherung vor jeder neuen Version** heißt jetzt `…-vor-<Version>.db`; die drei neuesten davon werden nie
  automatisch gelöscht.
- **Versionsnummer der `docker-compose.yml`:** Ist sie zu alt, zeigt AssetLotse Administratoren einen Hinweis mit
  dem, was zu tun ist. Das Image bringt die aktuelle Datei mit.
- **Ohne Internet:** Am Release hängen die Images als Dateien (`assetlotse-<Version>-amd64.tar.gz`,
  `…-arm64.tar.gz`) mit Prüfsumme; `./update.sh --offline DATEI`.

**Geändert**
- **`update.sh` neu** (läuft mit `/bin/sh`, auch auf einem NAS): merkt sich das laufende Image als `previous`,
  lädt die Version, startet, wartet auf den Gesundheitscheck und startet bei einem Fehlstart auf Wunsch die vorige
  Version wieder (`-y` automatisch, später `--rollback`). Eine neuere `docker-compose.yml` aus dem Image wird
  abgelegt, die alte bleibt als `docker-compose.yml.alt`. Kein Entpacken, kein Bauen mehr.
- **`update.sh --cleanup`** räumt Installationen von vor 2.0.0 auf: zeigt die nicht mehr gebrauchten
  Programmdateien, alte Zips und alte lokal gebaute Images, fragt nach und löscht nur diese – nie Daten,
  Sicherungen, `.env`, `docker-compose.yml` oder eingebundene Ordner, auch nicht über Links.
- **Veröffentlicht wird nur noch von Hand** (*Run workflow → release*). Jeder Push und jeder Pull Request wird nur
  geprüft (bauen, testen, Trivy). Die wöchentlichen Sicherheitsupdates erscheinen weiter automatisch.
- Jedes GitHub-Release enthält die Installationsdateien (`docker-compose.yml`, `env.example`, `update.sh`) und die
  Image-Dateien.

- Pakete auf aktuellem Stand (u. a. Python 3.13.18).

**Entfernt**
- Der Zip-Weg (`./update.sh assetlotse-X.Y.Z.zip`), das lokale Bauen auf dem Server und die Anleitung für den
  Betrieb ohne Docker. Docker ist Voraussetzung.

**Umstieg:** einmalig `./update.sh --pull`, danach `./update.sh --cleanup` (siehe README, „Umstieg von einer
Installation vor 2.0.0“).

## 1.12.0 – 2026-10-28

Ergebnis eines vollständigen Code-Reviews.

**Geändert**
- **„Verwaltet den gesamten Bestand“ ist ein eigener Haken** beim Benutzer. Bisher galt ein Verwalter ohne Gruppe
  automatisch als zuständig für alles – verlor er seine letzte Gruppe (etwa durch den AD-Abgleich), sah er plötzlich
  den ganzen Bestand. Jetzt verwaltet er dann nichts. Verwalter, die beim Update keine Gruppe haben, bekommen den
  Haken einmalig automatisch, damit sich für sie nichts ändert. Wird jemand (auch über das AD) zu einer anderen
  Rolle, verschwindet der Haken.
- **Ersteinrichtung nur direkt:** `/setup` ist nur noch von localhost oder direkt aus dem internen Netz erreichbar;
  Proxy-Kopfzeilen (`X-Forwarded-For`) werden dafür nicht mehr beachtet.
- **Image ohne apt:** Paketverwaltung (apt, libapt-pkg) wird nach der Installation entfernt.
- Hinweise oben auf der Seite verschwinden nach 20 Sekunden (bleiben stehen, solange die Maus darauf ist).
  Fehler bleiben stehen.

**Korrigiert**
- **E-Mail-Änderung im Profil:** höchstens drei Bestätigungsmails pro Stunde; nur der neueste Link gilt und nur
  einmal; Domain und Eindeutigkeit werden beim Klick erneut geprüft; die wartende Adresse steht im Profil. Die Mail
  enthält keinen frei wählbaren Text mehr. Jede Adresse gilt jetzt für alle lokalen Konten nur einmal.
- Gegenstände in Gruppen mit „Kein Verleih“ zählen nicht mehr als „verfügbar“ (Filter und Startseite) und lassen
  sich von Ausleihenden nicht selbst verlängern.
- Ausgabetheke: Hat die gewählte Person den Gegenstand schon, ist „Ausgeben“ gesperrt („hat ihn schon“); ein
  nicht erreichbares AD führt zu einer Meldung statt eines Fehlers; nach einer abgelehnten Ausgabe bleibt die Person
  gewählt; Höchstzahl an Ausleihen auch bei gleichzeitigen Ausgaben (PostgreSQL) eingehalten.
- Aufbewahrungsfristen löschen nur, wenn die Sicherung am selben Tag erfolgreich war und danach keine scheiterte.
- Die tägliche Übersicht an Verwalter läuft weiter, wenn sie für einen einzelnen Verwalter scheitert.
- Der Hinweis zum Abschalten von Benachrichtigungen steht nur noch in Ausleih-Mails.
- Bedienung: Beschriftungen für Bildschirmleser an Such- und Filterfeldern, Fotos und Ausgabezeile auch auf schmalen
  Bildschirmen, „Foto entfernen“ bleibt nach einem Fehler gewählt.

**Build und Update**
- Ein Release darf keine älteren Pakete enthalten als das zuletzt veröffentlichte Sicherheitsupdate.
- Wiederholte Läufe veröffentlichen nicht doppelt; fehlende GitHub-Releases werden nachgetragen; ein
  Sicherheitsupdate wird nicht veröffentlicht, wenn sich `main` während des Laufs geändert hat.
- Prüfläufe (Scan, manuell) warten nicht mehr auf Veröffentlichungen. Fehler zeigen eine lesbare Meldung.
- pip wird als Kalenderversion behandelt (neue Jahresversion ist kein Versionssprung).
- `update.sh` kennt Sicherheitsupdates (`--version 1.12.0-s1`), kehrt bei Problemen zum vorher laufenden Build zurück
  und prüft, ob alle Build-Dateien vorhanden sind.

## 1.11.0 – 2026-10-27

**Neu**
- **Aus dem Bestand ausgeben:** In der Bestandsliste steht bei jedem freien Gegenstand **Ausgeben**, auf der Seite
  eines Gegenstands **Person suchen**. Beides öffnet die Ausgabetheke mit dem schon gewählten Gegenstand – Person
  suchen (auch im AD), bestätigen, fertig; danach geht es zurück zum Gegenstand.
- **Gruppen mit „Kein Verleih“:** Neuer Haken in der Gruppe. Ihre Gegenstände werden nie ausgegeben – weder an der
  Theke (dort gar nicht erst angeboten) noch aus dem Bestand noch zur Selbstausleihe. Im Bestand steht
  „kein Verleih“. Schon ausgegebene Gegenstände können weiter zurückgegeben werden.
- **Vorschläge beim Erfassen:** Bezeichnung, Hersteller und Modell werden aus dem vorhandenen Bestand
  vorgeschlagen. Wer eine bekannte Bezeichnung oder ein bekanntes Modell wählt (z. B. „Vostro 3400“), bekommt leere
  Felder wie Hersteller und Kategorie gleich passend ausgefüllt.
- **„Ähnlichen anlegen“ übernimmt das Foto** (auch „Speichern & nächsten“); abwählbar.
- **Eigene Angaben:** Lokale und selbst registrierte Konten ändern Name und E-Mail-Adresse unter *Mein Profil →
  Meine Angaben* selbst. Bei AD-Konten kommen beide weiter aus dem Active Directory. Für selbst registrierte Konten
  gelten dieselben Regeln wie bei der Registrierung: erlaubte Domain, jede Adresse nur einmal, und – wenn dort eine
  Bestätigung verlangt wird – gilt die neue Adresse erst nach Klick auf den Link in der Mail.

**Korrigiert**
- **Falsch vergebene Inventar- oder Seriennummer korrigieren:** Steht eine Nummer versehentlich bei einem anderen
  Gerät, bietet das Formular an, sie dort zu entfernen und hier einzutragen – in einem Schritt. Vertauschte Nummern
  lassen sich so korrigieren, ohne ein Gerät zu löschen und neu anzulegen.

## 1.10.2 – 2026-10-25

**Geändert**
- **Der Hauptzweig heißt jetzt `main`** (vorher `master`). Workflow, Skripte, Selbsttests und Doku sind umgestellt;
  veröffentlicht wird nur von `main`. Auf GitHub die Umgebung `release` entsprechend auf `main` beschränken.
  Lokale Kopien: `git branch -m master main && git fetch origin && git branch -u origin/main main`.

Für die App selbst ändert sich nichts.

## 1.10.1 – 2026-10-25

**Neu**
- **Releases auf GitHub:** Jede neue Version und jedes Sicherheitsupdate (`<Version>-s<Zähler>`) bekommt nach dem
  Veröffentlichen eine eigene Seite unter *Releases* – mit dem passenden Abschnitt aus diesem CHANGELOG. Als
  „Latest“ markiert wird nur, was auch `latest` ist. Erst nach bestandenen Tests und Prüfung, wie das Image.

Für die App selbst ändert sich nichts.

## 1.10.0 – 2026-10-25

**Sicherheit – Image bleibt automatisch sicher und aktuell, ohne Versionssprünge**
- **Reproduzierbarer Bau:** Basis-Image mit fester Version und Prüfsumme, **alle Debian-Pakete** (auch die des
  Basis-Images) mit exakter Version in `build/debian-packages.lock` (fester Stand von snapshot.debian.org), alle
  Python-Pakete inkl. Unterabhängigkeiten exakt in `requirements.txt`, `pip` als `ARG PIP_VERSION`.
  `scripts/lock.py refresh` legt neue Versionen fest – nur innerhalb des Unterbaus; auf GitHub:
  *Run workflow → relock*.
- **Grenzen:** gleiche Debian-Version (trixie), gleiche Python-Reihe (3.13), jedes Paket innerhalb seiner
  Hauptversion (vor 1.0: Unterversion), Kalenderversionen (Stammzertifikate, Zeitzonen) ohne Grenze.
  `requirements.in` entsprechend auf die Hauptversionen erweitert.
- **Sicherheitsupdate jede Woche** (montags; von Hand *Run workflow → security-update*): neue Versionen innerhalb
  der Grenzen, Bau ohne Zwischenspeicher, Tests im Image, Trivy – nur wenn alles besteht, Veröffentlichung als
  `<Version>-s<Zähler>` (z. B. `1.10.0-s1`) und `latest`, Eintrag hier im CHANGELOG mit den geänderten Paketen.
  Nichts Neues → nichts passiert. Ersetzt den bisherigen Montagsneubau (`<Version>-<Datum>`).
- **Trivy ohne GitHub Action:** feste Version, per SHA-256 geprüft (trivy-action/setup-trivy wurden im März 2026
  kompromittiert, CVE-2026-33634). `.trivyignore`-Einträge brauchen Begründung und Ablaufdatum.
- **Täglicher Scan** des veröffentlichten `latest`; **Unterbau-Warnung** montags über endoflife.date
  (Hinweis ab 12, roter Lauf ab 6 Monaten vor Ende der Sicherheitsupdates).
- **Workflow:** Release nur vom Zweig `master` (keine Releases mehr über Tag-Pushes oder `main`), nie zwei
  gleichzeitig, veröffentlichte Versionen werden nie überschrieben, `latest` nur für die höchste Version,
  keine gespeicherten Git-Anmeldedaten, Tests und Scans nie in Jobs mit Schreibrechten.
- `ensurepip` wird zusätzlich zu `pip` aus dem Image entfernt; Python 3.13.17 mit neu festgelegten Debian-Paketen.
- Halb abgebrochene Läufe holt der nächste nach (`latest`/`<Minor>` werden nur vorwärts nachgezogen).
- Lokaler Bau (`./update.sh` mit Zip) braucht jetzt HTTPS-Zugang zu `snapshot.debian.org`.
- Einmalig auf GitHub: Umgebung `release` auf den Zweig `master` beschränken (siehe README).

**Dokumentation:** README (Abschnitt „Sicherheit und Aktualität des Images“, inkl. „Wenn ein Lauf rot ist“) und
Hilfe *Update und Wartung*.

Für die Bedienung der App ändert sich nichts.

## 1.9.5 – 2026-10-24

**Sicherheit**
- **Der Montagsneubau aktualisiert jetzt auch die Python-Pakete:** jeweils die neueste Version innerhalb der Bereiche
  aus `requirements.in`, Unterabhängigkeiten eingeschlossen. Tests und Trivy laufen gegen genau diese Versionen;
  nur wenn beides besteht, wird veröffentlicht. Sprünge über die Bereiche hinaus kommen weiterhin als neue Version.
- Feste Paketversionen aktualisiert: cryptography 50.0.2, SQLAlchemy 2.1.6, WTForms 3.3.1, MarkupSafe 3.0.4,
  certifi 2026.10.23, charset-normalizer 3.5.2, pycparser 3.12.

## 1.9.4 – 2026-10-24

**Sicherheit**
- **Wöchentlicher Sicherheits-Neubau statt reiner Nachprüfung:** Jeden Montag baut GitHub die höchste Version neu
  (aus ihrem Tag, neuestes Python-Image der Reihe, alle Debian-Updates), lässt alle Tests im fertigen Image laufen
  und prüft mit Trivy. Nur wenn beides besteht und sich etwas geändert hat, wird das Image als `latest`, `<Minor>`
  und `<Version>-<Datum>` veröffentlicht. Das exakte Versions-Tag bleibt unverändert. Von Hand: *Run workflow → weekly*.
- **Tests laufen jetzt auch bei jedem Release** im gebauten Image (vorher nur lokal) – veröffentlicht wird erst nach
  bestandenen Tests und bestandener Prüfung.
- `Dockerfile`: neues `ARG OS_UPGRADE` für den Neubau; ohne Angabe passiert nichts, normale Builds bleiben exakt
  reproduzierbar.

Für die App selbst ändert sich nichts.

## 1.9.3 – 2026-10-24

**Sicherheit**
- **Schwachstellenprüfung mit Trivy:** Der GitHub-Workflow prüft jedes Image vor dem Veröffentlichen. Bekannte
  Schwachstellen der Stufe HIGH oder CRITICAL mit verfügbarer Korrektur (Debian- und Python-Pakete) stoppen das
  Release – `latest` bleibt dann auf der bisherigen Version. Veröffentlicht wird genau das geprüfte Image.
- Jeden Montag wird `latest` erneut geprüft; bei einem Fund schlägt der Lauf fehl und GitHub schickt eine E-Mail.
- Bewusste Ausnahmen mit Begründung und Ablaufdatum in `.trivyignore`. Trivy wird in einer Version nach dem
  Lieferketten-Vorfall vom März 2026 verwendet und wie alle Actions über den Commit-SHA festgelegt.
- Der Workflow ist in Bauen → Prüfen → Veröffentlichen aufgeteilt: Der Prüfer läuft ohne Schreibrechte und ohne
  Anmeldung an der Registry, Schreibrechte hat nur der letzte Schritt. Ein manipulierter Prüfer könnte so weder
  Images noch Tags verändern.
- **pip ist nicht mehr im Image:** Es wird nur zum Installieren gebraucht und danach entfernt. pip bringt eigene,
  ältere Kopien von urllib3, msgpack und setuptools mit, in denen die erste Prüfung vier HIGH-Lücken gefunden hat
  (AssetLotse selbst nutzt urllib3 2.8.0 und hatte keine).

Für die App selbst ändert sich nichts.

## 1.9.2 – 2026-10-24

Code-Audit: Korrekturen in App, Oberfläche und Update-Weg. Keine neuen Funktionen, keine Datenbankänderung.

**Sicherheit**
- Löschen einer Gruppe, deren Verwalter sonst ohne Gruppe dastünde, wird abgelehnt – ein Verwalter ohne Gruppe
  verwaltet den gesamten Bestand. Beim Speichern eines Verwalters ohne Gruppe gibt es einen Hinweis.
- Grenze für Registrierungsversuche gilt jetzt für alle Prozesse gemeinsam (vorher pro Worker).
- Sicherungs-Download wird nicht mehr zwischengespeichert.

**Korrigiert**
- AD: Ist der Verzeichnisserver kurz nicht erreichbar oder ausgelastet, gilt das als Fehler statt als „Benutzer
  unbekannt“ bzw. „keine Gruppen“ – niemand verliert dadurch Gruppen oder wird gesperrt.
- Erste Anmeldung zweier Sitzungen gleichzeitig führt nicht mehr zu einem Fehler 500.
- Benutzer mit offenen Ausleihen werden nie gelöscht (auch nicht durch Aufräumen oder Aufbewahrungsfristen).
- Doppelte Rückgabe (zwei Klicks, zwei Geräte) wird nur einmal gebucht.
- Ausschalten des AD-Abgleichs entsperrt keine ausgeschiedenen Konten mehr.
- Erinnerungsmails: Ein Fehler bei einer Person stoppt den Lauf nicht mehr; fehlgeschlagene Läufe werden bis zu
  dreimal am selben Tag nachgeholt. Die Hintergrundaufgaben laufen unabhängig voneinander.
- Aufbewahrung läuft bei aktiver Sicherung erst nach der täglichen Sicherung.
- Theke: Inventarnummern mit Umlauten werden unabhängig von Groß-/Kleinschreibung gefunden; das Häkchen
  „automatisch“ bleibt gemerkt.
- Gleichzeitiges Bearbeiten eines Gegenstands und Konflikte beim Speichern ergeben eine Meldung statt Fehler 500.
- DNS-Fehler bei Netzbereichen mit Hostnamen werden zwischengespeichert (keine Wartezeit bei jedem Aufruf).
- Schnellere Benutzerliste und AD-Abgleich bei vielen Konten.
- Sicherungsmeldungen und -mails in der englischen Oberfläche auf Englisch, Größen mit passendem Dezimalzeichen.
- Oberfläche: kein seitliches Scrollen auf dem Handy, Menü per Escape schließbar und für Screenreader korrekt,
  alle Formularfelder mit Beschriftung verknüpft, besserer Kontrast, Fehlermeldungen bleiben stehen, abgelaufene
  Sitzung wird gemeldet.

**Update-Weg**
- `update.sh`: Abbruch (Strg+C) mitten im Austausch wird sauber zurückgerollt; zwei gleichzeitige Updates werden
  verhindert; `latest` wird bei einem abgelehnten Image zurückgesetzt; kein stilles Downgrade; der
  Wiederherstellungsordner bleibt bei einem Fehler erhalten; Registry-Adressen mit Port gehen.
- Healthcheck und Stoppzeit des Containers passen zur Startzeit; der Workflow setzt Minor-Tag und `latest` nur für
  die höchste Version.

## 1.9.1 – 2026-10-24

**Geändert**
- **Neue Versionen ohne Tag:** Der GitHub-Workflow baut jetzt bei jedem Push auf `master` mit einer neuen
  Versionsnummer selbst das Image, setzt `latest` (wenn es die höchste Version ist) und legt den Tag `v<Version>` an.
  Pushes ohne neue Versionsnummer bauen nichts. Einen Tag von Hand zu setzen geht weiterhin.
- Server, die `latest` folgen (`./update.sh --pull` oder ein Update-Tool wie Dockhand), bekommen neue Versionen damit
  ohne weiteren Schritt – nach dem Hochladen dauert es ein, zwei Minuten, bis das Image bereitsteht.

## 1.9.0 – 2026-10-24

**Neu**
- **Neuigkeiten in der App:** Ein kleiner Punkt neben der Versionsnummer unten links zeigt, wenn es nach einem Update
  etwas Neues gibt; er führt zur Seite *Neuigkeiten* (auch über *Über dieses Programm*). Kurze Hinweise zu neuen
  Funktionen, Sicherheitsupdates und Korrekturen, auf Deutsch und Englisch, je nach Rolle gefiltert. Nach dem Lesen
  verschwindet der Punkt bis zum nächsten Update. Inhalt: `assetlotse/news.json`.

## 1.8.2 – 2026-10-23

**Neu**
- **Englisches Handbuch** `docs/Manual-AssetLotse.pdf` mit Bildschirmfotos der englischen Oberfläche. Die Hilfe
  verlinkt in der englischen Oberfläche jetzt dieses statt des deutschen.

## 1.8.1 – 2026-10-23

Ersetzt 1.8.0 (dort gab es einen eigenen Dockhand-Modus – entfernt, jetzt geht es mit jedem Tool).

**Neu: Updates auch mit anderen Tools**
- Nach `./update.sh --pull` folgt der Container dem Image-Tag **`latest`**. Neue Versionen lassen sich damit wie
  bisher mit `./update.sh --pull` einspielen – oder mit einem beliebigen Tool, das Container aktualisiert. Es gibt
  keine Einstellungen mehr für ein bestimmtes Tool; das Dockhand-Label in der `docker-compose.yml` ist entfernt.
- `./update.sh --pull --version X.Y.Z` legt eine Version fest (Tools lassen sie dann in Ruhe), das nächste
  `./update.sh --pull` folgt wieder der neuesten.
- **Sicherung beim ersten Start einer neuen Version:** AssetLotse merkt sich seine Version in der Datenbank. Läuft
  eine neue Version zum ersten Mal, legt die App **vor** jeder Änderung an der Datenbank eine Sicherung an
  (`…-vor-update.db`, nur SQLite) – egal, wie das Update kam. Startet eine ältere Version auf einer neueren
  Datenbank, steht eine Warnung im Protokoll.

**Hinweis:** Ein Update-Tool tauscht nur den Container aus, nicht `update.sh` und `docker-compose.yml` auf dem
Server. Wenn sich diese ändern, steht es hier im CHANGELOG – dann einmal `./update.sh --pull`.

## 1.7.0 – 2026-10-23

**Neu: fertige Images von GitHub**
- **GitHub Actions** (`.github/workflows/docker.yml`): Ein Versions-Tag wie `v1.7.0` baut das Image und legt es unter
  `ghcr.io/soleil0815/assetlotse` ab (Tags `1.7.0`, `1.7`, `latest`). Der Lauf bricht ab, wenn Tag und Version in
  `assetlotse/__init__.py` nicht zusammenpassen. Von Hand gestartet entsteht nur ein Test-Image `test-<commit>`.
- **`./update.sh --pull`** lädt das Image statt zu bauen: Sicherung → Image laden → `update.sh`, `docker-compose.yml`
  & Co. aus dem Image übernehmen → Neustart → alte Images aufräumen. `--version X.Y.Z` für eine bestimmte Version
  (auch zum Zurückgehen), `--image` für eine andere Adresse. Einmalig auf dem Server: `docker login ghcr.io`.
- **Beide Wege bleiben:** Ein Zip wird weiter lokal gebaut, dann als `assetlotse:X.Y.Z` – nie unter dem
  GitHub-Namen, damit Dockhand es nicht ersetzt. Welches Image läuft, verwaltet `update.sh` in der `.env`
  (`ASSETLOTSE_IMAGE`, `ASSETLOTSE_TAG`, `ASSETLOTSE_REGISTRY_IMAGE`). Aufräumen behält immer das laufende und das
  zuvor laufende Image, egal ob lokal gebaut oder von GitHub.
- **Dockhand:** weiterhin kein automatisches Update (`dockhand.update=false`), weil nur `update.sh` vorher sichert.
- **Vertrauen:** `--pull` prüft, dass das Image aus dem passenden GitHub-Repository gebaut wurde, zeigt Version und
  Digest und verlangt bei einer neuen Image-Adresse immer ein „ja“ (auch mit `-y`). Die GitHub-Actions sind auf
  feste Commits festgelegt. `latest` bekommt nur die höchste Version – ein Nachtrag für eine ältere Reihe
  verschiebt es nicht.
- Das Image enthält jetzt auch die Installationsdateien (`/app/update.sh` usw.). `.gitignore` ergänzt (Pakete,
  Archiv), das Zip enthält keine `.env` mehr (`update.sh` legt sie bei Bedarf an).

**Erster Umstieg:** Dieses Zip wie gewohnt mit `./update.sh assetlotse-1.7.0.zip` einspielen (das alte Skript kennt
`--pull` noch nicht). Danach den Code mit Tag `v1.7.0` zu GitHub schicken, auf dem Server einmal
`docker login ghcr.io -u soleil0815` und `./update.sh --pull`.

## 1.6.3 – 2026-10-23

**Geändert**
- **Installationsdateien auf Englisch:** `update.sh`, `docker-entrypoint.sh`, `docker-compose.yml`, `.env.example`,
  `Dockerfile`, `gunicorn.conf.py`, `wsgi.py` und `requirements*` – Kommentare und alle Meldungen. Die Rückfrage von
  `update.sh` lautet jetzt `[Y/n]` (ein „j“ wird weiterhin angenommen). Oberfläche, Hilfe und Handbuch bleiben
  zweisprachig bzw. deutsch.
- Das Paketarchiv heißt jetzt **`packages/`** statt `pakete/`. Ein vorhandener Ordner `pakete/` wird beim
  nächsten Update bzw. `./update.sh --cleanup` automatisch umbenannt. Zurück zur vorigen Version:
  `./update.sh packages/assetlotse-ALT.zip`.

## 1.6.2 – 2026-10-23

**Hinweise zum Update von 1.6.1:**
- Das alte `update.sh` kennt das Aufräumen noch nicht. Nach diesem Update einmal `./update.sh --cleanup` ausführen –
  ab dann räumt jedes Update selbst auf.
- Ist die Selbstregistrierung mit E-Mail-Bestätigung an, muss unter *Administration → E-Mail* die **Adresse von
  AssetLotse** eingetragen sein (so, wie sie von außen erreichbar ist) – sonst ist die Registrierung gesperrt.
- `./data` ist jetzt nur noch für den App-Benutzer lesbar. Kopiert ein Skript auf dem Server die Sicherungen, muss
  es als root oder als `PUID` laufen.

**Neu: Aufräumen beim Update**
- Nach einem erfolgreichen Update kommt das Zip ins Archiv `./pakete` (aktuelles und voriges Paket bleiben für den
  Rückweg, ältere werden gelöscht). Lose `assetlotse-*.zip` im Programmordner wandern ins Archiv, Reste
  abgebrochener Updates und ältere AssetLotse-Images werden entfernt; das Image der vorigen Version bleibt.
  Andere Docker-Images auf dem Server werden nie angefasst.
- Neue Optionen: `--no-cleanup` (nicht aufräumen), `--cleanup` (nur aufräumen). `--prune` löscht zusätzlich das
  vorige Image. Zurück zur vorigen Version: `./update.sh pakete/assetlotse-ALT.zip`.
- Nach dem Austausch der Dateien übernimmt das **neue** `update.sh` Bauen, Starten und Aufräumen – Verbesserungen
  am Skript wirken so schon beim Update selbst.

**Sicherheit** (Ergebnis einer vollständigen Prüfung – nichts Kritisches, folgende Punkte behoben)
- **Links in E-Mails** (Bestätigung, Admin-Hinweis, Freigabe) werden nicht mehr aus der Host-Angabe der Anfrage
  gebaut – ein Fremder konnte sonst Mails mit Links auf seine eigene Seite auslösen. Ohne eingetragene Adresse von
  AssetLotse ist die Registrierung mit E-Mail-Bestätigung gesperrt; beim Einschalten der Registrierung übernimmt
  AssetLotse die Adresse, unter der der Admin die Seite aufruft.
- **Vertrauliche Angaben (BitLocker-Schlüssel) für Verwalter** – anzeigen, ändern, löschen und für die ausleihende
  Person freigeben – nur noch mit bestätigtem zweiten Faktor in der laufenden Sitzung, auch wenn im internen Netz das Passwort zum Anmelden reicht (wie schon beim eigenen Gerät).
- **Keine Zwischenspeicherung angemeldeter Seiten** in Proxys/CDNs oder auf der Platte des Browsers (u. a.
  CSV-Export, 2FA-Einrichtung, Wiederherstellungscodes); Fotos und Handbuch nur noch „privat“ zwischenspeicherbar.
- **Registrierung:** höchstens 30 Versuche pro Stunde und IP (bremst das Abfragen vorhandener Benutzernamen und
  Adressen).
- **Ersteinrichtung** (`/setup`) nur noch aus dem internen Netz oder über localhost – sonst `create-admin`.
- **Loopback zählt nicht mehr automatisch als internes Netz** (Tunnel und Weiterleitungen ohne Besucher-Adresse
  hätten sonst die 2FA-Ausnahme fürs interne Netz bekommen). Bei Bedarf `127.0.0.0/8` eintragen.
- **Erster zweiter Faktor** meldet alle anderen Geräte ab; **Passwort ändern** ist gegen Raten gebremst.
- **Standorte:** Gruppen-Verwalter sehen keine frei eingetragenen Orte anderer Gruppen mehr und können sie auch
  nicht erraten oder als festen Standort übernehmen.
- **Sicherungen herunterladen:** wird nie zwischengespeichert.
- **Bilder:** nur noch PNG/JPEG/WebP/GIF werden überhaupt eingelesen; Fotos werden speichersparend dekodiert.
  **Pillow 13.0.0** (behebt u. a. Speicher- und Rechenzeit-Angriffe über präparierte Bilder), Werkzeug 3.1.9,
  certifi 2026.10.19.
- **Datenbank:** Gelöschtes wird überschrieben (`secure_delete`), damit z. B. entfernte Schlüssel nicht im freien
  Speicher der SQLite-Datei liegen bleiben (ältere Sicherungen enthalten sie weiterhin).
- **Container:** `./data` nur für den App-Benutzer lesbar, neue Dateien mit 0600; `PUID`/`PGID` 0 oder keine Zahl
  werden abgelehnt; `no-new-privileges`.
- **update.sh:** arbeitet mit einer eigenen Kopie des Pakets, zeigt die SHA-256-Prüfsumme und vergleicht sie mit
  `PAKET.zip.sha256`, falls vorhanden; lehnt absolute Pfade, `..`, symbolische Links und Sonderdateien ab;
  übernimmt keine Schreibrechte aus dem Zip; die Wiederherstellung bei einem Fehler fasst nur nachweislich
  ausgetauschte Einträge an; eine neu angelegte `.env` ist nur für den Besitzer lesbar.
- Warnung beim Start, wenn `ASSETLOTSE_SECRET_KEY` kürzer als 32 Zeichen ist.

## 1.6.1 – 2026-10-23

**Neu**
- **Handbuch als PDF** (`docs/Handbuch-AssetLotse.pdf`, 30 Seiten mit Bildschirmfotos): Teil A für alle, Teil B für
  Verwalter, Teil C für Administratoren, dazu Fehlerbehebung, Notfallbefehle und alle `.env`-Einstellungen. Die Hilfe
  verlinkt es oben; die englische Oberfläche bekommt das deutsche Handbuch, solange es kein englisches gibt.

## 1.6.0 – 2026-10-23

**Neu**
- **AD-Abgleich im Hintergrund** (mit Dienstkonto; Standard an, stündlich – wählbar bis täglich; „Jetzt abgleichen“):
  - **Schnelle Personensuche an der Theke:** Personen ohne AssetLotse-Konto, die sich nach den AD-Regeln anmelden
    dürften, werden mit Benutzername, Name und E-Mail zwischengespeichert. Die Theke findet sie sofort, schon beim
    Tippen (Vorschläge mit „AD – noch kein Konto“, ein Klick übernimmt das Konto). Findet der Zwischenspeicher
    niemanden, wird wie bisher live im AD gesucht. „Konto übernehmen“ prüft weiterhin direkt im AD.
  - **Vorhandene AD-Konten werden aktuell gehalten** – auch ohne Anmeldung: Name, E-Mail, AssetLotse-Gruppen (bei
    „Gruppen automatisch zuordnen“) und Umbenennungen im AD (erkannt an der objectGUID). Die Rolle nur bei Konten, die
    sich schon selbst angemeldet haben.
  - **Deaktivierte, abgelaufene, gelöschte oder nicht mehr berechtigte Personen** fliegen aus dem Zwischenspeicher;
    ihre Konten werden deaktiviert (Ausleihen bleiben) und automatisch wieder aktiv, wenn die Person zurückkommt.
    Beim ersten Abgleich und bei auffälligen Mengen (mehr als 20 %, alle Konten oder der letzte Administrator) wird
    niemand automatisch deaktiviert – die AD-Seite listet die Namen zur Bestätigung. Vom Abgleich gesperrte Konten
    werden auch bei der nächsten Anmeldung sofort wieder aktiv, wenn das AD sie freigibt.
  - Umbenennungen werden auch bei Login und Kontoübernahme erkannt (kein zweites Konto); ein im AD neu vergebener
    Benutzername übernimmt nie das Konto einer früheren Person. Abgebrochene Leseabfragen gelten nie als vollständig.
  - Schutz der Administratoren: AD-Admins werden nie automatisch deaktiviert; keine Herabstufung, wenn danach kein
    Administrator bliebe oder viele Konten auf einmal Rechte verlören; ein AD-Admin kann keine AD-Einstellungen
    speichern, die ihn selbst aussperren würden.
  - AD-Konten lassen sich unter „Benutzer“ umbenennen (z. B. altes Konto beiseiteschieben, wenn ein Name neu vergeben
    wurde). Gesperrte Altkonten ohne AD-Kennung werden nie automatisch wieder aktiv.
  - Effizient: zwei große Abfragen (alle Gruppen, alle Personen) statt einer pro Person.
- Personenfeld an der Ausgabetheke mit Vorschlägen beim Tippen (ersetzt die alte Auswahlliste).

## 1.5.0 – 2026-10-22

**Neu**
- **Vorschläge beim Tippen in der Suche oben:** passende Gegenstände mit Inventar-/Seriennummer, Kategorie und
  „x von y frei“; Klick (oder ↓ und Enter) öffnet das Gerät, „Alle Treffer anzeigen“ bzw. Enter die ganze Liste.
- **Vorschläge beim Tippen an der Ausgabetheke:** z. B. „Festplatte“ zeigt sofort die passenden Geräte, die die
  gewählte Person bekommen darf – ein Klick gibt aus. Barcodescanner funktionieren wie bisher.
- **Inventarnummer beim Anlegen:** Unter dem Feld steht, ob die Nummer frei oder schon vergeben ist (mit Link),
  welche ähnlichen Nummern es gibt und die nächste freie (z. B. IT-0044) zum Übernehmen. Gleiches für die
  Seriennummer (frei/vergeben).

**Geändert**
- **Doppelte Inventar- und Seriennummern werden abgelehnt** (bisher nur eine Warnung), unabhängig von Groß- und
  Kleinschreibung (auch Umlaute) und Leerzeichen am Rand; die Eingaben bleiben im Formular stehen. Zusätzlich
  verhindert ein eindeutiger Index in der Datenbank (SQLite, PostgreSQL, MariaDB/MySQL), dass zwei Personen
  gleichzeitig dieselbe Nummer vergeben. Doppelte Nummern aus älteren Versionen blockieren das Bearbeiten nicht
  (Warnung); solange es sie gibt, entfällt nur der Datenbank-Index für diese Nummernart (Hinweis im Protokoll beim
  Start). Neue Spalten werden beim ersten Start automatisch ergänzt.

## 1.4.1 – 2026-10-22

**Geändert**
- **Keine Mindestversion für Docker Compose mehr** (bisher 2.24): Die `docker-compose.yml` nutzt nur noch Angaben, die
  jede Compose-Version versteht – getestet mit 1.29, 2.20 und 5.6. `update.sh` arbeitet mit dem Plugin
  `docker compose` und mit dem älteren eigenständigen `docker-compose` (wählbar mit `ASSETLOTSE_COMPOSE`).
- Im Paket liegt eine leere `.env`, damit `docker compose up` sofort läuft; fehlt sie, legt `update.sh` sie an.
  Eine vorhandene `.env` wird wie bisher nie überschrieben.

## 1.4.0 – 2026-10-22

**Geändert**
- **Mitarbeiter sehen standardmäßig keinen Bestand mehr**, nur noch ihre eigenen Gegenstände unter „Meine
  Gegenstände“ (samt Detailseite, solange ihnen der Gegenstand ausgegeben ist). Menüpunkt „Bestand“, Suchfeld und
  „Neu im Bestand“ entfallen für sie. Umstellbar unter *Einstellungen → Sichtbarkeit des Bestands* (neu:
  „nur eigene Gegenstände“; weiterhin „eigene Gruppen“ und „gesamter Bestand“). Verwalter und Administratoren sehen
  den Bestand wie bisher. Die Selbstausleihe wirkt nur, wenn Mitarbeiter den Bestand sehen.
  Wer die Sichtbarkeit früher schon einmal gespeichert hat, behält seine Einstellung.

## 1.3.0 – 2026-10-22

**Neu**
- **Vertrauliche Angaben für die ausleihende Person:** Pro Gegenstand lässt sich freigeben, dass die Person, der er
  gerade ausgegeben ist, die vertraulichen Angaben selbst ansehen darf – z. B. den BitLocker-Schlüssel ihrer
  Festplatte, wenn sie ihn vergessen hat. Standard: aus. Nur solange die Ausgabe läuft (mit der Rückgabe ist es
  vorbei), nur nach erneuter Eingabe des eigenen Passworts (AD-Konten werden gegen das AD geprüft, Fehlversuche
  zählen zur Anmeldesperre). Jeder Abruf steht als „angezeigt (Ausleiher)“ im Protokoll, ebenso das Ein- und
  Ausschalten der Freigabe. Andere Mitarbeiter sehen weiterhin nichts; schalten darf nur, wer die Angaben selbst
  sehen darf. Die Person sieht ihren Gegenstand auch dann, wenn er zu einer fremden Gruppe gehört.
  Damit sich niemand den Schlüssel „erleihen“ kann: nur bei Einzelstücken und nur, wenn die Ausgabe von jemandem
  stammt, der die Angaben selbst sehen darf (nicht bei „Selbst ausleihen“). Wer sich ohne zweiten Faktor angemeldet
  hat, bestätigt ihn vorher; Fehlversuche zählen auch über mehrere Adressen.

## 1.2.1 – 2026-10-22

**Geändert**
- **Standardport ist jetzt 9343** (bisher 9300). Wer 9300 behalten will: `ASSETLOTSE_PORT=9300` in die `.env`.
- **Port frei wählbar mit einer Einstellung:** `ASSETLOTSE_PORT` in der `.env` gilt jetzt außen auf dem Server *und*
  innen im Container (gunicorn, Healthcheck). Bisher lauschte der Container fest auf 9300 – wer die
  `docker-compose.yml` angepasst hatte, landete schnell ins Leere. Erlaubt sind 1024–65535; ein ungültiger Wert
  wird beim Start und von `update.sh` mit klarer Meldung abgelehnt.
- `update.sh` zeigt am Ende die richtige Adresse und warnt, wenn `ASSETLOTSE_BIND=127.0.0.1` gesetzt ist
  (dann nur auf dem Server selbst erreichbar) sowie beim Update von älteren Versionen über den neuen Standardport.

## 1.2.0 – 2026-10-22

**Neu**
- **Personen aus dem Active Directory an der Ausgabetheke:** Die Personensuche findet auch Mitarbeitende, die sich
  noch nie angemeldet haben – aber nur, wer nach den AD-Regeln (Basis-DN, Benutzerfilter, OUs/Gruppen für
  Mitarbeiter, Verwalter, Administratoren) Zugang hat; im AD deaktivierte Konten bleiben unsichtbar.
  Ebenso ausgeblendet: abgelaufene Konten, Computer- und Gruppenobjekte. „Konto übernehmen“ prüft die Person
  serverseitig erneut im AD und legt das Konto an – zunächst als Mitarbeiter, die AD-Rolle gilt ab der ersten
  eigenen Anmeldung. Braucht das Dienstkonto; Suchen werden protokolliert.
- **Bestandssuche in der Ausgabe:** Suchtext und Kategorie, standardmäßig nur verfügbare Gegenstände; Ausgabe direkt
  aus der Trefferliste, die Suche bleibt danach stehen.
- Passt die Personensuche auf mehrere Konten, erscheint eine Auswahlliste (statt „nicht eindeutig“).

## 1.1.0 – 2026-10-22

**Neu**
- **Etiketten mit Barcode** als PDF im Format **62 × 29 mm** (z. B. Brother QL mit DK-11209 oder 62-mm-Endlosband):
  Bezeichnung, Kategorie/Modell, Seriennummer, Barcode (Code 128) mit der Inventarnummer und Name der Einrichtung.
  Einzeln auf der Detailseite („Etikett“) oder für die ganze aktuelle Auswahl im Bestand („Etiketten“, höchstens 500).
  Ohne Inventarnummer (oder mit Umlauten darin) wird `#<interne Nummer>` codiert – die Ausgabetheke findet beides.
- **Startseite mit Kennzahlen** für Verwalter: Bestand, Verfügbar, Ausgegeben (mit Auslastung), Überfällig,
  vertrauliche Angaben; Tabelle „Bestand nach Kategorie“ mit Auslastungsbalken, „Letzte Bewegungen“, „Bald zurück“.

**Geändert**
- **Neues Erscheinungsbild** im Stil einer Asset-Management-Software: Schiefergrau und Petrol, eine sachliche
  Schrift (Inter) für alles, dichtere Tabellen, kleinere Radien. Die Zierschrift Fraunces ist entfallen.
- **Bestand** öffnet standardmäßig als **Tabelle** (Kacheln weiter umschaltbar) – mit Spalte „Ausgegeben an“.
- Standardfarben im Erscheinungsbild, Gruppenfarbe, Favicon und E-Mails passend zum neuen Design.

## 1.0.0 – 2026-10-22

Erste Version: Verwaltung und Ausgabe von Arbeitsmitteln (Laptops, USB-Sticks, Headsets, Schlüssel, Kabel …).

- **Gegenstände** mit Bezeichnung, Kategorie (frei, mit Vorschlägen), Hersteller, Modell, Seriennummer,
  Inventarnummer, Beschreibung/Zubehör, Standort, Gruppe, Anzahl und Foto.
- **Vertrauliche Angaben** je Gegenstand (z. B. BitLocker-Wiederherstellungsschlüssel): verschlüsselt mit der
  `secret_key`, sichtbar nur für Administratoren und Verwalter der Gruppe des Gegenstands über „Anzeigen“; jedes
  Anzeigen/Ändern/Löschen wird protokolliert, nie im Export. Das Zugriffsprotokoll wird nach der Aufbewahrungsfrist gelöscht.
- **Ausgabe** mit Rückgabedatum oder **unbefristet** (Standard). Unbefristete Ausgaben sind nie überfällig und
  bekommen keine Erinnerungen. **Ausgabetheke** für Barcodescanner (Inventar-, Seriennummer, Bezeichnung, `#Nummer`).
- „Ähnlichen anlegen“ und „Speichern & nächsten“; Warnung bei doppelter Inventar- oder Seriennummer.
- Filter nach Kategorie, Gruppe, Standort und Status; CSV-Export mit Spalte „Ausgegeben an“.
- Rollen Mitarbeiter, Verwalter, Administrator; Gruppen (z. B. Abteilungen); Standorte.
- Active Directory/LDAP, Zwei-Faktor-Anmeldung (TOTP, Sicherheitsschlüssel), E-Mail-Benachrichtigungen,
  Selbstregistrierung, eigenes Erscheinungsbild, automatische Datensicherung, Aufbewahrungsfristen (DSGVO),
  Deutsch und Englisch, Docker mit `update.sh`.
