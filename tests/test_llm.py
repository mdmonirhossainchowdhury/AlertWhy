"""Meterless Nightshift provider contract + routing."""
import httpx
import pytest

from alertwhy import config
from alertwhy.llm import (MeterlessHeaders, Provider, ProviderError, Router, TokenMeter, build_router,
                          normalize_base_url, parse_connect_link)
from fakes import FakeNightshift


@pytest.mark.parametrize("raw,expected", [
    ("http://127.0.0.1:8088", "http://127.0.0.1:8088/v1"),
    ("http://127.0.0.1:8088/", "http://127.0.0.1:8088/v1"),
    ("http://127.0.0.1:8088/v1", "http://127.0.0.1:8088/v1"),
    ("http://127.0.0.1:8088/v1/", "http://127.0.0.1:8088/v1"),
    ("http://127.0.0.1:8088/v1/chat/completions", "http://127.0.0.1:8088/v1"),
    ("https://integrate.api.nvidia.com/v1", "https://integrate.api.nvidia.com/v1"),
    ("", ""),
])
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


def test_parse_connect_link():
    assert parse_connect_link("http://192.168.1.5:8088/connect#k=abc123") == ("http://192.168.1.5:8088/v1", "abc123")
    assert parse_connect_link("http://x/not-a-link") is None


def prov(ns, **kw):
    return Provider("meterless", "http://127.0.0.1:8088", ns.token, "meterless/auto", headers=MeterlessHeaders(),
                    client=ns.client(), **kw)


def test_request_carries_bearer_and_meterless_headers():
    ns = FakeNightshift()
    r = prov(ns).chat([{"role": "user", "content": "hi"}])
    h = ns.headers_seen[-1]
    assert h["authorization"] == "Bearer tok-test"
    assert h["meterless-app"] == config.METERLESS_APP and h["meterless-scope"] == "user"
    assert r["model"] == "fake/nemotron-test" and r["provider"] == "meterless"  # actual model, not the routing mode


def test_status_online_unauthorized_offline():
    ns = FakeNightshift()
    assert prov(ns).status()["state"] == "online"
    bad = Provider("meterless", "http://x", "wrong", "meterless/auto", client=ns.client())
    s = bad.status()
    assert s["state"] == "unauthorized" and "wrong" not in (s["error"] or "")
    off = FakeNightshift(fail="offline")
    assert prov(off).status()["state"] == "offline"


def test_status_does_not_treat_healthz_as_ready():
    """Only /v1/models decides online (guide: /healthz is a process check only)."""
    ns = FakeNightshift()
    ns.fail = "500"
    assert prov(ns).status()["state"] != "online"


def test_router_falls_back_on_offline_but_not_on_unauthorized():
    primary_off, secondary = FakeNightshift(fail="offline"), FakeNightshift()
    r = Router([prov(primary_off), Provider("nvidia", "http://y", secondary.token, "m", client=secondary.client())])
    assert r.chat([{"role": "user", "content": "x"}])["provider"] == "nvidia"
    ns_bad, secondary2 = FakeNightshift(token="other"), FakeNightshift()
    p1 = Provider("meterless", "http://x", "mismatch", "meterless/auto", client=ns_bad.client())
    p2 = Provider("nvidia", "http://y", secondary2.token, "m", client=secondary2.client())
    with pytest.raises(ProviderError) as e:
        Router([p1, p2]).chat([{"role": "user", "content": "x"}])
    assert e.value.kind == "unauthorized" and secondary2.calls == []  # misconfiguration is not silently masked


def test_token_meter_sums_provider_usage():
    ns, m = FakeNightshift(), TokenMeter()
    p = prov(ns)
    for _ in range(3):
        m.add(p.chat([{"role": "user", "content": "x"}]))
    assert (m.tokens_in, m.tokens_out, m.calls) == (300, 150, 3) and m.models == {"fake/nemotron-test"}


def test_build_router_routes(monkeypatch):
    for k in ("METERLESS_BASE_URL", "METERLESS_RUNTIME_TOKEN", "MODEL_BASE_URL", "MODEL_API_KEY", "NEMOTRON_MODEL"):
        monkeypatch.setattr(config, k, "")
    assert not build_router().enabled
    monkeypatch.setattr(config, "METERLESS_BASE_URL", "http://127.0.0.1:8088")
    monkeypatch.setattr(config, "METERLESS_RUNTIME_TOKEN", "t")
    assert [p.name for p in build_router().providers] == ["meterless"]
    monkeypatch.setattr(config, "MODEL_BASE_URL", "https://integrate.api.nvidia.com/v1")
    monkeypatch.setattr(config, "MODEL_API_KEY", "k")
    monkeypatch.setattr(config, "NEMOTRON_MODEL", "nvidia/some-nemotron")
    assert [p.name for p in build_router().providers] == ["meterless", "nvidia"]
    monkeypatch.setattr(config, "METERLESS_BASE_URL", "")
    monkeypatch.setattr(config, "MODEL_BASE_URL", "http://127.0.0.1:8088/v1")  # guide's MODEL_* pointing at Nightshift
    assert [p.name for p in build_router().providers] == ["meterless"]
