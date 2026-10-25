from alertwhy import cli


class FakeInvestigator:
    def __init__(self, status):
        self._status = status

    def sponsor_status(self):
        return self._status


def test_status_fails_for_offline_sponsor_services(monkeypatch):
    monkeypatch.setattr(cli, "Investigator", lambda: FakeInvestigator({
        "zetaris": {
            "live": False,
            "backend": "local-duckdb (offline fallback, NOT Zetaris)",
            "check": {"reachable": True, "select1": True, "tables": {}, "error": None},
        },
        "meterless": {"providers": [], "live": False},
        "hmem": {"state": "online", "records": 0, "error": None},
    }))

    assert cli.status() is False


def test_status_passes_only_when_all_required_layers_are_online(monkeypatch):
    monkeypatch.setattr(cli, "Investigator", lambda: FakeInvestigator({
        "zetaris": {
            "live": True,
            "backend": "Zetaris (REST, live)",
            "check": {"reachable": True, "select1": True, "tables": {}, "error": None},
        },
        "meterless": {
            "providers": [{"name": "meterless", "state": "online", "base_url": "http://localhost:8088/v1",
                           "model": "meterless/auto", "error": None}],
            "live": True,
        },
        "hmem": {"state": "online", "records": 0, "error": None},
    }))

    assert cli.status() is True
