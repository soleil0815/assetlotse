"""Gemeinsame Test-Einstellungen.

Seit 1.4.0 sehen Mitarbeiter standardmäßig keinen Bestand („catalog_visibility = own“). Die älteren Tests prüfen
Bestand, Suche und Selbstausleihe aus Sicht von Mitarbeitern; sie laufen daher mit dem früheren Standard
„groups“. Tests mit der Markierung `catalog_default` (tests/test_catalog_own.py) sehen den echten Standard."""
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "catalog_default: mit dem echten Standard für catalog_visibility testen")


@pytest.fixture(autouse=True)
def _legacy_catalog_default(request, monkeypatch):
    if request.node.get_closest_marker("catalog_default") is None:
        from assetlotse import models
        monkeypatch.setitem(models.DEFAULT_SETTINGS, "catalog_visibility", "groups")
    yield


@pytest.fixture(autouse=True)
def _fast_password_hashes(monkeypatch):
    """Im Betrieb scrypt (absichtlich langsam); in den Tests ein schnelles Verfahren – sonst kostet jedes
    angelegte Konto und jeder Wiederherstellungscode rund 0,1 s. Geprüft wird weiterhin mit check_password_hash."""
    from functools import partial

    from werkzeug.security import generate_password_hash

    from assetlotse import models, twofa
    fast = partial(generate_password_hash, method="pbkdf2:sha256:1000")
    monkeypatch.setattr(models, "generate_password_hash", fast)
    monkeypatch.setattr(twofa, "generate_password_hash", fast)
    yield
