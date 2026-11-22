"""Minimal chat client for an OpenAI-compatible LLM API (DeepSeek by default).

Used offline by question generation and review, never by the daemon's hot path.
`python -m ltp.llm [model]` reads a prompt on stdin and prints the reply, so it can serve
as a `--cmd` reviewer for scripts/review_bank.py.
"""
from __future__ import annotations

import os
import sys

import httpx

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
KEY_ENV = "DEEPSEEK_API_KEY"


class LLMError(RuntimeError):
    pass


def chat(prompt: str, *, model: str = DEFAULT_MODEL, api_key: str | None = None,
         base_url: str | None = None, timeout: float = 600.0, max_tokens: int = 8000,
         client: httpx.Client | None = None) -> str:
    key = api_key or os.environ.get(KEY_ENV)
    if not key:
        raise LLMError(f"{KEY_ENV} is not set")
    url = (base_url or os.environ.get("LTP_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0.7,
            "messages": [{"role": "user", "content": prompt}]}
    own = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(f"{url}/chat/completions", json=body,
                         headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        raise LLMError(f"request failed: {exc}") from exc
    finally:
        if own:
            http.close()
    if resp.status_code != 200:
        raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        text = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected reply shape: {exc}") from exc
    if not isinstance(text, str) or not text.strip():
        raise LLMError("empty reply")
    return text


def main(argv: list[str]) -> int:
    model = argv[0] if argv else os.environ.get("LTP_LLM_MODEL", DEFAULT_MODEL)
    prompt = sys.stdin.buffer.read().decode("utf-8")
    try:
        reply = chat(prompt, model=model)
    except LLMError as exc:
        print(f"llm error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(reply.encode("utf-8"))
    sys.stdout.buffer.write(f"\nmodel: {model}\n".encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
