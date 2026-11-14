"""InfluxDB 1.x: der Client braucht ein Zeitlimit.

Ohne ``timeout`` wartet der v1-Client unbegrenzt. Ein haengender InfluxDB
blockierte so den ganzen Zyklus, bis der systemd-Watchdog nach 35 min den
Dienst neu startete.
"""
from __future__ import annotations

import pytest

from ems.influx import InfluxV1Backend, V1_TIMEOUT_SECONDS


@pytest.fixture
def client_kwargs(monkeypatch):
    import influxdb
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(influxdb, "InfluxDBClient", FakeClient)
    return seen


def test_v1_client_gets_a_default_timeout(client_kwargs):
    InfluxV1Backend({"database": "homey"})
    assert V1_TIMEOUT_SECONDS == 15.0
    assert client_kwargs["timeout"] == 15.0


def test_v1_timeout_is_configurable(client_kwargs):
    InfluxV1Backend({"database": "homey", "timeout": 4})
    assert client_kwargs["timeout"] == 4.0


@pytest.mark.parametrize("wert", [0, -1])
def test_v1_timeout_must_be_positive(client_kwargs, wert):
    with pytest.raises(ValueError, match="influxdb.v1.timeout"):
        InfluxV1Backend({"database": "homey", "timeout": wert})


def test_v1_ssl_flags_are_read_strictly(client_kwargs):
    InfluxV1Backend({"database": "homey", "ssl": "false",
                     "verify_ssl": "true"})
    assert client_kwargs["ssl"] is False
    assert client_kwargs["verify_ssl"] is True
