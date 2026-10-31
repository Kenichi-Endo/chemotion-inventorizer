"""Chemotion API access and inventory retrieval."""
import json
import re
import tempfile
import threading
import time
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from urllib.parse import urlencode
from urllib.parse import urlsplit
from chemotion_api import Instance
from chemotion_api.connection import Connection, request_error
from chemotion_api.elements import ElementSet
from requests.exceptions import RequestException
from .inventory import (
    as_dict,
    chemical_cas,
    inventory_row,
    inventory_vendor,
    natural_sort_value,
    shown,
)


DEFAULT_CHEMOTION_URL = "https://"
CHEMOTION_URL = DEFAULT_CHEMOTION_URL
CHEMOTION_USERNAME = ""
CHEMOTION_PASSWORD = ""
_authentication_lock = threading.RLock()
_authentication_key = None
_authentication_token = None

# Keep concurrent inventory reads small enough to avoid TLS connection bursts.
MAX_CONCURRENT_SAMPLE_REQUESTS = 8
REQUEST_TIMEOUT_SECONDS = 45
TRANSIENT_REQUEST_ATTEMPTS = 3
# Retry transient server failures only for safe read requests.
TRANSIENT_SERVER_ERROR_CODES = frozenset((500, 502, 503, 504))


class InventoryLoadCancelled(Exception):
    """Raised internally when the user stops an in-progress inventory load."""
    def __init__(self, rows=None):
        self.rows = rows or []
        super().__init__()


def normalized_chemotion_url(value):
    """Return a safe Chemotion server root, suitable for API paths."""
    server = str(value or "").strip()
    if not server:
        raise ValueError("Enter the Chemotion server address.")
    server = server.rstrip("/")
    parts = urlsplit(server)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.path or parts.query or parts.fragment:
        raise ValueError("Enter a server address such as https://eln.example.org.")
    return server


