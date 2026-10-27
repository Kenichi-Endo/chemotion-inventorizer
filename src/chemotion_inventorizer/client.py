"""Chemotion API access and inventory retrieval."""
import json
import re
import socket
import ssl
import tempfile
import threading
import time
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener
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


class ChemotionClient:
    """Authenticated client for the Chemotion REST API."""
    def __init__(self):
        # The framework Python used by the macOS launcher may not inherit the
        # system trust store.  Windows' Python normally does, so use its
        # platform default when the macOS certificate bundle is unavailable.
        certificate_bundle = "/etc/ssl/cert.pem"
        context = (ssl.create_default_context(cafile=certificate_bundle)
                   if Path(certificate_bundle).is_file()
                   else ssl.create_default_context())
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()), HTTPSHandler(context=context))
    def request(self, path, data=None, headers=None, method=None):
        target = path if urlsplit(path).scheme in {"http", "https"} else CHEMOTION_URL + path
        request = Request(target, data=data,
                          headers=headers or {"Accept": "application/json"}, method=method)
        last_error = None
        for attempt in range(TRANSIENT_REQUEST_ATTEMPTS):
            try:
                with self.opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    return response.read()
            except HTTPError as error:
                # Retrying writes could cause a duplicate or unexpected edit if
                # Chemotion applied the request before returning its error.
                is_safe_read = request.get_method() in {"GET", "HEAD"}
                if (error.code in TRANSIENT_SERVER_ERROR_CODES and is_safe_read
                        and attempt + 1 < TRANSIENT_REQUEST_ATTEMPTS):
                    last_error = error
                    time.sleep(attempt + 1)
                    continue
                body = error.read().decode("utf-8", "replace")[:250]
                retries = (f" after {TRANSIENT_REQUEST_ATTEMPTS} attempts"
                           if last_error is not None else "")
                raise ConnectionError(f"Chemotion returned {error.code}{retries}: {body}") from error
            except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as error:
                last_error = error
                if attempt + 1 < TRANSIENT_REQUEST_ATTEMPTS:
                    time.sleep(attempt + 1)
        reason = getattr(last_error, "reason", last_error)
        raise ConnectionError(f"Could not reach Chemotion after {TRANSIENT_REQUEST_ATTEMPTS} attempts: {reason}") from last_error
    def login(self):
        page = self.request("/users/sign_in", headers={"Accept": "text/html"}).decode("utf-8", "replace")
        token = re.search(r'name="csrf-token" content="([^"]+)"', page)
        if not token: raise ConnectionError("Chemotion did not provide a login security token.")
        body = urlencode({"user[login]": CHEMOTION_USERNAME, "user[password]": CHEMOTION_PASSWORD, "user[remember_me]": "0"}).encode()
        self.request("/users/sign_in", body, {"Accept": "text/html", "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": token.group(1)})
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
            if not isinstance(error.__cause__, HTTPError) or error.__cause__.code != 404:
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
        """Return normal (editable) collection records available to this account."""
        records = []
        try:
            # Chemotion ELN 3.x combines own and remotely available
            # collections under this single endpoint.
            payload = json.loads(self.request("/api/v1/collections").decode("utf-8"))
            if "own" in payload or "shared_with_me" in payload:
                records.extend((item, False) for item in (payload.get("own") or []))
                records.extend((item, True) for item in (payload.get("shared_with_me") or []))
            else:
                raise ConnectionError("Chemotion returned an unrecognized collection list.")
        except ConnectionError as modern_error:
            # Older Chemotion versions expose the collection trees separately
            # and may reject this newer GET route with either 404 or 405.
            # Authentication and other server failures should still surface.
            if not any(code in str(modern_error) for code in ("404", "405")):
                raise
            for path in ("/api/v1/collections/locked.json", "/api/v1/collections/roots.json", "/api/v1/collections/shared_roots.json"):
                payload = json.loads(self.request(path).decode("utf-8"))
                records.extend((item, False) for item in (payload.get("collections") or []))
        # Synchronized collections use a different sample query and browser
        # route. They may be unavailable for some accounts, so do not let a
        # permissions error hide normal collections.
        try:
            payload = json.loads(self.request("/api/v1/syncCollections/sync_remote_roots.json").decode("utf-8"))
            records.extend((item, True) for item in (payload.get("syncCollections") or payload.get("sync_collections") or []))
        except ConnectionError: pass
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
        if progress: progress("Signing in to Chemotion…")
        self.login(); check_cancelled(); samples = []; page = 1; total = None
        while True:
            check_cancelled()
            parameter = "sync_collection_id" if synchronized else "collection_id"
            result = json.loads(self.request(f"/api/v1/samples?{parameter}={collection_id}&per_page=100&page={page}").decode("utf-8")); batch = result.get("samples") or []; samples += batch
            total = int(result.get("samples_count", 0))
            if progress: progress(f"Retrieving the list of {total} samples…")
            if len(samples) >= total or not batch: break
            page += 1
        # The compact list intentionally omits physical location and amount fields.
        # Fetching each sample serially is very slow, so use a small, bounded pool.
        # Each worker owns its authenticated client because urllib cookie jars are not
        # designed to be shared between threads.
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
        detail = json.loads(self.request(f"/api/v1/samples/{sample_id}").decode("utf-8")).get("sample", fallback_sample or {})
        chemical = json.loads(self.request(f"/api/v1/chemicals?sample_id={sample_id}").decode("utf-8"))
        row = inventory_row(detail, inventory_vendor(chemical), collection_id, synchronized)
        row["chemical_data"] = as_dict(chemical).get("chemical_data") or []
        row["chemical_id"] = as_dict(chemical).get("id")
        row["chemical_cas"] = chemical_cas(chemical, detail)
        return row
    def set_location(self, sample_id, location, container, decoupled, inventory_sample):
        """Set one location while preserving flags defaulted by Chemotion's API."""
        body = json.dumps({"location": location, "container": container,
                           "decoupled": decoupled, "inventory_sample": inventory_sample}).encode("utf-8")
        self.request(f"/api/v1/samples/{sample_id}", body,
                     {"Accept": "application/json", "Content-Type": "application/json"}, method="PUT")
    def set_amount(self, sample_id, field, value, unit, container, decoupled, inventory_sample):
        """Update one amount while preserving the sample's other editable flags."""
        body = json.dumps({f"{field}_value": str(value), f"{field}_unit": unit, "container": container,
                           "decoupled": decoupled, "inventory_sample": inventory_sample}).encode("utf-8")
        self.request(f"/api/v1/samples/{sample_id}", body,
                     {"Accept": "application/json", "Content-Type": "application/json"}, method="PUT")
    def set_density(self, sample_id, density, container, decoupled, inventory_sample):
        """Update density while retaining the current container and sample flags."""
        body = json.dumps({"density": density, "container": container,
                           "decoupled": decoupled, "inventory_sample": inventory_sample}).encode("utf-8")
        self.request(f"/api/v1/samples/{sample_id}", body,
                     {"Accept": "application/json", "Content-Type": "application/json"}, method="PUT")
    def set_sample_flags(self, sample_id, container, decoupled, inventory_sample):
        """Update the two sample flags together, retaining the current container."""
        body = json.dumps({"container": container, "decoupled": decoupled,
                           "inventory_sample": inventory_sample}).encode("utf-8")
        self.request(f"/api/v1/samples/{sample_id}", body,
                     {"Accept": "application/json", "Content-Type": "application/json"}, method="PUT")
    def remove_sample_from_collection(self, collection_id, sample_id, synchronized=False, source_collection_id=None):
        """Remove one sample's membership in a collection without deleting it."""
        if synchronized:
            # Chemotion 2.x handles a synchronized collection through its sync
            # link ID.  Its UI sends this legacy endpoint and flag, which let
            # the server resolve the original collection with the recipient's
            # permissions.
            payload = {
                "ui_state": {
                    "currentCollection": {"id": collection_id, "is_sync_to_me": True},
                    "sample": {"checkedAll": False, "checkedIds": [sample_id], "uncheckedIds": []},
                },
            }
            response = self.request(
                "/api/v1/collections/elements/", json.dumps(payload).encode("utf-8"),
                {"Accept": "application/json", "Content-Type": "application/json"}, method="DELETE")
            return json.loads(response.decode("utf-8")) if response else {}
        payload = {
            "collection_id": collection_id,
            "ui_state": {
                "currentCollection": {"id": collection_id},
                "sample": {"checkedAll": False, "checkedIds": [sample_id], "uncheckedIds": []},
            },
        }
        response = self.request(
            f"/api/v1/collection_elements/{collection_id}", json.dumps(payload).encode("utf-8"),
            {"Accept": "application/json", "Content-Type": "application/json"}, method="DELETE")
        return json.loads(response.decode("utf-8")) if response else {}
    def set_sample_name(self, sample_id, name, container, decoupled, inventory_sample):
        """Update a sample name without letting API defaults reset its flags."""
        body = json.dumps({"name": name, "container": container, "decoupled": decoupled,
                           "inventory_sample": inventory_sample}).encode("utf-8")
        self.request(f"/api/v1/samples/{sample_id}", body,
                     {"Accept": "application/json", "Content-Type": "application/json"}, method="PUT")
    def create_molecule_name(self, molecule_id, name):
        """Create a molecule-name option and return Chemotion's identifier for it."""
        query = urlencode({"id": molecule_id, "new_name": name})
        names = json.loads(self.request(f"/api/v1/molecules/names?{query}").decode("utf-8")).get("molecules") or []
        match = next((item for item in names if item.get("label") == name), None)
        if not match or match.get("value") is None: raise ConnectionError("Chemotion did not return the new molecule name.")
        return match["value"]
    def set_molecule_name(self, sample_id, molecule_name_id, container, decoupled, inventory_sample):
        """Select a molecule name for one sample while preserving its flags."""
        body = json.dumps({"molecule_name_id": molecule_name_id, "container": container,
                           "decoupled": decoupled, "inventory_sample": inventory_sample}).encode("utf-8")
        self.request(f"/api/v1/samples/{sample_id}", body,
                     {"Accept": "application/json", "Content-Type": "application/json"}, method="PUT")
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

