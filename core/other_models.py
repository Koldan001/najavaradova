"""Read a notice with a non-Gemini model, for scripts/eval_model.py only - never used for real
alerts. Same instructions and the same answer shape (parser.OutageAlert) as the Gemini reader, so
the scores compare like for like.

Groq (OpenAI-compatible API, free tier, no card): put GROQ_API_KEY in the server's .env - never
in chat or GitHub. Model names: `python scripts/eval_model.py groq/list`.

Only the notice text is sent, as with Gemini.
"""
import json
import os
import time

import requests

GROQ_URL = "https://api.groq.com/openai/v1"


def _key() -> str:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise SystemExit("GROQ_API_KEY is not set - add it to the server's .env first.")
    return key


def groq_models() -> list[str]:
    r = requests.get(f"{GROQ_URL}/models", headers={"Authorization": f"Bearer {_key()}"}, timeout=30)
    r.raise_for_status()
    return sorted(m["id"] for m in r.json().get("data", []))


def read_groq(raw_text: str, model: str, tries: int = 3):
    """parser.OutageAlert or None. JSON mode with the schema in the instructions: works on every
    Groq chat model, unlike strict schema mode, which only some accept."""
    from parser import SYSTEM_INSTRUCTION, OutageAlert
    schema = json.dumps(OutageAlert.model_json_schema(), ensure_ascii=False)
    system = (SYSTEM_INSTRUCTION + "\n\nAnswer with ONE JSON object only, matching this JSON schema "
              "exactly (same field names; null for anything not stated):\n" + schema)
    body = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": "Analyze this utility announcement and extract the "
                                                     f"exact details:\n\n{raw_text}"}]}
    for attempt in range(tries):
        try:
            r = requests.post(f"{GROQ_URL}/chat/completions", json=body, timeout=120,
                              headers={"Authorization": f"Bearer {_key()}"})
        except requests.RequestException as e:
            print(f"   groq: {e}")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            wait = float(r.headers.get("retry-after") or 10 * (attempt + 1))
            print(f"   groq {r.status_code}, waiting {wait:.0f}s")
            time.sleep(min(wait, 60))
            continue
        if r.status_code != 200:
            print(f"   groq {r.status_code}: {r.text[:300]}")
            return None
        text = r.json()["choices"][0]["message"]["content"]
        try:
            return OutageAlert.model_validate_json(text)
        except Exception as e:
            print(f"   groq answer doesn't fit the schema: {str(e)[:300]}")
            return None
    return None
