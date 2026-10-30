"""Exercise the real chemotionapi connection without contacting a server."""

import json
from urllib.parse import parse_qs

import pytest
from requests import Response
from requests.exceptions import Timeout

from chemotion_inventorizer import client as api_client


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_client, "CHEMOTION_URL", "https://eln.example.org")
    return api_client.ChemotionClient()


def test_login_and_inventory_requests_use_chemotionapi_token(client, monkeypatch):
    monkeypatch.setattr(api_client, "CHEMOTION_USERNAME", "example")
    monkeypatch.setattr(api_client, "CHEMOTION_PASSWORD", "test-password")
    requests = []

    def send(request, **kwargs):
        requests.append(request)
        assert kwargs["timeout"] == api_client.REQUEST_TIMEOUT_SECONDS
        assert kwargs["verify"] is True
        response = Response()
        response.request = request
        response.status_code = 201 if request.url.endswith("/token") else 200
        response._content = b'{"token":"test-token"}' if response.status_code == 201 else b'{}'
        return response

    monkeypatch.setattr(client.connection, "send", send)
    client.login()
    client.request("/api/v1/samples")
    client.request("/api/v1/samples/1", b'{"name":"updated"}',
                   {"Content-Type": "application/json"}, "PUT")
    client.request("https://eln.example.org/images/molecules/1.svg")
    assert requests[0].url == "https://eln.example.org/api/v1/public/token"
    assert parse_qs(requests[0].body) == {"username": ["example"], "password": ["test-password"]}
    for request in requests[1:]:
        assert request.headers["Authorization"] == "Bearer test-token"
    assert json.loads(requests[2].body) == {"name": "updated"}
    assert requests[3].url == "https://eln.example.org/images/molecules/1.svg"


@pytest.mark.parametrize("url", ["https://other.example.org/image.svg", "//other.example.org/image.svg",
                                 "http://eln.example.org/image.svg"])
def test_external_urls_are_rejected_before_sending_credentials(client, monkeypatch, url):
    def send(*args, **kwargs):
        pytest.fail("An off-site request must not be sent")

    monkeypatch.setattr(client.connection, "send", send)
    with pytest.raises(ConnectionError, match="outside"):
        client.request(url)


@pytest.mark.parametrize("method, attempts", [("GET", 3), ("PUT", 1), ("POST", 1), ("DELETE", 1)])
def test_transport_failure_only_retries_reads(client, monkeypatch, method, attempts):
    calls = []

    def send(request, **kwargs):
        calls.append(request)
        raise Timeout("Timed out")

    monkeypatch.setattr(client.connection, "send", send)
    monkeypatch.setattr(api_client.time, "sleep", lambda seconds: None)
    with pytest.raises(ConnectionError):
        client.request("/api/v1/samples/1", method=method)
    assert len(calls) == attempts
