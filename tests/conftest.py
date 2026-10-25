import shutil
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from alertwhy import config, datagen  # noqa: E402
from alertwhy.federation import LocalFederation  # noqa: E402
from alertwhy.llm import MeterlessHeaders, Provider, Router  # noqa: E402

HAS_NODE = bool(shutil.which("node")) and (ROOT / "engines" / "hmem" / "node_modules" / ".bin").exists()
needs_node = pytest.mark.skipif(not HAS_NODE, reason="Node.js + `npm install` in engines/hmem required")


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("alertwhy-data")
    config.DATA_DIR = Path(d)
    config.HMEM_DIR = Path(d) / "hmem"
    datagen.build(d)
    return Path(d)


@pytest.fixture(scope="session")
def local_fed(data_dir):
    return LocalFederation(data_dir)


@pytest.fixture()
def ai_router():
    """Build a Router whose only provider is a fake Meterless Nightshift runtime."""
    def make(ns, name="meterless"):
        p = Provider(name, "http://127.0.0.1:8088", ns.token, "meterless/auto", headers=MeterlessHeaders(),
                     client=ns.client())
        return Router([p])
    return make


@pytest.fixture()
def mem(tmp_path):
    from alertwhy.hmem import AnalystMemory, HMemClient
    m = AnalystMemory(HMemClient(persist_dir=tmp_path / "hmem"))
    yield m
    m.client.close()
