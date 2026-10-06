"""Shared Gemini/Groq call helpers.

Every AI-touching module (resume extraction, keyword tools, section editing)
used to carry its own copy of "configure Gemini, walk a list of model names,
fall back to Groq" — three near-identical implementations that had drifted
out of sync. This is the one place that knows how to do that — which also
means it was the only place that needed touching to move off the deprecated
`google.generativeai` SDK onto its replacement, `google.genai`.

The model list below is ordered from production evidence (server.log): on
this project's API key/version, gemini-2.0-flash and gemini-pro-latest
consistently 404 ("not found"), so they've been dropped rather than kept as
guaranteed-to-fail attempts that only add latency before the real fallback.
"""

import logging
import os
import time

from google import genai
from dotenv import load_dotenv

from src import ai_fallback
from src.logging_config import get_logger

load_dotenv()

logger = get_logger(__name__)

# google-genai logs an "automatic function calling" advisory on every single
# generate_content call — noise, since we never use function calling here.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

GEMINI_MODELS_TO_TRY = [
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
]


def configure_gemini():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not found in environment variables.")
    return genai.Client(api_key=api_key)


def generate_with_gemini_fallback(prompt, models=None, retry_on_rate_limit=True, rate_limit_sleep_seconds=30):
    """Tries each model in `models` in order. On a rate-limit error, sleeps
    once and retries that same model before moving on. Raises the last
    error if every model fails."""
    client = configure_gemini()
    models = models or GEMINI_MODELS_TO_TRY

    last_error = Exception("No Gemini models were attempted.")
    for model_name in models:
        try:
            logger.info(f"Attempting Gemini model: {model_name}")
            response = client.models.generate_content(model=model_name, contents=prompt)
            logger.info(f"Success with {model_name}")
            return response
        except Exception as e:
            last_error = e
            err_msg = str(e)
            if "quota" in err_msg.lower() or "429" in err_msg:
                logger.warning(f"Quota exhausted for {model_name}, skipping")
            elif "rate" in err_msg.lower() and retry_on_rate_limit:
                logger.warning(f"Rate limited for {model_name}, waiting {rate_limit_sleep_seconds}s and retrying once")
                time.sleep(rate_limit_sleep_seconds)
                try:
                    response = client.models.generate_content(model=model_name, contents=prompt)
                    return response
                except Exception as retry_e:
                    last_error = retry_e
                    logger.warning(f"Retry of {model_name} failed: {retry_e}")
            elif "not found" in err_msg.lower():
                logger.warning(f"{model_name} not found, skipping")
            else:
                logger.warning(f"Error with {model_name}: {err_msg}")

    raise last_error


def generate_with_ai_fallback(prompt, models=None, groq_first=False, retry_on_rate_limit=True):
    """Full fallback chain across Gemini's model list and Groq. Returns an
    object with a `.text` attribute regardless of which provider answered.

    groq_first=True is for latency-sensitive interactive calls (e.g. the
    "Tell AI" section editor) where a user is watching a spinner and Groq is
    both fast and currently reliable — only fall back to working through the
    (often partially quota-exhausted) Gemini list if Groq itself fails.
    """
    if groq_first and ai_fallback.groq_available():
        try:
            return ai_fallback.generate_with_groq(prompt)
        except Exception as e:
            logger.warning(f"Groq (tried first) failed ({e}); falling back to Gemini")

    try:
        return generate_with_gemini_fallback(prompt, models=models, retry_on_rate_limit=retry_on_rate_limit)
    except Exception as e:
        if not groq_first and ai_fallback.groq_available():
            logger.warning(f"All Gemini models failed ({e}); trying Groq fallback")
            return ai_fallback.generate_with_groq(prompt)
        raise
