import logging
import sys

import pytest

import logging_setup


@pytest.fixture(autouse=True)
def _clean():
    logging_setup.teardown_logging()
    yield
    logging_setup.teardown_logging()


def _flush():
    for h in logging.getLogger().handlers:
        h.flush()


def test_creates_file_and_logs(tmp_path):
    d = tmp_path / "logs"
    logging_setup.setup_logging(d)
    logging.getLogger("x").info("hello-log")
    _flush()
    text = (d / "bot.log").read_text(encoding="utf-8")
    assert "hello-log" in text and "INFO" in text and "x:" in text


def test_idempotent(tmp_path):
    logging_setup.setup_logging(tmp_path)
    n = len(logging.getLogger().handlers)
    logging_setup.setup_logging(tmp_path)
    assert len(logging.getLogger().handlers) == n


def test_print_is_teed_and_noisy_quieted(tmp_path):
    logging_setup.setup_logging(tmp_path)
    print("printed-line")
    _flush()
    assert "printed-line" in (tmp_path / "bot.log").read_text(encoding="utf-8")
    assert logging.getLogger("httpx").level == logging.WARNING


def test_none_streams(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    logging_setup.setup_logging(tmp_path)
    print("no-console")
    _flush()
    assert "no-console" in (tmp_path / "bot.log").read_text(encoding="utf-8")
    logging_setup.teardown_logging()
