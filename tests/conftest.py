"""Shared test settings.

Since 1.4.0 employees see no inventory by default ("catalog_visibility = own"). The older tests check
inventory, search and self-checkout from the employees' point of view; they therefore run with the former default
"groups". Tests with the marker `catalog_default` (tests/test_catalog_own.py) see the real default."""
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "catalog_default: test with the real default for catalog_visibility")


@pytest.fixture(autouse=True)
def _legacy_catalog_default(request, monkeypatch):
    if request.node.get_closest_marker("catalog_default") is None:
        from assetlotse import models
        monkeypatch.setitem(models.DEFAULT_SETTINGS, "catalog_visibility", "groups")
    yield


@pytest.fixture(autouse=True)
def _fast_password_hashes(monkeypatch):
    """In production scrypt (deliberately slow); in the tests a fast method – otherwise every account and every
    recovery code created costs about 0.1 s. Checking still uses check_password_hash."""
    from functools import partial

    from werkzeug.security import generate_password_hash

    from assetlotse import models, twofa
    fast = partial(generate_password_hash, method="pbkdf2:sha256:1000")
    monkeypatch.setattr(models, "generate_password_hash", fast)
    monkeypatch.setattr(twofa, "generate_password_hash", fast)
    yield
