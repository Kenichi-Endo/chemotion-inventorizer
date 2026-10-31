"""Exercise the real chemotionapi connection without contacting a server."""

import json
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import chemotion_api
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


@pytest.fixture
def sample_server(client, monkeypatch):
    """Run real chemotionapi objects against an in-memory server response."""
    template = Path(chemotion_api.__file__).parent / "elements" / "empty_elements" / "sample.json"
    sample = json.loads(template.read_text())
    sample.update({
        "id": 7, "is_new": False, "short_label": "S7", "name": "Original",
        "container": {"id": 3, "container_type": "root", "children": [
            {"id": 4, "container_type": "analyses", "children": []}]},
        "decoupled": True, "inventory_sample": False,
        "real_amount_value": "12", "real_amount_unit": "mg",
        "target_amount_value": "20", "target_amount_unit": "mg",
        "description": "Keep this description", "molecule_name_id": 11, "molfile": "sample molfile",
        "molecule": {"id": 9, "molfile": "original molfile", "cano_smiles": "CCO"},
    })
    calls = []

    def send(request, **kwargs):
        path = urlsplit(request.url).path
        calls.append((request.method, path, request.body))
        response = Response()
        response.request = request
        response.status_code = 200
        if path == "/about":
            response._content = b"Version: 3.1.2"
            return response
        if path == "/api/v1/generic_elements/klasses.json":
            payload = {"klass": [{"name": "sample", "is_generic": False}]}
        elif path == "/api/v1/segments/klasses.json":
            payload = {"klass": []}
        elif path in {"/api/v1/samples/7.json", "/api/v1/samples/7"}:
            payload = {"sample": sample}
        elif path == "/api/v1/chemicals":
            payload = {"id": 5, "chemical_data": [{"vendor": "Vendor"}]}
        else:
            pytest.fail(f"Unexpected request: {request.method} {path}")
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(client.connection, "send", send)
    return sample, calls


def test_inventory_detail_uses_library_sample_loading(client, sample_server):
    sample, calls = sample_server
    row = client.inventory_entry(7, 42)
    assert row["id"] == 7
    assert row["sample"] == "Original"
    assert row["vendor"] == "Vendor"
    assert any(path == "/api/v1/samples/7.json" for _, path, _ in calls)


@pytest.mark.parametrize("method, args, expected", [
    ("set_location", ("New",), {"location": "New"}),
    ("set_density", ("1.25",), {"density": "1.25"}),
    ("set_sample_name", ("Renamed",), {"name": "Renamed"}),
    ("set_molecule_name", (12,), {"molecule_name_id": 12}),
    ("set_amount", ("real_amount", "0", "g"), {"real_amount_value": "0", "real_amount_unit": "g"}),
    ("set_amount", ("target_amount", "250", "mg"), {"target_amount_value": "250", "target_amount_unit": "mg"}),
])
def test_library_save_preserves_current_sample_data(client, sample_server, method, args, expected):
    sample, calls = sample_server
    # Deliberately stale GUI flags/container must not replace server values.
    getattr(client, method)(7, *args, {"id": 999}, False, True)
    writes = [(verb, path, json.loads(body)) for verb, path, body in calls if verb != "GET"]
    assert len(writes) == 1
    verb, path, body = writes[0]
    assert verb == "PUT" and path == "/api/v1/samples/7"
    for key, value in expected.items():
        assert body[key] == value
    assert body["container"] == sample["container"]
    assert body["decoupled"] is True
    assert body["inventory_sample"] is False
    assert body["description"] == "Keep this description"
    assert body["molfile"] == "sample molfile"
    for key in ("real_amount_value", "real_amount_unit", "target_amount_value", "target_amount_unit", "molecule_name_id"):
        if key not in expected:
            assert body[key] == sample[key]


def test_library_save_updates_flags_together(client, sample_server):
    sample, calls = sample_server
    client.set_sample_flags(7, {"id": 999}, False, True)
    body = json.loads([body for method, _, body in calls if method == "PUT"][0])
    assert body["decoupled"] is False
    assert body["inventory_sample"] is True
    assert body["container"] == sample["container"]


def test_library_save_retains_null_sample_structure(client, sample_server):
    sample, calls = sample_server
    sample["molfile"] = None
    client.set_location(7, "New", {}, False, True)
    body = json.loads([body for method, _, body in calls if method == "PUT"][0])
    assert body["molfile"] is None


