"""AI providers. PRIMARY: Meterless Runtime (Nightshift) - an OpenAI-compatible provider. Optional secondary: any
OpenAI-compatible endpoint (e.g. NVIDIA's API for Nemotron).

Meterless Runtime contract (Meterless "Add Meterless as a provider" prompt, engines page):
  base URL normalised to <host>/v1 | Authorization: Bearer <runtime token> | header `meterless-app` | header
  `meterless-scope: user` | GET /v1/models for discovery | POST /v1/chat/completions | /healthz is only a process check.
  State: online (models 200) / unauthorized (401, 403) / offline (no connection). Model ids such as `meterless/auto`
  are routing modes, not specific models: the real model that answered is reported back in the response.
Secrets are never logged and never included in error messages.
"""
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from . import config


RETRY_DELAYS = (0.5, 1.5)          # seconds between attempts: up to 3 tries in total
TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind  # offline | unauthorized | http | bad_response
        self.status = None  # HTTP status for kind == "http"
        self.attempts = 1   # total tries made before giving up


def normalize_base_url(url: str) -> str:
    """http://host:8088 | http://host:8088/v1 | http://host:8088/v1/chat/completions -> http://host:8088/v1"""
    u = (url or "").strip().rstrip("/")
    u = re.sub(r"/chat/completions$", "", u)
    u = re.sub(r"/models$", "", u)
    if not u:
        return ""
    if not u.endswith("/v1"):
        u += "/v1"
    return u


def parse_connect_link(link: str):
    """Meterless connect links look like http://<host>:<port>/connect#k=<key>. Returns (base_url, key) or None."""
    m = re.match(r"^(https?://[^/#]+)/connect#k=(.+)$", (link or "").strip())
    return (normalize_base_url(m.group(1)), m.group(2)) if m else None


@dataclass
class TokenMeter:
    """Tokens and calls spent by one investigation (read from the provider's own `usage` fields)."""
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    models: set = field(default_factory=set)
    providers: set = field(default_factory=set)

    def add(self, resp: dict):
        u = resp.get("usage") or {}
        self.tokens_in += int(u.get("prompt_tokens") or 0)
        self.tokens_out += int(u.get("completion_tokens") or 0)
        self.calls += 1
        if resp.get("model"):
            self.models.add(resp["model"])
        self.providers.add(resp["provider"])


class Provider:
    def __init__(self, name, base_url, token, model, headers=None, timeout=None, client=None):
        self.name = name
        self.base_url = normalize_base_url(base_url)
        self._token = token
        self.model = model
        self.extra_headers = headers or {}
        self.timeout = timeout or config.METERLESS_TIMEOUT
        self.client = client or httpx.Client(timeout=self.timeout)

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json", **self.extra_headers}

    def _req(self, method, path, **kw):
        """Retry only transient failures (connection errors, 429 and 5xx), with a short backoff.
        Auth and malformed-response errors are never retried: retrying them hides misconfiguration."""
        for i in range(len(RETRY_DELAYS) + 1):
            try:
                return self._req_once(method, path, **kw)
            except ProviderError as e:
                e.attempts = i + 1
                transient = e.kind == "offline" or (e.kind == "http" and e.status in TRANSIENT_STATUS)
                if not transient or i == len(RETRY_DELAYS):
                    raise
                time.sleep(RETRY_DELAYS[i])

    def _req_once(self, method, path, **kw):
        try:
            r = self.client.request(method, self.base_url + path, headers=self._headers(), **kw)
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name}: cannot connect to {self.base_url} ({type(e).__name__})", "offline")
        if r.status_code in (401, 403):
            raise ProviderError(f"{self.name}: unauthorized (HTTP {r.status_code}); check the runtime token", "unauthorized")
        if r.status_code >= 400:
            err = ProviderError(f"{self.name}: HTTP {r.status_code}: {r.text[:200]}", "http")
            err.status = r.status_code
            raise err
        try:
            return r.json()
        except ValueError:
            raise ProviderError(f"{self.name}: non-JSON response", "bad_response")

    def models(self):
        data = self._req("GET", "/models")
        items = data.get("data", data) if isinstance(data, dict) else data
        return [m.get("id") if isinstance(m, dict) else str(m) for m in items]

    def status(self) -> dict:
        try:
            ids = self.models()
            return {"name": self.name, "state": "online", "base_url": self.base_url, "model": self.model,
                    "models": ids[:30], "model_listed": (self.model in ids) or self.model.endswith("/auto"),
                    "error": None}
        except ProviderError as e:
            return {"name": self.name, "state": e.kind if e.kind in ("offline", "unauthorized") else "offline",
                    "base_url": self.base_url, "model": self.model, "models": [], "model_listed": False, "error": str(e)}

    def chat(self, messages, tools=None, temperature=0.1, max_tokens=700) -> dict:
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        data = self._req("POST", "/chat/completions", json=body)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"{self.name}: unexpected response shape", "bad_response")
        return {"message": msg, "usage": data.get("usage") or {}, "model": data.get("model") or self.model,
                "provider": self.name}


class Router:
    """Primary provider first; fall back to the next one only on connection/server errors."""

    def __init__(self, providers):
        self.providers = list(providers)

    @property
    def enabled(self):
        return bool(self.providers)

    def chat(self, messages, tools=None, **kw):
        last = None
        for p in self.providers:
            try:
                return p.chat(messages, tools=tools, **kw)
            except ProviderError as e:
                last = e
                if e.kind in ("unauthorized", "bad_response"):
                    break  # misconfiguration: do not silently use another provider
        raise last or ProviderError("no AI provider configured", "offline")

    def status(self):
        return [p.status() for p in self.providers]

    def set_primary(self, provider):
        """Make `provider` first (runtime settings screen); a previous provider with the same name is replaced."""
        self.providers = [provider] + [p for p in self.providers if p.name != provider.name]

    def get(self, name):
        return next((p for p in self.providers if p.name == name), None)


def meterless_provider(base_url, token, model=None, client=None):
    """The Meterless Runtime (Nightshift) provider: bearer token, `meterless-app` and `meterless-scope: user` headers."""
    return Provider("meterless", base_url, token, model or config.METERLESS_MODEL,
                    headers={"meterless-app": config.METERLESS_APP, "meterless-scope": "user"}, client=client)


def build_router() -> Router:
    """Providers in priority order.
    1. METERLESS_BASE_URL + METERLESS_RUNTIME_TOKEN: Meterless Nightshift runtime (primary).
    2. MODEL_BASE_URL + MODEL_API_KEY + NEMOTRON_MODEL: the guide's generic route variables. A URL containing
       "nvidia" is treated as NVIDIA's own API; anything else is treated as a Meterless route."""
    ps = []
    if config.METERLESS_BASE_URL and config.METERLESS_RUNTIME_TOKEN:
        ps.append(Provider("meterless", config.METERLESS_BASE_URL, config.METERLESS_RUNTIME_TOKEN,
                           config.METERLESS_MODEL, headers=MeterlessHeaders()))
    if config.MODEL_BASE_URL and config.MODEL_API_KEY and config.NEMOTRON_MODEL:
        nvidia = "nvidia" in config.MODEL_BASE_URL.lower()
        ps.append(Provider("nvidia" if nvidia else "meterless", config.MODEL_BASE_URL, config.MODEL_API_KEY,
                           config.NEMOTRON_MODEL, headers={} if nvidia else MeterlessHeaders(), timeout=90))
    return Router(ps)


def MeterlessHeaders():
    return {"meterless-app": config.METERLESS_APP, "meterless-scope": "user"}


def host_of(url: str) -> str:
    return urlparse(url).netloc
