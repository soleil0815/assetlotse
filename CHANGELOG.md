# Änderungen an AssetLotse

Die aktuelle Version steht nach der Anmeldung unten links in der Seitenleiste.

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
- Ist die Selbstregistrierung mit E-Mail-Bestätigung an, muss unter *Verwaltung → E-Mail* die **Adresse von
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
