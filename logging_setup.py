"""Logging setup: rotating file + console, uncaught-exception capture, print() tee.

Call ``setup_logging(config.LOG_DIR)`` once at startup.
"""
from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5
_NOISY = ("httpx", "httpcore", "urllib3", "telegram", "telegram.ext", "apscheduler")

_TAG = "_ebay_bot_handler"
_state: dict = {}


class _Tee:
    """File-like wrapper: passes writes to the original stream (if any) and
    mirrors complete lines to a file-only logger. Never raises."""

    def __init__(self, original, logger: logging.Logger, level: int):
        self._orig = original
        self._logger = logger
        self._level = level
        self._buf = ""
        self._busy = threading.local()

    def write(self, s):
        if not isinstance(s, str):
            try:
                s = s.decode("utf-8", "replace")
            except Exception:
                return 0
        try:
            if self._orig is not None:
                self._orig.write(s)
        except Exception:
            pass
        if getattr(self._busy, "on", False):  # guard against recursion
            return len(s)
        self._busy.on = True
        try:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self._logger.log(self._level, line.rstrip())
        except Exception:
            pass
        finally:
            self._busy.on = False
        return len(s)

    def flush(self):
        try:
            if self._orig is not None:
                self._orig.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return bool(self._orig and self._orig.isatty())
        except Exception:
            return False

    @property
    def encoding(self):
        return getattr(self._orig, "encoding", None) or "utf-8"

    def __getattr__(self, name):  # fileno, buffer, etc. fall through
        if self._orig is None:
            raise AttributeError(name)
        return getattr(self._orig, name)


def _file_only_logger(name: str, file_handler) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.handlers = [file_handler]
    lg.propagate = False  # keeps teed output out of the console handler
    lg.setLevel(logging.DEBUG)
    return lg


def teardown_logging() -> None:
    """Undo setup_logging (used by tests and re-setup with a new dir)."""
    if not _state:
        return
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _TAG, False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    for name in ("stdout", "stderr"):
        lg = logging.getLogger(f"std.{name}")
        for h in list(lg.handlers):
            lg.removeHandler(h)
    if "stdout" in _state:
        sys.stdout = _state["stdout"]
    if "stderr" in _state:
        sys.stderr = _state["stderr"]
    sys.excepthook = _state["excepthook"]
    threading.excepthook = _state["thread_hook"]
    _state.clear()


def setup_logging(log_dir, level: int = logging.INFO, tee_streams: bool = True) -> Path:
    """Configure logging. Idempotent; returns the log file path.

    tee_streams: also mirror print()/stderr output into the log file (the code
    base uses print() almost exclusively). Safe when stdout/stderr are None.
    """
    log_dir = Path(log_dir)
    log_file = log_dir / "bot.log"
    if _state:
        if _state["log_file"] == log_file:
            return log_file
        teardown_logging()

    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    fh = RotatingFileHandler(log_file, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    setattr(fh, _TAG, True)
    root.addHandler(fh)

    orig_out, orig_err = sys.stdout, sys.stderr
    if orig_err is not None:  # None under pythonw / Task Scheduler
        ch = logging.StreamHandler(orig_err)  # the real stream, not the tee
        ch.setFormatter(fmt)
        setattr(ch, _TAG, True)
        root.addHandler(ch)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    _state.update(log_file=log_file, excepthook=sys.excepthook,
                  thread_hook=threading.excepthook)

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            _state["excepthook"](exc_type, exc, tb)
            return
        logging.getLogger("uncaught").critical("Uncaught exception",
                                               exc_info=(exc_type, exc, tb))

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        logging.getLogger("uncaught").critical(
            "Uncaught exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _hook
    threading.excepthook = _thread_hook

    if tee_streams:
        _state.update(stdout=orig_out, stderr=orig_err)
        sys.stdout = _Tee(orig_out, _file_only_logger("std.stdout", fh), logging.INFO)
        sys.stderr = _Tee(orig_err, _file_only_logger("std.stderr", fh), logging.WARNING)

    logging.getLogger(__name__).info("Logging started -> %s", log_file)
    return log_file