class InventoryConnection(Connection):
    """chemotionapi transport with bounded reads and no automatic write retries."""

    def _send_request(self, method, url_path, default_header, kwargs):
        global _authentication_token
        # chemotionapi normally treats all paths as relative. Downloads and
        # thumbnails can supply absolute URLs; never send credentials off-site.
        target = urlsplit(url_path)
        if target.scheme or target.netloc:
            origin = urlsplit(self.host_url)
            if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
                raise ConnectionError("Chemotion returned a URL outside the configured server.")
            kwargs["url"] = url_path
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
        kwargs["headers"] = {**self.get_default_session_header(),
                             **kwargs.get("headers", default_header)}
        last_error = None
        for attempt in range(TRANSIENT_REQUEST_ATTEMPTS):
            try:
                response = super()._send_request(method, url_path, default_header, dict(kwargs))
                if response.status_code < 400:
                    # Older servers provide the total in JSON instead of the
                    # pagination headers consumed by chemotionapi's ElementSet.
                    if (method.lower() == "get" and target.path == "/api/v1/samples.json"
                            and ("X-Total" not in response.headers or "X-Total-Pages" not in response.headers)):
                        total = response.json().get("samples_count")
                        if total is not None:
                            per_page = int((kwargs.get("data") or {}).get("per_page", 100))
                            response.headers.setdefault("X-Total", str(total))
                            response.headers.setdefault("X-Total-Pages", str(max(1, (int(total) + per_page - 1) // per_page)))
                    return response
                error = request_error(response)
                response.close()
                if response.status_code == 401 and self.bearer_token is not None:
                    with _authentication_lock:
                        if self.bearer_token == _authentication_token:
                            _authentication_token = None
                if (response.status_code in TRANSIENT_SERVER_ERROR_CODES
                        and method.lower() in {"get", "head"}
                        and attempt + 1 < TRANSIENT_REQUEST_ATTEMPTS):
                    last_error = error
                    time.sleep(attempt + 1)
                    continue
                retries = (f" after {TRANSIENT_REQUEST_ATTEMPTS} attempts"
                           if last_error is not None else "")
                raise ConnectionError(f"Chemotion returned {response.status_code}{retries}: {error.snippet[:250]}") from error
            except (RequestException, ConnectionError) as error:
                if getattr(error.__cause__, "status_code", None) is not None:
                    raise
                last_error = error
                if method.lower() not in {"get", "head"}:
                    raise ConnectionError("Could not reach Chemotion; the write was not retried.") from error
                if attempt + 1 < TRANSIENT_REQUEST_ATTEMPTS:
                    time.sleep(attempt + 1)
        raise ConnectionError(f"Could not reach Chemotion after {TRANSIENT_REQUEST_ATTEMPTS} attempts: {last_error}") from last_error


class ChemotionClient:
    """Inventory operations over a chemotionapi authenticated connection."""
    def __init__(self):
        self.connection = InventoryConnection(CHEMOTION_URL)
        self.instance = Instance(self.connection)

    def request(self, path, data=None, headers=None, method=None):
        """Send exports, chemical metadata, images, and compatibility requests."""
        # Retain the byte-oriented inventory interface. chemotionapi serializes
        # non-string JSON data itself, so decode our already serialized bodies.
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        verb = (method or ("POST" if data is not None else "GET")).lower()
        if verb not in {"get", "post", "put", "patch", "delete"}:
            raise ValueError(f"Unsupported Chemotion request method: {verb}")
        with getattr(self.connection, verb)(path, data=data,
                                            headers=headers or {"Accept": "application/json"}) as response:
            return response.content

    def login(self):
        global _authentication_key, _authentication_token
        # Share only the token; each worker retains its own requests session.
        key = (CHEMOTION_URL, CHEMOTION_USERNAME, CHEMOTION_PASSWORD)
        with _authentication_lock:
            if key != _authentication_key:
                _authentication_key = None
                _authentication_token = None
            if _authentication_token is None:
                self.instance.login(CHEMOTION_USERNAME, CHEMOTION_PASSWORD)
                _authentication_key = key
                _authentication_token = self.connection.bearer_token
            else:
                self.connection.set_bearer_token(_authentication_token)
    def export_collection(self, collection, destination, timeout=900, queued=None):
        """Wait for a new export notification and save its ZIP atomically."""
        def messages():
            payload = json.loads(self.request("/api/v1/messages/list?is_ack=9"))
            return payload.get("messages") or []

        previous = {str(item.get("message_id", item.get("id"))) for item in messages()}
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        try:
            self.request("/api/v1/collections/export",
                         json.dumps({"collection_ids": [collection["id"]]}).encode(),
                         headers, method="POST")
        except ConnectionError as error:
            # Chemotion 2.x/3.x uses the plural route and different parameters.
            if getattr(error.__cause__, "status_code", None) != 404:
                raise
            self.request("/api/v1/collections/exports",
                         json.dumps({"collections": [collection["id"]], "format": "zip", "nested": False}).encode(),
                         headers, method="POST")
        if queued:
            queued()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for item in messages():
                if str(item.get("message_id", item.get("id"))) in previous:
                    continue
                content = as_dict(item.get("content"))
                data = str(content.get("data") or "")
                if str(collection["label"]) not in data or "Export" not in data:
                    continue
                if item.get("subject") == "Collection Import and Export Failure":
                    raise ConnectionError("Chemotion could not export this collection.")
                if item.get("subject") != "Collection Import and Export":
                    continue
                link = content.get("url") or ""
                parsed = urlsplit(link)
                server = urlsplit(CHEMOTION_URL)
                if (parsed.netloc and (parsed.scheme, parsed.netloc) != (server.scheme, server.netloc)) or not re.fullmatch(r"/zip/[\w-]+\.zip", parsed.path):
                    raise ConnectionError("Chemotion returned an unexpected export download link.")
                archive = self.request(link)
                if not zipfile.is_zipfile(io.BytesIO(archive)):
                    raise ConnectionError("Chemotion did not return a ZIP archive.")
                destination = Path(destination)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as output:
                        temporary = Path(output.name)
                        output.write(archive)
                    temporary.replace(destination)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return
            time.sleep(3)
        raise TimeoutError("The export is still pending. Check Chemotion notifications for its download link.")

    def collections(self):
        """Return selectable records from chemotionapi's own and shared trees."""
        root = self.instance.get_root_collection()
        records = [(root.all, False)]
        records.extend((collection.to_json(), False) for collection in root.children)
        records.extend((collection.to_json(), True) for collection in root.sync_root.children)
        found = {}
        def add(collection, synchronized=False, source_collection_id=None):
            if not isinstance(collection, dict) or collection.get("id") is None: return
            item = dict(collection); item["_synchronized"] = synchronized
            if synchronized:
                # Child entries use a sync-link ID, whereas Chemotion's
                # collection-elements endpoint requires the originating root.
                item["_source_collection_id"] = source_collection_id or item["id"]
            found[(item["id"], synchronized)] = item
            for child in collection.get("children") or []:
                add(child, synchronized, item.get("_source_collection_id"))
        for record, synchronized in records: add(record, synchronized)
        # Collection trees include grouping containers as well as the actual
        # collections.  A container has child collections but no inventory of
        # its own, so showing it in the picker produces empty results.  Keep
        # only leaves, while preserving every nested selectable collection.
        leaves = (item for item in found.values() if not item.get("children"))
        return sorted(leaves, key=lambda item: str(item.get("label") or "").casefold())
    def inventory(self, collection_id, synchronized=False, progress=None, cancelled=None, rows_loaded=None, sequential=False, row_ready=None):
        """Retrieve the inventory, reporting progress through ``progress``."""
        def check_cancelled():
            if cancelled and cancelled.is_set():
                raise InventoryLoadCancelled()

        check_cancelled()
        if progress: progress("Retrieving the selected Chemotion collection…")
        self.login(); check_cancelled(); samples = []
        # ElementSet handles pagination and synchronized collection queries;
        # retain raw listing records without triggering serial detail loads.
        elements = ElementSet(self.connection, {"name": "sample", "is_generic": False},
                              collection_id, collection_is_sync=synchronized)
        elements.load_elements(per_page=100)
        while True:
            check_cancelled()
            batch = [sample.json_data for sample in list.__iter__(elements)]
            samples.extend(batch)
            if progress: progress(f"Retrieving the list of {len(elements)} samples…")
            if elements.page >= elements.max_page or not batch: break
            check_cancelled()
            elements.next_page()
        # The compact list intentionally omits physical location and amount fields.
        # Fetching each sample serially is very slow, so use a small, bounded pool.
        # Each worker owns its authenticated chemotionapi session; mutable
        # sessions must not be shared between threads.
        if not samples: return []
        samples.sort(key=lambda sample: natural_sort_value(shown(sample.get("short_label"))), reverse=True)
        positions = {sample["id"]: position for position, sample in enumerate(samples)}
        rows = [None] * len(samples)
        worker_state = threading.local()
        def retrieve_sample(sample):
            check_cancelled()
            client = getattr(worker_state, "client", None)
            if client is None:
                client = ChemotionClient(); client.login(); worker_state.client = client
            check_cancelled()
            row = client.inventory_entry(sample["id"], collection_id, synchronized, sample)
            row["_inventory_position"] = positions[sample["id"]]
            # Retain retrieved details even if Stop interrupts thumbnail display.
            rows[positions[sample["id"]]] = row
            if row_ready:
                row_ready([row])
            return row

        workers = 1 if sequential else min(MAX_CONCURRENT_SAMPLE_REQUESTS, len(samples))
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sample-retrieval")
        try:
            initial_samples = samples[:1] if sequential else samples
            futures = {executor.submit(retrieve_sample, sample): position for position, sample in enumerate(initial_samples)}
            next_sample = 1
            completed = 0
            pending = set(futures)
            while pending:
                if cancelled and cancelled.is_set():
                    raise InventoryLoadCancelled([row for row in rows if row is not None])
                done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                batch_rows = []
                for future in done:
                    try:
                        rows[futures[future]] = future.result()
                    except InventoryLoadCancelled:
                        raise InventoryLoadCancelled([row for row in rows if row is not None])
                    batch_rows.append(rows[futures[future]])
                    completed += 1
                    if progress: progress(f"Retrieving sample information: {completed} of {len(samples)}…")
                if batch_rows and rows_loaded:
                    rows_loaded(batch_rows)
                if cancelled and cancelled.is_set():
                    raise InventoryLoadCancelled([row for row in rows if row is not None])
                if sequential and next_sample < len(samples):
                    future = executor.submit(retrieve_sample, samples[next_sample])
                    futures[future] = next_sample
                    pending.add(future)
                    next_sample += 1
        finally:
            if cancelled and cancelled.is_set():
                for future in (futures if "futures" in locals() else ()):
                    future.cancel()
            executor.shutdown(wait=not (cancelled and cancelled.is_set()), cancel_futures=True)
        return rows
    def inventory_entry(self, sample_id, collection_id=None, synchronized=False, fallback_sample=None):
        """Retrieve the complete, current display record for one sample."""
        detail = self.instance.get_sample(sample_id).json_data
        chemical = json.loads(self.request(f"/api/v1/chemicals?sample_id={sample_id}").decode("utf-8"))
        row = inventory_row(detail, inventory_vendor(chemical), collection_id, synchronized)
        row["chemical_data"] = as_dict(chemical).get("chemical_data") or []
        row["chemical_id"] = as_dict(chemical).get("id")
        row["chemical_cas"] = chemical_cas(chemical, detail)
        return row
    def set_location(self, sample_id, location, container, decoupled, inventory_sample):
        """Set one location while preserving flags defaulted by Chemotion's API."""
        self._save_sample(sample_id, {"location": location})
    def set_amount(self, sample_id, field, value, unit, container, decoupled, inventory_sample):
        """Update one amount while preserving the sample's other editable flags."""
        self._save_sample(sample_id, {field: {"value": str(value), "unit": unit}})
    def set_density(self, sample_id, density, container, decoupled, inventory_sample):
        """Update density while retaining the current container and sample flags."""
        self._save_sample(sample_id, {"density": density})
    def set_sample_flags(self, sample_id, container, decoupled, inventory_sample):
        """Update the two sample flags together, retaining the current container."""
        self._save_sample(sample_id, {"decoupled": decoupled, "inventory_sample": inventory_sample})

    def _save_sample(self, sample_id, properties, molecule_name_id=None):
        # Fetch current data before saving: GUI rows may be stale and save()
        # serializes the complete sample, including its container and flags.
        sample = self.instance.get_sample(sample_id)
        for key, value in properties.items():
            sample.properties[key] = value
        if molecule_name_id is not None:
            sample.json_data["molecule_name_id"] = molecule_name_id
        # chemotionapi normally copies the molecule molfile into the sample.
        # Inventory edits must retain the sample's own structure, even if null.
        if "molfile" in sample.json_data:
            sample.molecule["molfile"] = sample.json_data["molfile"]
        sample.save()
    def remove_sample_from_collection(self, collection_id, sample_id, synchronized=False, source_collection_id=None):
        """Remove one sample's membership in a collection without deleting it."""
        root = self.instance.get_root_collection(reload=False)
        tree = root.sync_root if synchronized else root
        matches = tree.find(id=collection_id)
        if not matches:
            raise ConnectionError("The collection is no longer available. Reload the collection list.")
        # A listing record identifies an existing sample without fetching it.
        elements = ElementSet(self.connection, {"name": "sample", "is_generic": False})
        sample = elements.new_element({"id": sample_id, "type": "sample", "molecule": {}})
        matches[0].remove_elements(sample)
        return {}
    def set_sample_name(self, sample_id, name, container, decoupled, inventory_sample):
        """Update a sample name without letting API defaults reset its flags."""
        self._save_sample(sample_id, {"name": name})
    def create_molecule_name(self, molecule_id, name):
        """Create a molecule-name option and return Chemotion's identifier for it."""
        query = urlencode({"id": molecule_id, "new_name": name})
        names = json.loads(self.request(f"/api/v1/molecules/names?{query}").decode("utf-8")).get("molecules") or []
        match = next((item for item in names if item.get("label") == name), None)
        if not match or match.get("value") is None: raise ConnectionError("Chemotion did not return the new molecule name.")
        return match["value"]
    def set_molecule_name(self, sample_id, molecule_name_id, container, decoupled, inventory_sample):
        """Select a molecule name for one sample while preserving its flags."""
        self._save_sample(sample_id, {}, molecule_name_id=molecule_name_id)
    def set_vendor(self, sample_id, chemical_data, vendor, chemical_id=None, cas=""):
        """Set a vendor, creating Chemotion's chemical record when it is absent."""
        data = json.loads(json.dumps(chemical_data)) if chemical_data else [{}]
        if not isinstance(data[0], dict): data[0] = {}
        data[0]["vendor"] = vendor
        payload = {"sample_id": sample_id, "chemical_data": data}
        path = "/api/v1/chemicals"
        method = "PUT"
        if chemical_id is None:
            # GET /chemicals returns an unsaved object (id: null) for entries
            # such as iNV-173 that do not yet have any inventory metadata.
            # Such an object must be created, not updated.
            path = "/api/v1/chemicals/create"
            payload["cas"] = cas or ""
            method = "POST"
        body = json.dumps(payload).encode("utf-8")
        self.request(path, body,
                     {"Accept": "application/json", "Content-Type": "application/json"}, method=method)

