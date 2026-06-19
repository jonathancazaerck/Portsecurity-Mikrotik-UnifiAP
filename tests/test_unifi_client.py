import pytest
import requests

from ap_switch_watchdog.unifi_client import UniFiClient, UniFiError


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


class FakeSession:
    def __init__(self, responses):
        # responses: dict mapping (method, url) -> FakeResponse or list of FakeResponse
        self._responses = responses
        self.calls = []
        self.verify = None

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        return self._pop("POST", url)

    def get(self, url, timeout=None):
        self.calls.append(("GET", url, None))
        return self._pop("GET", url)

    def _pop(self, method, url):
        value = self._responses[(method, url)]
        if isinstance(value, list):
            return value.pop(0)
        return value

    def close(self):
        pass


UNIFI_OS_LOGIN = ("POST", "https://192.0.2.1/api/auth/login")
CLASSIC_LOGIN = ("POST", "https://192.0.2.1/api/login")


def make_client(responses):
    session = FakeSession(responses)
    client = UniFiClient(
        url="https://192.0.2.1",
        username="watchdog",
        password="secret",
        site="default",
        verify_ssl=False,
        session=session,
    )
    return client, session


def test_login_unifi_os_sets_proxy_prefix():
    client, session = make_client({UNIFI_OS_LOGIN: FakeResponse(200)})

    client.login()

    assert client._api_prefix == "/proxy/network"


def test_login_falls_back_to_classic_controller():
    client, session = make_client(
        {
            UNIFI_OS_LOGIN: FakeResponse(404),
            CLASSIC_LOGIN: FakeResponse(200),
        }
    )

    client.login()

    assert client._api_prefix == ""


def test_login_failure_raises():
    client, session = make_client(
        {
            UNIFI_OS_LOGIN: FakeResponse(404),
            CLASSIC_LOGIN: FakeResponse(401),
        }
    )

    with pytest.raises(UniFiError):
        client.login()


def test_get_known_ap_macs_filters_to_uap_and_lowercases(monkeypatch):
    devices_url = ("GET", "https://192.0.2.1/proxy/network/api/s/default/stat/device")
    client, session = make_client(
        {
            UNIFI_OS_LOGIN: FakeResponse(200),
            devices_url: FakeResponse(
                200,
                {
                    "data": [
                        {"type": "uap", "mac": "aa:bb:cc:dd:ee:01", "state": 1},
                        # Known APs are returned regardless of "state".
                        {"type": "uap", "mac": "aa:bb:cc:dd:ee:02", "state": 0},
                        # MACs are normalized to lowercase.
                        {"type": "uap", "mac": "AA:BB:CC:DD:EE:03", "state": 1},
                        {"type": "usw", "mac": "aa:bb:cc:dd:ee:04", "state": 1},
                    ]
                },
            ),
        }
    )

    result = client.get_known_ap_macs()

    assert result == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:03"}


def test_get_known_ap_macs_relogs_in_on_401():
    devices_url = ("GET", "https://192.0.2.1/proxy/network/api/s/default/stat/device")
    client, session = make_client(
        {
            UNIFI_OS_LOGIN: [FakeResponse(200), FakeResponse(200)],
            devices_url: [
                FakeResponse(401),
                FakeResponse(200, {"data": [{"type": "uap", "mac": "aa:bb:cc:dd:ee:01", "state": 1}]}),
            ],
        }
    )

    result = client.get_known_ap_macs()

    assert result == {"aa:bb:cc:dd:ee:01"}
    # login, devices(401), login again, devices(200)
    assert len(session.calls) == 4


def test_get_known_ap_macs_raises_on_persistent_error():
    devices_url = ("GET", "https://192.0.2.1/proxy/network/api/s/default/stat/device")
    client, session = make_client(
        {
            UNIFI_OS_LOGIN: [FakeResponse(200), FakeResponse(200)],
            devices_url: [FakeResponse(500), FakeResponse(500)],
        }
    )

    with pytest.raises(UniFiError):
        client.get_known_ap_macs()


def test_login_network_error_raises():
    class BrokenSession(FakeSession):
        def post(self, url, json=None, timeout=None):
            raise requests.ConnectionError("boom")

    client = UniFiClient(
        url="https://192.0.2.1",
        username="watchdog",
        password="secret",
        session=BrokenSession({}),
    )

    with pytest.raises(UniFiError):
        client.login()
