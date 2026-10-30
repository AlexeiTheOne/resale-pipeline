"""scripts/notify.py: the Telegram alert helper the deploy script calls."""
import importlib.util
from pathlib import Path

import httpx

_spec = importlib.util.spec_from_file_location(
    "notify", Path(__file__).parent.parent / "scripts" / "notify.py")
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_ids_matches_bot_format():
    assert notify.parse_ids("1, 2;3,,x,2") == [1, 2, 3]
    assert notify.parse_ids(None) == []


def test_send_posts_to_every_chat_and_counts_successes():
    seen = []

    def handler(request):
        body = request.read().decode()
        seen.append(body)
        return httpx.Response(200 if '"chat_id":1' in body.replace(" ", "") else 403)

    sent = notify.send("hi", "TOKEN", [1, 2], _client(handler))
    assert sent == 1
    assert len(seen) == 2


def test_send_truncates_long_text():
    bodies = []

    def handler(request):
        bodies.append(request.read().decode())
        return httpx.Response(200)

    notify.send("x" * 5000, "TOKEN", [1], _client(handler))
    assert "truncated" in bodies[0]
    assert len(bodies[0]) < 4200


def test_network_error_never_prints_token(capsys):
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    assert notify.send("hi", "SECRET123", [1], _client(handler)) == 0
    err = capsys.readouterr().err
    assert "SECRET123" not in err
    assert "ConnectError" in err


def test_main_fails_without_config(monkeypatch, capsys):
    monkeypatch.setattr(notify, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    assert notify.main(["hello"]) == 1
    assert notify.main([]) == 1
