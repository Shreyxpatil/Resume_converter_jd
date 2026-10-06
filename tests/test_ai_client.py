import pytest

from src import ai_client, ai_fallback


class FakeResponse:
    def __init__(self, text):
        self.text = text


def _patch_genai(monkeypatch, model_behaviors):
    """model_behaviors: dict[model_name] -> success text (str) or Exception to raise."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    class FakeModels:
        @staticmethod
        def generate_content(model, contents):
            behavior = model_behaviors.get(model, Exception(f"unexpected model {model}"))
            if isinstance(behavior, Exception):
                raise behavior
            return FakeResponse(behavior)

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    class FakeGenaiModule:
        Client = FakeClient

    monkeypatch.setattr(ai_client, "genai", FakeGenaiModule)


def test_configure_gemini_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        ai_client.configure_gemini()


def test_generate_with_gemini_fallback_succeeds_on_first_model(monkeypatch):
    _patch_genai(monkeypatch, {"model-a": "ok-a"})
    resp = ai_client.generate_with_gemini_fallback("prompt", models=["model-a"])
    assert resp.text == "ok-a"


def test_generate_with_gemini_fallback_skips_quota_exhausted_model(monkeypatch):
    _patch_genai(monkeypatch, {
        "model-a": Exception("429 quota exceeded"),
        "model-b": "ok-b",
    })
    resp = ai_client.generate_with_gemini_fallback("prompt", models=["model-a", "model-b"])
    assert resp.text == "ok-b"


def test_generate_with_gemini_fallback_skips_not_found_model(monkeypatch):
    _patch_genai(monkeypatch, {
        "model-a": Exception("model not found"),
        "model-b": "ok-b",
    })
    resp = ai_client.generate_with_gemini_fallback("prompt", models=["model-a", "model-b"])
    assert resp.text == "ok-b"


def test_generate_with_gemini_fallback_retries_once_on_rate_limit(monkeypatch):
    monkeypatch.setattr(ai_client.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = {"count": 0}

    class FakeModels:
        @staticmethod
        def generate_content(model, contents):
            calls["count"] += 1
            if calls["count"] == 1:
                raise Exception("rate limit exceeded, try later")
            return FakeResponse("ok-after-retry")

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    class FakeGenaiModule:
        Client = FakeClient

    monkeypatch.setattr(ai_client, "genai", FakeGenaiModule)
    resp = ai_client.generate_with_gemini_fallback("prompt", models=["model-a"])
    assert resp.text == "ok-after-retry"
    assert calls["count"] == 2


def test_generate_with_gemini_fallback_raises_when_all_models_fail(monkeypatch):
    _patch_genai(monkeypatch, {
        "model-a": Exception("boom-a"),
        "model-b": Exception("boom-b"),
    })
    with pytest.raises(Exception):
        ai_client.generate_with_gemini_fallback(
            "prompt", models=["model-a", "model-b"], retry_on_rate_limit=False
        )


def test_generate_with_ai_fallback_falls_back_to_groq_when_gemini_fails(monkeypatch):
    _patch_genai(monkeypatch, {"model-a": Exception("boom")})
    monkeypatch.setattr(ai_fallback, "groq_available", lambda: True)
    monkeypatch.setattr(ai_fallback, "generate_with_groq", lambda prompt: ai_fallback.GroqResponse("groq-result"))

    resp = ai_client.generate_with_ai_fallback(
        "prompt", models=["model-a"], groq_first=False, retry_on_rate_limit=False
    )
    assert resp.text == "groq-result"


def test_generate_with_ai_fallback_raises_when_gemini_fails_and_no_groq(monkeypatch):
    _patch_genai(monkeypatch, {"model-a": Exception("boom")})
    monkeypatch.setattr(ai_fallback, "groq_available", lambda: False)

    with pytest.raises(Exception):
        ai_client.generate_with_ai_fallback(
            "prompt", models=["model-a"], groq_first=False, retry_on_rate_limit=False
        )


def test_generate_with_ai_fallback_groq_first_tries_groq_before_gemini(monkeypatch):
    # Gemini would succeed too, but groq_first=True must return the Groq result
    # without ever needing the Gemini path to work.
    _patch_genai(monkeypatch, {"model-a": "should-not-be-used"})
    monkeypatch.setattr(ai_fallback, "groq_available", lambda: True)
    monkeypatch.setattr(ai_fallback, "generate_with_groq", lambda prompt: ai_fallback.GroqResponse("groq-first-result"))

    resp = ai_client.generate_with_ai_fallback("prompt", models=["model-a"], groq_first=True)
    assert resp.text == "groq-first-result"


def test_generate_with_ai_fallback_groq_first_falls_back_to_gemini_on_groq_failure(monkeypatch):
    _patch_genai(monkeypatch, {"model-a": "gemini-result"})
    monkeypatch.setattr(ai_fallback, "groq_available", lambda: True)

    def failing_groq(prompt):
        raise Exception("groq down")

    monkeypatch.setattr(ai_fallback, "generate_with_groq", failing_groq)

    resp = ai_client.generate_with_ai_fallback("prompt", models=["model-a"], groq_first=True)
    assert resp.text == "gemini-result"
