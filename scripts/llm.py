"""OpenRouter free-model client: JSON-only chat with retry/backoff and model fallback.

Models come from OPENROUTER_MODEL (comma-separated, first = primary) so they can be
swapped in .env / Modal secrets without code changes. A task can put its own models in
front of that list (e.g. OPENROUTER_SCORING_MODEL for score_jobs).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time

import requests

log = logging.getLogger("llm")

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODELS = "nvidia/nemotron-3-super-120b-a12b:free,nvidia/nemotron-3-ultra-550b-a55b:free,google/gemma-4-31b-it:free"
# Per-task primary models (env var -> default), tried before the OPENROUTER_MODEL list.
TASK_MODELS = {"OPENROUTER_SCORING_MODEL": "nvidia/nemotron-3-ultra-550b-a55b:free"}
RETRIES_PER_MODEL = 4      # for 429/5xx; bad-output retries are capped lower (they burn the daily quota)
BAD_OUTPUT_RETRIES = 1
# Reasoning models spend hidden tokens before answering; without headroom they return empty/truncated
# JSON. Low-effort reasoning is kept on purpose: with it disabled, scores came out noticeably inflated.
REASONING_HEADROOM = 6000

calls_made = 0  # read by run_pipeline for the run summary


class LLMError(RuntimeError):
    pass


class DailyLimitReached(LLMError):
    """OpenRouter's free-model daily request cap — retrying won't help until tomorrow."""


def _split(value: str) -> list[str]:
    return [m.strip() for m in value.split(",") if m.strip()]


def models(model_env: str | None = None) -> list[str]:
    general = _split(os.getenv("OPENROUTER_MODEL") or DEFAULT_MODELS)
    if not model_env:
        return general
    preferred = _split(os.getenv(model_env) or TASK_MODELS.get(model_env, ""))
    return preferred + [m for m in general if m not in preferred]


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def _backoff(attempt: int, resp: requests.Response | None) -> float:
    if resp is not None and resp.headers.get("Retry-After", "").isdigit():
        return min(float(resp.headers["Retry-After"]), 90)
    return min(5 * 2 ** attempt, 60) + random.uniform(0, 2)


def chat_json(system: str, user: str, *, max_tokens: int = 2000, temperature: float = 0.2,
              model_env: str | None = None) -> dict:
    """Ask for a JSON object; returns it parsed. Raises LLMError when every model fails.

    model_env names an env var (see TASK_MODELS) whose models are tried first."""
    global calls_made
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise LLMError("OPENROUTER_API_KEY is not set")
    headers = {"Authorization": f"Bearer {key}", "X-Title": "job-apply-agent"}
    messages = [{"role": "system", "content": system + "\nRespond with a single JSON object only — no prose, no markdown."},
                {"role": "user", "content": user}]
    last_err = ""

    for model in models(model_env):
        bad_outputs = 0
        for attempt in range(RETRIES_PER_MODEL):
            resp = None
            calls_made += 1
            try:
                resp = requests.post(URL, headers=headers, timeout=240, json={
                    "model": model, "messages": messages, "temperature": temperature,
                    "max_tokens": max_tokens + REASONING_HEADROOM,
                    "reasoning": {"effort": "low", "exclude": True},
                })
                body = resp.json() if resp.content else {}
            except (requests.RequestException, ValueError) as e:
                last_err = f"{model}: {e}"
                time.sleep(_backoff(attempt, None))
                continue

            err = body.get("error") if isinstance(body, dict) else None
            if resp.status_code in (401, 403):
                raise LLMError(f"OpenRouter auth failed ({resp.status_code}): {err}")
            if err or resp.status_code >= 400:
                msg = (err or {}).get("message", "") if isinstance(err, dict) else str(err)
                last_err = f"{model}: HTTP {resp.status_code} {msg}"
                if "per-day" in msg or "per day" in msg:
                    raise DailyLimitReached(last_err)
                if resp.status_code == 429 or resp.status_code >= 500 or "rate" in msg.lower():
                    wait = _backoff(attempt, resp)
                    log.warning("%s — retrying in %.0fs", last_err, wait)
                    time.sleep(wait)
                    continue
                log.warning("%s — not retryable, switching model", last_err)
                break  # 400/404 etc: this model won't work, try the next one

            choice = (body.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            try:
                return parse_json(content)
            except (ValueError, json.JSONDecodeError) as e:
                last_err = f"{model}: unparseable output (finish={choice.get('finish_reason')}, {e}): {content[:150]!r}"
                log.warning(last_err)
                bad_outputs += 1
                if bad_outputs > BAD_OUTPUT_RETRIES:
                    break
        log.warning("model %s gave up; trying next model", model)

    raise LLMError(f"all models failed; last error: {last_err}")
