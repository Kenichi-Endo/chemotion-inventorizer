"""Collection export notification matching and safe archive saving."""
from chemotion_inventorizer import client as api_client
import io
import json
import zipfile
from urllib.error import HTTPError

import pytest

from test_amount_display import app


@pytest.mark.parametrize("result", ["zip", "html", "foreign", "failure"])
def test_export_download(tmp_path, monkeypatch, result):
    monkeypatch.setattr(api_client, "CHEMOTION_URL", "https://eln.example.org")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("collection.json", "{}")
    link = "https://eln.example.org/zip/job-123.zip"
    if result == "foreign":
        link = "https://other.example.org/zip/job-123.zip"
    notification = {"message_id": 2, "subject": "Collection Import and Export",
                    "content": {"data": "Export Inventory", "url": link}}
    if result == "failure":
        notification["subject"] += " Failure"
    calls = []
    old = dict(notification, message_id=1)

    def request(path, data=None, headers=None, method=None):
        calls.append((path, data, method))
        if "messages/list" in path:
            return json.dumps({"messages": [old] if len(calls) == 1 else [old, notification]}).encode()
        if path.endswith("/export"):
            assert json.loads(data) == {"collection_ids": [7]}
            assert method == "POST"
            return b'{}'
        assert path == link
        return b"<html>Sign in</html>" if result == "html" else archive.getvalue()

    client = api_client.ChemotionClient()
    client.request = request
    destination = tmp_path / "export.zip"
    destination.write_bytes(b"existing")
    if result == "zip":
        client.export_collection({"id": 7, "label": "Inventory"}, destination)
        assert destination.read_bytes() == archive.getvalue()
    else:
        with pytest.raises(ConnectionError):
            client.export_collection({"id": 7, "label": "Inventory"}, destination)
        assert destination.read_bytes() == b"existing"
    assert not list(tmp_path.glob("*.part"))


def test_export_timeout_preserves_file(tmp_path):
    client = api_client.ChemotionClient()
    client.request = lambda *args, **kwargs: b'{"messages": []}'
    destination = tmp_path / "export.zip"
    with pytest.raises(TimeoutError):
        client.export_collection({"id": 7, "label": "Inventory"}, destination, timeout=0)
    assert not destination.exists()


@pytest.mark.parametrize("status", [404, 403, 500])
def test_legacy_export_route_only_on_not_found(tmp_path, status):
    calls = []

    def request(path, data=None, headers=None, method=None):
        calls.append(path)
        if "messages/list" in path:
            return b'{"messages": []}'
        if path.endswith("/export"):
            raise ConnectionError("Export failed") from HTTPError(path, status, "Error", {}, None)
        assert path.endswith("/exports")
        assert json.loads(data) == {"collections": [7], "format": "zip", "nested": False}
        assert method == "POST"

    client = api_client.ChemotionClient()
    client.request = request
    with pytest.raises(TimeoutError if status == 404 else ConnectionError):
        client.export_collection({"id": 7, "label": "Inventory"}, tmp_path / "export.zip", timeout=0)
    assert any(path.endswith("/exports") for path in calls) == (status == 404)
