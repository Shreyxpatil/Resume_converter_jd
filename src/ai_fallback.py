"""Groq fallback used when every Gemini model in a caller's list has failed
(daily quota exhausted, deprecated model, network issue, etc). Groq's API is
OpenAI-compatible, so this talks to it with plain `requests` rather than
pulling in a new SDK dependency.
"""

import os
import requests
from dotenv import load_dotenv

from src.logging_config import get_logger

load_dotenv()

logger = get_logger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Ordered by capability — 70B first for extraction/edit quality, smaller ones
# only as a last resort if the big one is unavailable/rate-limited.
GROQ_MODELS_TO_TRY = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-8b-instant"]


class GroqResponse:
    """Mimics the minimal surface of a google.generativeai response (`.text`)
    so callers can treat a Groq result the same way as a Gemini one."""

    def __init__(self, text):
        self.text = text


def groq_available():
    return bool(os.getenv("GROQ_API") or os.getenv("GROK_API"))


def generate_with_groq(prompt):
    api_key = os.getenv("GROQ_API") or os.getenv("GROK_API")
    if not api_key:
        raise Exception("GROQ_API or GROK_API not set in environment — no fallback available.")

    last_error = Exception("No Groq models were attempted.")
    for model_name in GROQ_MODELS_TO_TRY:
        try:
            print(f"🟢 Trying Groq fallback model: {model_name}...")
            resp = requests.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_name,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 8000,
                },
                timeout=120,
            )
            if resp.status_code != 200:
                last_error = Exception(f"Groq API error {resp.status_code}: {resp.text[:300]}")
                print(f"⚠️ Groq model {model_name} failed: {last_error}")
                continue

            content = resp.json()["choices"][0]["message"]["content"]
            print(f"✅ Groq fallback succeeded with {model_name}!")
            return GroqResponse(content)
        except Exception as e:
            last_error = e
            print(f"⚠️ Groq model {model_name} failed: {e}")
            continue

    raise last_error
