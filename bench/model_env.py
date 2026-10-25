"""Environment variables that point a model at its provider: API key and base URL.

Shared by the dashboard (single runs, custom prompts) and the suite runner, which
hands them to its subprocess workers. Two copies of this mapping drifted: the
suite runner forwarded keys for four prefixes only, so a Groq or NVIDIA key typed
in the form never reached the worker, and it passed a base URL through raw where
the dashboard adds the scheme, port and ``/v1`` the OpenAI client needs.
"""

from __future__ import annotations

from urllib.parse import urlparse

PROVIDER_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "custom": "OPENAI_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}


def normalize_openai_base_url(base_url: str, default_port: int | None = None) -> str:
    """Ensure a user-supplied URL has an http:// scheme, optional default port, and /v1 suffix."""
    url = base_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    parsed = urlparse(url)
    if default_port and not parsed.port:
        url = f"{parsed.scheme}://{parsed.hostname}:{default_port}{parsed.path or ''}"
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def model_env_vars(model_key: str, api_key: str | None, base_url: str | None) -> dict[str, str]:
    provider = model_key.partition(":")[0]
    env: dict[str, str] = {}
    if api_key:
        var = PROVIDER_ENV.get(provider)
        if var:
            env[var] = api_key
    if base_url:
        if provider == "ollama":
            env["OLLAMA_HOST"] = base_url
            # pydantic-ai uses AsyncOpenAI (OpenAI-compat) for ollama, so also
            # set OPENAI_BASE_URL — the only path that reaches _build_model
            env["OPENAI_BASE_URL"] = normalize_openai_base_url(base_url, default_port=11434)
            env.setdefault("OPENAI_API_KEY", "not-needed")
        elif provider in ("lmstudio", "custom", "openai"):
            env["OPENAI_BASE_URL"] = normalize_openai_base_url(
                base_url, default_port=1234 if provider == "lmstudio" else None
            )
            if not api_key:
                env.setdefault("OPENAI_API_KEY", "local")
    return env
