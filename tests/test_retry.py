"""Provider retry policy: transient failures are retried a bounded number of times; misconfiguration is not."""
import httpx
import pytest

from alertwhy import llm
from alertwhy.llm import Provider, ProviderError


def make(handler, monkeypatch):
    monkeypatch.setattr(llm, "RETRY_DELAYS", (0, 0))  # no sleeping in tests
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Provider("meterless", "http://fake:8088/v1", "tok", "meterless/auto", client=client)


OK = {"choices": [{"message": {"role": "assistant", "content": "hi"}}], "model": "meterless/auto", "usage": {}}


def test_503_then_success_is_retried(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503 if len(calls) == 1 else 200, json=OK)

    r = make(handler, monkeypatch).chat([{"role": "user", "content": "x"}])
    assert r["message"]["content"] == "hi" and len(calls) == 2


def test_connection_error_gives_up_after_three_tries(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ProviderError) as e:
        make(handler, monkeypatch).chat([{"role": "user", "content": "x"}])
    assert e.value.kind == "offline" and len(calls) == 3 and e.value.attempts == 3


def test_unauthorized_is_never_retried(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad token"})

    with pytest.raises(ProviderError) as e:
        make(handler, monkeypatch).chat([{"role": "user", "content": "x"}])
    assert e.value.kind == "unauthorized" and len(calls) == 1


def test_client_error_400_is_not_retried(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": "bad request"})

    with pytest.raises(ProviderError) as e:
        make(handler, monkeypatch).chat([{"role": "user", "content": "x"}])
    assert e.value.kind == "http" and e.value.status == 400 and len(calls) == 1
