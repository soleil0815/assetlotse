"""Neuigkeiten in der App: kurze Hinweise zu neuen Funktionen, Sicherheitsupdates und Korrekturen.

Die Einträge stehen in news.json (mit dem Programm ausgeliefert). Jede Person sieht nur, was sie betrifft
(audience), und ein dezenter Punkt bei der Versionsnummer zeigt Ungelesenes an. Gelesen gilt alles bis zur
Version, die beim letzten Öffnen der Seite lief (users.news_seen)."""
from __future__ import annotations

import functools
import json
import logging
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)
KINDS = ("feature", "security", "fix")
_RANK = {"all": 0, "manager": 1, "admin": 2}


def version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in (v or "").split("."))


def _load_raw() -> dict:
    return json.loads(Path(__file__).with_name("news.json").read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def _entries() -> tuple:
    try:
        data = _load_raw()
    except (OSError, ValueError) as exc:
        log.warning("Neuigkeiten nicht lesbar: %s", exc)
        return ()
    current = version_tuple(__version__)
    out = []
    for e in data.get("entries", []):
        if (e.get("kind") in KINDS and e.get("audience") in _RANK and e.get("de", {}).get("title")
                and version_tuple(e.get("version", "")) <= current):       # nichts aus der Zukunft anzeigen
            out.append(e)
    out.sort(key=lambda e: version_tuple(e["version"]), reverse=True)
    return tuple(out)


def _user_rank(user) -> int:
    return 2 if user.is_admin else (1 if user.is_manager else 0)


def for_user(user) -> list[dict]:
    rank = _user_rank(user)
    return [e for e in _entries() if _RANK[e["audience"]] <= rank]


def _baseline(user) -> tuple:
    """Bis zu welcher Version gilt alles als gelesen? Ohne Eintrag (neues Konto, erstes Update mit dieser
    Funktion): alles vor der laufenden Version – dann ist nur Neues dieser Version ungelesen."""
    seen = (user.news_seen or "").strip()
    if seen:
        return version_tuple(seen)
    older = [version_tuple(e["version"]) for e in _entries() if version_tuple(e["version"]) < version_tuple(__version__)]
    return max(older) if older else ()


def unread(user) -> list[dict]:
    base = _baseline(user)
    return [e for e in for_user(user) if version_tuple(e["version"]) > base]


def mark_read(user) -> bool:
    """Alles bis zur laufenden Version als gelesen merken. True, wenn sich etwas geändert hat."""
    if (user.news_seen or "") == __version__:
        return False
    user.news_seen = __version__
    return True


def text(entry: dict, lang: str) -> dict:
    t = entry.get(lang) or {}
    return {"title": t.get("title") or entry["de"]["title"], "text": t.get("text") or entry["de"].get("text", "")}