@pytest.mark.parametrize("shared", [False, True])
def test_collection_picker_uses_library_trees_and_retains_all(client, monkeypatch, shared):
    paths = []
    payloads = {
        "/api/v1/collections/roots.json": {"collections": [
            {"id": 2, "label": "Group", "children": [{"id": 3, "label": "Inventory"}]}]},
        "/api/v1/collections/all": {"collection": {"id": 1, "label": "All", "is_locked": True}},
        "/api/v1/syncCollections/sync_remote_roots": {"syncCollections": [
            {"id": 4, "label": "Shared group", "children": [
                {"id": 5, "label": "Shared inventory", "sharer": {"initials": "ABC"}}]}] if shared else []},
    }

    def send(request, **kwargs):
        path = urlsplit(request.url).path
        paths.append(path)
        assert request.method == "GET"
        response = Response()
        response.status_code = 200
        response._content = json.dumps(payloads[path]).encode()
        return response

    def custom_request(*args, **kwargs):
        pytest.fail("Collection loading must use chemotionapi functions")

    monkeypatch.setattr(client.connection, "send", send)
    monkeypatch.setattr(client, "request", custom_request)
    collections = client.collections()
    assert [(item["id"], item["_synchronized"]) for item in collections] == (
        [(1, False), (3, False), (5, True)] if shared else [(1, False), (3, False)])
    if shared:
        assert collections[-1]["_source_collection_id"] == 4
        assert collections[-1]["sharer"] == {"initials": "ABC"}
    assert paths == list(payloads)


@pytest.mark.parametrize("synchronized", [False, True])
@pytest.mark.parametrize("headers", [False, True])
def test_library_pagination_loads_every_page(client, sample_server, monkeypatch, synchronized, headers):
    sample, calls = sample_server
    original_send = client.connection.send
    pages = []

    def send(request, **kwargs):
        if urlsplit(request.url).path != "/api/v1/samples.json":
            return original_send(request, **kwargs)
        query = parse_qs(request.body)
        collection_key = "sync_collection_id" if synchronized else "collection_id"
        assert query[collection_key] == ["42"]
        page = int(query["page"][0])
        pages.append(page)
        numbers = range(1, 101) if page == 1 else (101,)
        response = Response()
        response.status_code = 200
        response._content = json.dumps({
            "samples": [dict(sample, id=number, short_label=f"S{number}") for number in numbers],
            "samples_count": 101,
        }).encode()
        if headers:
            response.headers.update({"X-Total": "101", "X-Total-Pages": "2"})
        return response

    monkeypatch.setattr(client.connection, "send", send)
    monkeypatch.setattr(client, "login", lambda: None)
    monkeypatch.setattr(api_client.ChemotionClient, "inventory_entry", lambda self, sample_id, *args: {"id": sample_id})
    monkeypatch.setattr(api_client.ChemotionClient, "login", lambda self: None)
    progress = []
    rows = client.inventory(42, synchronized=synchronized, progress=progress.append)
    assert pages == [1, 2]
    assert [row["id"] for row in rows] == list(range(101, 0, -1))
    assert "Retrieving the list of 101 samples…" in progress


@pytest.mark.parametrize("synchronized", [False, True])
@pytest.mark.parametrize("status", [204, 404, 405, 403, 500])
def test_library_removal_never_falls_back_or_retries(client, sample_server, monkeypatch, synchronized, status):
    sample, calls = sample_server
    original_send = client.connection.send
    deletes = []

    def send(request, **kwargs):
        path = urlsplit(request.url).path
        response = Response()
        response.request = request
        response.status_code = 200
        if path == "/api/v1/collections/roots.json":
            payload = {"collections": [{"id": 42, "label": "Inventory"}]}
        elif path == "/api/v1/collections/all":
            payload = {"collection": {"id": 1}}
        elif path == "/api/v1/syncCollections/sync_remote_roots":
            payload = {"syncCollections": [{"id": 42, "label": "Shared inventory"}]}
        elif request.method == "DELETE":
            deletes.append((path, json.loads(request.body)))
            response.status_code = status if len(deletes) == 1 else 204
            response._content = b"" if response.status_code == 204 else b'{"error":"test error"}'
            return response
        else:
            return original_send(request, **kwargs)
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(client.connection, "send", send)
    if status != 204:
        with pytest.raises(ConnectionError):
            client.remove_sample_from_collection(42, 7, synchronized=synchronized)
    else:
        client.remove_sample_from_collection(42, 7, synchronized=synchronized)
    assert deletes[0][0] == "/api/v1/collections/elements"
    state = deletes[0][1]["ui_state"]
    assert state["currentCollection"] == {"id": 42, "is_sync_to_me": synchronized}
    assert state["sample"]["checkedIds"] == [7]
    assert len(deletes) == 1
