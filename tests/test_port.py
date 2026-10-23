"""Port-Einstellung: eine Variable (ASSETLOTSE_PORT, Standard 9343) für Host, Container und Healthcheck."""
import pathlib
import re
import runpy

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _conf(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ASSETLOTSE_PORT", raising=False)
    else:
        monkeypatch.setenv("ASSETLOTSE_PORT", value)
    return runpy.run_path(str(ROOT / "gunicorn.conf.py"))


@pytest.mark.parametrize("value,bind", [(None, "0.0.0.0:9343"), ("", "0.0.0.0:9343"), ("9300", "0.0.0.0:9300"),
                                        ("8080", "0.0.0.0:8080"), (' "9500" ', "0.0.0.0:9500")])
def test_gunicorn_bind_follows_env(monkeypatch, value, bind):
    assert _conf(monkeypatch, value)["bind"] == bind


@pytest.mark.parametrize("value", ["abc", "80", "0", "70000", "9343x"])
def test_gunicorn_rejects_bad_port(monkeypatch, value):
    with pytest.raises(SystemExit) as exc:
        _conf(monkeypatch, value)
    assert "ASSETLOTSE_PORT" in str(exc.value.code)


def test_no_hardcoded_port_left():
    docker = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    for text in (docker, compose, (ROOT / "wsgi.py").read_text()):
        assert "9300" not in text
    assert '--bind' not in docker and 'COPY wsgi.py gunicorn.conf.py' in docker
    assert "ASSETLOTSE_PORT=9343" in docker and "os.environ.get('ASSETLOTSE_PORT')" in docker
    assert "${ASSETLOTSE_PORT:-9343}:${ASSETLOTSE_PORT:-9343}" in compose
    assert '"${PORT:-9343}"' in (ROOT / "update.sh").read_text()
    assert re.search(r"ASSETLOTSE_PORT: \$\{ASSETLOTSE_PORT:-9343\}", compose)
