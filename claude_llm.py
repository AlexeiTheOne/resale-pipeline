"""The one place the bot calls Claude.

ask() wraps a single Messages API request with what every caller needs:
  - the monthly budget guard picks the model actually used (llm_usage.choose_model),
  - every response's token usage is logged to db.llm_usage with its cost,
  - a refusal or a max_tokens cut-off raises instead of handing back a partial
    answer that looks complete (a truncated JSON price is worse than no price),
  - the SDK's own retries cover connection errors, 408/409/429 and 5xx; anything
    left (bad request, auth, exhausted retries) propagates as anthropic.APIError.

Thinking is left to the model's default (adaptive): Opus 5.5 and Sonnet 5.5
reject {"type": "disabled"} outright, so cost is controlled with `effort` and
max_tokens instead. max_tokens covers thinking AND the answer, so leave headroom.
Requests are non-streaming, which is fine at the sizes this bot uses (a few
thousand output tokens); a caller needing tens of thousands should stream.
"""
import base64
import json
from pathlib import Path

import anthropic

import config
import llm_usage


class ClaudeError(RuntimeError):
    """Claude answered, but not with something usable."""


class ClaudeRefusal(ClaudeError):
    def __init__(self, model: str, category: str | None, explanation: str | None):
        self.model, self.category, self.explanation = model, category, explanation
        super().__init__(f"{model} declined (category={category}): {explanation or ''}".strip())


class ClaudeTruncated(ClaudeError):
    """Hit max_tokens before finishing: raise max_tokens or ask for less."""


# Opus/Sonnet 5.5 accept server-side refusal fallback; Haiku 5.5 has none (a
# fallback list 400s there), so it is only sent to these families.
_FALLBACK_FAMILIES = ("opus", "sonnet")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    """Lazy so importing this module never needs a key. Credentials resolve the
    SDK's usual way (ANTHROPIC_API_KEY from .env / the environment)."""
    global _client
    if _client is None:
        from dotenv import load_dotenv
        load_dotenv()   # scripts that import this directly never loaded .env
        _client = anthropic.Anthropic(timeout=config.CLAUDE_TIMEOUT_S,
                                      max_retries=config.CLAUDE_MAX_RETRIES)
    return _client


_MEDIA_BY_SUFFIX = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".webp": "image/webp", ".gif": "image/gif"}


def _sniff_media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _image_block(image) -> dict:
    """An image content block from a file path, raw bytes, or (bytes, media_type)."""
    if isinstance(image, tuple):
        data, media_type = image
    elif isinstance(image, (bytes, bytearray)):
        data, media_type = bytes(image), _sniff_media_type(bytes(image))
    else:
        path = Path(image)
        data = path.read_bytes()
        media_type = _MEDIA_BY_SUFFIX.get(path.suffix.lower()) or _sniff_media_type(data)
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.standard_b64encode(data).decode("ascii")}}


def _build_messages(messages, images) -> list[dict]:
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    else:
        messages = [dict(m) for m in messages]
    if images:
        # Images go into the LAST user turn, ahead of its text.
        idx = max(i for i, m in enumerate(messages) if m["role"] == "user")
        content = messages[idx]["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
        messages[idx]["content"] = [_image_block(img) for img in images] + blocks
    return messages


def ask(stage: str, messages, *, model: str | None = None, system: str | None = None,
        max_tokens: int = 4096, effort: str | None = None, images=None,
        output_schema: dict | None = None, item_id: str | None = None,
        client=None, meta: dict | None = None):
    """One Claude request. Returns the answer text, or the parsed object when
    output_schema (a JSON Schema; objects need additionalProperties: false) is given.

    stage    names the call site in /usage and the llm_usage table.
    messages a prompt string, or a list of {"role", "content"} dicts.
    model    the model wanted (default config.CLAUDE_MODEL); the budget guard
             may substitute a cheaper one.
    effort   "low" | "medium" | "high" | "xhigh" | "max", or None for the model's
             default (medium on Opus/Haiku 5.5, high on Sonnet 5.5).
    images   file paths, bytes, or (bytes, media_type), added to the last user turn.
    meta     optional dict filled in with what the call cost (model, cost_usd and
             token counts), even when the answer then raises.

    Raises ClaudeRefusal / ClaudeTruncated / ClaudeError (usage is logged first:
    those tokens were billed), or anthropic.APIError once the SDK's retries are spent.
    """
    chosen = llm_usage.choose_model(model or config.CLAUDE_MODEL)
    params: dict = {"model": chosen, "max_tokens": max_tokens,
                    "messages": _build_messages(messages, images)}
    if system:
        params["system"] = system
    output_config: dict = {}
    if effort:
        output_config["effort"] = effort
    if output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": output_schema}
    if output_config:
        params["output_config"] = output_config
    if config.CLAUDE_REFUSAL_FALLBACK and any(f in chosen for f in _FALLBACK_FAMILIES):
        params["betas"] = [_FALLBACK_BETA]
        params["fallbacks"] = "default"

    client = client or get_client()
    response = client.beta.messages.create(**params)
    cost = llm_usage.log_anthropic(response, stage=stage, item_id=item_id)
    served_by = getattr(response, "model", None) or chosen
    if meta is not None:
        usage = getattr(response, "usage", None)
        meta.update(model=served_by, cost_usd=cost, **{
            k: getattr(usage, k, 0) or 0 for k in (
                "input_tokens", "output_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens")})

    stop = response.stop_reason
    if stop == "refusal":
        details = getattr(response, "stop_details", None)
        raise ClaudeRefusal(served_by, getattr(details, "category", None),
                            getattr(details, "explanation", None))
    if stop == "max_tokens":
        raise ClaudeTruncated(f"{stage}: {served_by} hit max_tokens={max_tokens} "
                              f"(${cost:.4f} spent); raise max_tokens or lower effort")
    if stop not in ("end_turn", "stop_sequence"):
        raise ClaudeError(f"{stage}: unexpected stop_reason={stop!r} from {served_by}")

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if output_schema is None:
        if not text:
            raise ClaudeError(f"{stage}: {served_by} returned no text")
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ClaudeError(f"{stage}: {served_by} returned invalid JSON: {e}: {text[:200]}") from e
