"""Model access: Claude (structured output, prompt caching, refusal fallback,
cost log) or a local model through Ollama (free). The engine is chosen in
engine.py; callers only ever see LLM.structured()."""

from __future__ import annotations

from typing import TypeVar

import anthropic
from pydantic import BaseModel

from jobright import store

T = TypeVar("T", bound=BaseModel)

# USD per million tokens: input, output, cache read, cache write (5-minute TTL).
# Source: Anthropic pricing, Oct 2026. Re-check https://www.anthropic.com/pricing
# if the dashboard's spend drifts from the console's.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}

# Haiku 5.5 has no server-side fallback; the others re-run a declined request
# on Anthropic's recommended fallback model inside the same call.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, provider: str | None = None) -> None:
        from jobright import engine
        self.provider = provider or engine.current()
        if self.provider == "free":
            raise LLMError("No AI engine: free mode runs on rules (add a Claude key or start Ollama for AI)")
        if self.provider == "local":
            self.client = None
            return
        try:
            self.client = anthropic.Anthropic(max_retries=4)
        except anthropic.AnthropicError as e:
            raise LLMError("No Anthropic credentials. Add your key in Setup, or use free / local mode") from e

    def structured(self, *, purpose: str, model: str, instructions: str, context: str,
                   prompt: str, schema: type[T], effort: str = "high",
                   max_tokens: int = 16000) -> T:
        """One request whose reply is validated against `schema`.

        `instructions` and `context` (the master profile) are the stable prefix
        and are cached, so repeated calls for different jobs read them at the
        cache price. `prompt` carries the per-job content.
        """
        if self.provider == "local":
            return self._local(purpose, instructions, context, prompt, schema)
        kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            system=[
                {"type": "text", "text": instructions},
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            output_config={"effort": effort},
        )
        if model in _FALLBACK_MODELS:
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        try:
            if max_tokens > 16000:
                # Large outputs (a whole resume, a career plan) must stream, or the SDK
                # refuses them as possibly longer than its 10-minute request limit.
                with self.client.beta.messages.stream(**kwargs) as stream:
                    response = stream.get_final_message()
            else:
                response = self.client.beta.messages.parse(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMError("ANTHROPIC_API_KEY was rejected; check job-right/.env") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"Rate limited on {model} after retries: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code} on {purpose}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Could not reach the Claude API: {e}") from e

        self._record(purpose, model, response.usage)

        if response.stop_reason == "refusal":
            raise LLMError(f"{purpose}: request declined by {model} and its fallback")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{purpose}: reply hit max_tokens={max_tokens}")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(f"{purpose}: reply did not match {schema.__name__}")
        return parsed

    def _local(self, purpose: str, instructions: str, context: str, prompt: str, schema: type[T]) -> T:
        """Ollama with the schema as its output format; one retry with the error."""
        import json
        import urllib.request

        from pydantic import ValidationError

        from jobright import engine
        messages = [{"role": "system", "content": instructions + "\n\n" + context},
                    {"role": "user", "content": prompt + "\n\nReply with JSON only."}]
        model = engine.local_model()
        for attempt in range(2):
            body = json.dumps({"model": model, "messages": messages, "stream": False,
                               "format": schema.model_json_schema(), "options": {"temperature": 0}}).encode()
            req = urllib.request.Request(engine.ollama_url().rstrip("/") + "/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    reply = json.load(r)
            except Exception as e:
                raise LLMError(f"Local model ({model}) did not answer: {e}") from e
            text = (reply.get("message") or {}).get("content", "")
            store.record_usage(purpose, f"ollama:{model}", reply.get("prompt_eval_count") or 0,
                               reply.get("eval_count") or 0, 0, 0, 0.0)
            try:
                return schema.model_validate_json(text)
            except ValidationError as e:
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"That JSON did not match the schema: {str(e)[:800]}. Reply again with valid JSON only."}]
        raise LLMError(f"{purpose}: the local model's reply did not match {schema.__name__}")

    @staticmethod
    def _record(purpose: str, model: str, usage) -> None:
        inp = usage.input_tokens or 0
        out = usage.output_tokens or 0
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        p_in, p_out, p_read, p_write = PRICES.get(model, PRICES["claude-opus-5-5"])
        cost = (inp * p_in + out * p_out + read * p_read + write * p_write) / 1_000_000
        store.record_usage(purpose, model, inp, out, read, write, cost)
