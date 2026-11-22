"""Thin provider-agnostic LLM client (plain HTTP, no SDK lock-in).
Supports Anthropic and any OpenAI-compatible endpoint (OpenAI, Gemini's
OpenAI endpoint, Groq, a local vLLM...)."""
import httpx

from . import config


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    start, end = s.find("{"), s.rfind("}")
    return s[start:end + 1] if start >= 0 else s


def complete_json(prompt: str, images_b64: list[str] | None = None, timeout=60) -> str:
    images_b64 = images_b64 or []
    if config.LLM_PROVIDER == "anthropic":
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
                   for b in images_b64[:3]]
        content.append({"type": "text", "text": prompt})
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=timeout,
                       headers={"x-api-key": config.ANTHROPIC_API_KEY,
                                "anthropic-version": "2023-06-01", "content-type": "application/json"},
                       json={"model": config.ANTHROPIC_MODEL, "max_tokens": 2000, "temperature": 0,
                             "messages": [{"role": "user", "content": content}]})
        r.raise_for_status()
        return _strip_fences("".join(b.get("text", "") for b in r.json()["content"]))
    if config.LLM_PROVIDER == "openai":
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images_b64[:3]]
        r = httpx.post(f"{config.OPENAI_BASE_URL.rstrip('/')}/chat/completions", timeout=timeout,
                       headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                       json={"model": config.OPENAI_MODEL, "temperature": 0,
                             "response_format": {"type": "json_object"},
                             "messages": [{"role": "user", "content": content}]})
        r.raise_for_status()
        return _strip_fences(r.json()["choices"][0]["message"]["content"])
    raise RuntimeError("No LLM provider configured")
