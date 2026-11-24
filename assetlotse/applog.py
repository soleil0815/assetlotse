"""Short program log in the data folder (2.7.0) – the source of the log in the diagnostic package.

AssetLotse logs to the container output (docker compose logs). The program itself cannot read that, so the same
messages also go to <data folder>/logs/assetlotse.log: messages of AssetLotse from INFO up, of all other parts
(libraries, web server) from WARNING up, including tracebacks.

* Small: at most ~1 MB per file, one older file (assetlotse.log.1) – about 2 MB in total, the oldest part is
  dropped automatically. So the log never fills the disk and personal data in it (usernames, addresses) does
  not pile up – like the container output, it stays on the server.
* Several workers write at the same time: every line is appended in one write (O_APPEND) under a file lock,
  the switch to the older file happens under the same lock.
* Never in the way: if the folder cannot be written, the line is only in the container output. Commands run as
  root in the container do not write to it (the files must stay writable for the app's user PUID).
"""
from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

MAX_BYTES = 1_000_000
LOG_NAME = "assetlotse.log"

# Personal values in log messages (usernames, typed search text …) are written to the data log between these two
# invisible characters (Unicode "first strong isolate" / "pop directional isolate"). The diagnostic package then
# replaces everything in between by a placeholder – also values that are not (or no longer) in the database:
# a deleted account, a name typed at the desk, a password typed into the username field.
MARK_START, MARK_END = "\u2068", "\u2069"


class Personal(str):
    """A personal value in a log message: log.info("Sign-in of %s", personal(user.username))."""
    category = "user"


class PersonalText(Personal):
    category = "text"


def personal(value: object, category: str = "user") -> str:
    """Mark a log argument as personal (see MARK_START). Other log outputs show it unchanged."""
    if value is None:
        return "None"
    return (PersonalText if category == "text" else Personal)(str(value))


def log_dir(instance: Path) -> Path:
    return instance / "logs"


def log_files(instance: Path) -> list[Path]:
    """Existing log files, oldest first."""
    d = log_dir(instance)
    return [p for p in (d / (LOG_NAME + ".1"), d / LOG_NAME) if p.is_file()]


class _Filter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or record.name == "assetlotse" or record.name.startswith("assetlotse.")


def _one_line(value: str) -> str:
    """The marked value on one line (the package reads the log line by line), without the marks themselves."""
    return " ".join(value.replace(MARK_START, "").replace(MARK_END, "").split())


class _MarkingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        args = record.args
        if isinstance(args, tuple) and any(isinstance(a, Personal) for a in args):
            record = logging.makeLogRecord(record.__dict__)
            record.args = tuple(f"{MARK_START}{'t:' if isinstance(a, PersonalText) else ''}"
                                f"{_one_line(a)}{MARK_END}"
                                if isinstance(a, Personal) else a for a in args)
        return super().format(record)


class DataLogHandler(logging.Handler):
    def __init__(self, path: Path, max_bytes: int = MAX_BYTES) -> None:
        super().__init__(logging.INFO)
        self.path = path
        self.max_bytes = max_bytes
        self.addFilter(_Filter())
        self.setFormatter(_MarkingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = (self.format(record) + "\n").encode("utf-8", "replace")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(self.path.with_name(self.path.name + ".lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    try:
                        if self.path.stat().st_size + len(data) > self.max_bytes:
                            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                    except FileNotFoundError:
                        pass
                    fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    try:
                        os.write(fd, data)
                    finally:
                        os.close(fd)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001 – logging must never break a request
            pass


def install(instance: Path) -> None:
    """Add the handler to the root logger (once per process and folder)."""
    path = log_dir(instance) / LOG_NAME
    if os.geteuid() == 0 and os.environ.get("ASSETLOTSE_CONTAINER") == "1":
        # a command run as root in the container (docker compose exec without the entrypoint): it must not create
        # root-owned log files the app (PUID) could no longer write to
        return
    root = logging.getLogger()
    if any(isinstance(h, DataLogHandler) and h.path == path for h in root.handlers):
        return
    root.addHandler(DataLogHandler(path))
