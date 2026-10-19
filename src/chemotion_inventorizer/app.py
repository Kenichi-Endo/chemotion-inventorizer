#!/usr/bin/env python3
"""Native desktop viewer for the Chemotion chemical inventory."""
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import webbrowser
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
from http.cookiejar import CookieJar
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

DEFAULT_CHEMOTION_URL = "https://"
CHEMOTION_URL = DEFAULT_CHEMOTION_URL
CHEMOTION_USERNAME = ""
CHEMOTION_PASSWORD = ""
KEYCHAIN_SERVICE = "de.endo-group.chemotion-inventorizer"
WINDOWS_CREDENTIAL_TARGET = KEYCHAIN_SERVICE


def multi_selection_modifier(windowing_system):
    """Return the platform's standard modifier for extending a selection."""
    return "Command" if windowing_system == "aqua" else "Control"


def prevent_combobox_scroll_selection(_event=None):
    """Keep the focused combobox value unchanged while the user scrolls."""
    return "break"


def disable_combobox_scroll_selection(combobox):
    """Prevent wheel events from selecting a different combobox item."""
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        combobox.bind(sequence, prevent_combobox_scroll_selection)


def profile_settings_path():
    """Return the per-user settings location for the current platform."""
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        if app_data:
            return Path(app_data) / "Chemotion Inventorizer" / "settings.json"
        return Path.home() / "AppData" / "Roaming" / "Chemotion Inventorizer" / "settings.json"
    return Path.home() / "Library" / "Application Support" / "Chemotion Inventorizer" / "settings.json"


PROFILE_SETTINGS_PATH = profile_settings_path()
# Chemotion and its reverse proxy can refuse or time out TLS handshakes when a
# fresh inventory load opens many sessions at once.  A small pool remains much
# faster than serial retrieval without creating a login/connection burst.
MAX_CONCURRENT_SAMPLE_REQUESTS = 8
REQUEST_TIMEOUT_SECONDS = 45
TRANSIENT_REQUEST_ATTEMPTS = 3
# A 5xx response means Chemotion (or its reverse proxy) accepted the request
# but could not finish it.  These are commonly transient while an inventory is
# being read, so retry safe read requests before giving up.
TRANSIENT_SERVER_ERROR_CODES = frozenset((500, 502, 503, 504))
PRINT_CHECKLIST_KEYS = ("correct", "incorrect")
PRINT_CHECKLIST_HEADINGS = ("Correct", "Incorrect")

class InventoryLoadCancelled(Exception):
    """Raised internally when the user stops an in-progress inventory load."""
    def __init__(self, rows=None):
        self.rows = rows or []
        super().__init__()

def as_dict(value): return value if isinstance(value, dict) else {}
def shown(value): return "—" if value is None or value == "" else str(value)

def synchronized_collection_source(collection):
    """Return the account Chemotion identifies as the source of a sync collection."""
    # A sync record's ``user`` is the receiving account (the signed-in user),
    # whereas ``sharer`` / ``shared_by`` identifies the account which made the
    # original collection available.  The latter must win when both are sent.
    # The exact spelling has varied between Chemotion ELN releases.
    for key in ("owner_name", "source_name", "synchronized_by", "shared_by", "sharer"):
        value = collection.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Chemotion 3.x sends remote collection provenance as, for example,
    # ``First Last (flast)``.  Keep the picker compact by using the account
    # abbreviation in the trailing parentheses.
    owner = collection.get("owner")
    if isinstance(owner, str) and owner.strip():
        abbreviation = re.search(r"\(([^()]+)\)\s*$", owner)
        return abbreviation.group(1).strip() if abbreviation else owner.strip()
    for key in ("sharer", "shared_by", "owner", "source", "creator", "created_by", "sync_user"):
        person = as_dict(collection.get(key))
        # Chemotion's name abbreviation is the concise account code shown in
        # the ELN and is clearer than a person's full name in this picker.
        # Legacy Chemotion calls this field ``initials`` in its synchronized
        # collection response, even though it contains the user code.
        for name_key in ("initials", "name_abbreviation", "login", "username", "name", "email"):
            value = person.get(name_key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        full_name = " ".join(str(person.get(name_key)).strip() for name_key in ("first_name", "last_name")
                             if person.get(name_key) is not None and str(person.get(name_key)).strip())
        if full_name:
            return full_name
    return None

def collection_picker_label(collection):
    """Build the collection selector label, including a sync collection's source."""
    label = shown(collection.get("label"))
    if not collection.get("_synchronized"):
        return label
    source = synchronized_collection_source(collection)
    return f"({source}) {label}" if source else f"(Synchronized) {label}"

def saved_profile_settings():
    """Read display preferences saved for the signed-in Mac user."""
    try:
        with PROFILE_SETTINGS_PATH.open(encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}

def save_profile_settings(settings):
    """Persist non-sensitive app preferences in the Mac user's profile."""
    try:
        PROFILE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    except OSError:
        # Preferences are optional; an unavailable profile directory must not
        # interrupt use of the inventory.
        pass

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

def visible_columns(saved_columns, all_columns):
    """Return a safe, ordered selection of columns from saved preferences."""
    # Settings loaded from JSON use lists, while a layout that has just been
    # accepted in the print dialog uses a tuple.  Accept both so the PDF made
    # immediately after pressing Print uses exactly the selected columns.
    if not isinstance(saved_columns, (list, tuple)):
        return tuple(all_columns)
    selected = tuple(key for key in saved_columns if key in all_columns)
    return selected or tuple(all_columns)

def column_order(saved_order, all_columns):
    """Return a safe complete column order, retaining any new columns."""
    if not isinstance(saved_order, (list, tuple)):
        return tuple(all_columns)
    ordered = tuple(dict.fromkeys(key for key in saved_order if key in all_columns))
    for key in all_columns:
        if key not in ordered:
            if key in {"density", "structure"} and "molecule" in ordered:
                position = ordered.index("molecule") + 1
                if key == "structure":
                    position -= 1
                ordered = ordered[:position] + (key,) + ordered[position:]
            else:
                ordered += (key,)
    return ordered

def print_preset(saved_preset, all_columns):
    """Return a safe, independent set of saved print-layout preferences."""
    preset = as_dict(saved_preset)
    default_columns = ("location", "label", "molecule", "sample", "amount") + PRINT_CHECKLIST_KEYS
    order = column_order(preset.get("column_order"), all_columns)
    visible = visible_columns(preset.get("visible_columns"), order)
    if not isinstance(saved_preset, dict):
        visible = tuple(key for key in default_columns if key in all_columns)
    elif not any(key in preset.get("column_order", ()) for key in PRINT_CHECKLIST_KEYS):
        # Upgrade the first layout saved before the checklist columns existed,
        # while preserving later intentional choices to hide either column.
        visible += tuple(key for key in PRINT_CHECKLIST_KEYS if key in all_columns and key not in visible)
    split_column = preset.get("split_column")
    if not isinstance(saved_preset, dict) and "location" in all_columns:
        split_column = "location"
    if split_column not in all_columns:
        split_column = None
    try:
        split_threshold = max(1, int(preset.get("split_threshold", 10)))
    except (TypeError, ValueError):
        split_threshold = 10
    return {"column_order": order, "visible_columns": visible,
            "split_column": split_column, "split_threshold": split_threshold}

def parse_localized_last_modified(value):
    """Parse Chemotion's localized German date/time representation, if present."""
    raw_value = str(value).strip().replace("\u00a0", " ")
    # Chemotion can localize this value before returning it. Match the visible
    # German form first, tolerating a non-breaking space and trailing locale or
    # time-zone text.
    localized = re.match(
        r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s*,?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
        raw_value,
    )
    if not localized:
        return None
    day, month, year, hour, minute, second = localized.groups()
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))

def format_last_modified(value):
    """Show Chemotion's modification timestamp in ISO date order with a space."""
    if value is None or value == "":
        return "—"
    raw_value = str(value).strip()
    try:
        timestamp = parse_last_modified(raw_value)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone()
        return timestamp.isoformat(sep=" ", timespec="seconds")
    except ValueError:
        # Retain an unexpected API value instead of hiding it. Where its
        # separator is the only variation, retain the app's space separator.
        return re.sub(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}(?::\d{2})?)", r"\1 \2", raw_value)

def displayed_row_value(row, key):
    """Return the final display value for one inventory field."""
    if key == "structure":
        return ""
    if key == "density":
        value = row.get(key)
        return "" if value is None or str(value).strip() in {"", "—"} or is_zero_amount(value) else str(value)
    if key == "last_modified":
        return format_last_modified(row.get("last_modified_at", row.get(key)))
    return shown(row.get(key))

def structure_image_request_path(source, directory="molecules"):
    """Resolve a sample or molecule SVG filename to its public image path."""
    source = str(source)
    if "<svg" in source[:4096].lower():
        return source
    if urlsplit(source).scheme in {"http", "https"}:
        return source
    path = source.lstrip("/")
    if "/" in path:
        return "/" + path
    return f"/images/{directory}/" + quote(path, safe="")

def parse_last_modified(value):
    """Convert the timestamp returned by Chemotion into a sortable datetime."""
    timestamp = str(value).strip()
    # Python 3.10 requires a colon in numeric UTC offsets.
    timestamp = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", timestamp)
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        localized = parse_localized_last_modified(timestamp)
        if localized is not None:
            return localized
        raise

def last_modified_sort_value(value):
    """Return a chronological sort key, with absent or malformed dates last."""
    if value is None or value == "":
        return (1, 0)
    try:
        return (0, parse_last_modified(value).timestamp())
    except (ValueError, OSError):
        return (1, 0)

def natural_sort_value(value):
    """Sort embedded numbers numerically while retaining text order."""
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", str(value)))

def windows_saved_credentials():
    """Read credentials from the current Windows user's Credential Manager."""
    # Keep the import local: ctypes exposes ``windll`` only on Windows.
    import ctypes
    from ctypes import wintypes
    credential_api = ctypes.WinDLL("Advapi32", use_last_error=True)

    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                   ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                   ("LastWritten", ctypes.c_byte * 8), ("CredentialBlobSize", wintypes.DWORD),
                   ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                   ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                   ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                   ("UserName", wintypes.LPWSTR)]

    credential_pointer = ctypes.POINTER(Credential)()
    credential_type_generic = 1
    if not credential_api.CredReadW(WINDOWS_CREDENTIAL_TARGET, credential_type_generic, 0,
                                    ctypes.byref(credential_pointer)):
        return "", ""
    try:
        credential = credential_pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return credential.UserName or "", blob.decode("utf-16-le")
    except (UnicodeDecodeError, ValueError):
        return "", ""
    finally:
        credential_api.CredFree(credential_pointer)


def windows_save_credentials(username, password):
    """Store credentials in the current Windows user's Credential Manager."""
    import ctypes
    from ctypes import wintypes
    credential_api = ctypes.WinDLL("Advapi32", use_last_error=True)

    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                   ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                   ("LastWritten", ctypes.c_byte * 8), ("CredentialBlobSize", wintypes.DWORD),
                   ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                   ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                   ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                   ("UserName", wintypes.LPWSTR)]

    encoded_password = password.encode("utf-16-le")
    password_blob = ctypes.create_string_buffer(encoded_password)
    credential = Credential(Type=1, TargetName=WINDOWS_CREDENTIAL_TARGET,
                            CredentialBlobSize=len(encoded_password),
                            CredentialBlob=ctypes.cast(password_blob, ctypes.POINTER(ctypes.c_byte)),
                            Persist=2, UserName=username)
    if not credential_api.CredWriteW(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise RuntimeError(f"The credentials could not be saved in Windows Credential Manager (error {error}).")


def windows_clear_credentials():
    """Remove the app credential from the current Windows user's Credential Manager."""
    import ctypes
    credential_api = ctypes.WinDLL("Advapi32", use_last_error=True)
    if not credential_api.CredDeleteW(WINDOWS_CREDENTIAL_TARGET, 1, 0):
        # ERROR_NOT_FOUND means there was simply no credential left to remove.
        error = ctypes.get_last_error()
        if error != 1168:
            raise RuntimeError(f"The credentials could not be removed from Windows Credential Manager (error {error}).")


def saved_credentials():
    """Read the app's saved credentials from the platform's secure credential store."""
    if os.name == "nt":
        try:
            return windows_saved_credentials()
        # A missing or unavailable credential must never prevent the app from opening.
        except Exception:
            return "", ""
    if os.name != "posix" or not Path("/usr/bin/security").is_file():
        return "", ""
    try:
        result = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-g"], capture_output=True, text=True, timeout=3)
        account = re.search(r'"acct"<blob>="([^"]*)"', result.stdout)
        password = re.search(r'password: "(.*)"', result.stderr)
        if result.returncode == 0 and account and password: return account.group(1), password.group(1)
    # A missing, locked, or unavailable Keychain must never stop the app from
    # opening; the user can still enter credentials manually.
    except Exception: pass
    return "", ""

def save_credentials(username, password):
    """Store one current set of credentials in the platform's secure credential store."""
    if os.name == "nt":
        windows_save_credentials(username, password)
        return
    if os.name != "posix" or not Path("/usr/bin/security").is_file():
        return
    previous_username, _previous_password = saved_credentials()
    if previous_username and previous_username != username:
        # Keychain identifies generic passwords by both service and account.
        # Remove the former account so later launches cannot retrieve stale
        # credentials when the username has changed.
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", previous_username], capture_output=True, text=True, timeout=5)
    result = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", username, "-w", password], capture_output=True, text=True, timeout=5)
    if result.returncode != 0: raise RuntimeError(result.stderr.strip() or "The credentials could not be saved in Keychain.")


def clear_credentials():
    """Remove the app's saved credentials from the platform's secure store."""
    if os.name == "nt":
        windows_clear_credentials()
        return
    if os.name != "posix" or not Path("/usr/bin/security").is_file():
        return
    result = subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE],
                            capture_output=True, text=True, timeout=5)
    # Keychain's "not found" failure is harmless; any other failure is useful to report.
    if result.returncode != 0 and "could not be found" not in result.stderr.casefold():
        raise RuntimeError(result.stderr.strip() or "The credentials could not be removed from Keychain.")

def format_amount(value):
    """Format an amount exactly as supplied; never round it for display or print."""
    try:
        number = Decimal(str(value))
        if not number.is_finite(): return shown(value)
        # ``.3g`` rounds values below one (for example 0.0025 can become
        # 0.003).  Amount strings are also used by the PDF checklist, where a
        # rounded-up quantity is unsafe.  Decimal's fixed-point normalized
        # representation removes insignificant zeroes without changing value.
        return format(number.normalize(), "f")
    except (InvalidOperation, ValueError):
        return shown(value)

def format_amount_with_unit(value, unit):
    """Use practical metric units when displaying normalized amounts."""
    display_unit = "L" if str(unit or "").strip().casefold() == "l" else shown(unit)
    try:
        number = Decimal(str(value))
        if not number.is_finite(): return f"{format_amount(value)} {display_unit}".strip()
        normalized_unit = str(unit or "").strip().casefold()
        if 0 < number < 1 and normalized_unit in {"g", "gram", "grams"}:
            return f"{format_amount(number * 1000)} mg"
        if number >= 1000 and normalized_unit in {"g", "gram", "grams"}:
            return f"{format_amount(number / 1000)} kg"
        if 0 < number < 1 and normalized_unit in {"l", "liter", "liters", "litre", "litres"}:
            return f"{format_amount(number * 1000)} mL"
    except (InvalidOperation, ValueError):
        pass
    return f"{format_amount(value)} {display_unit}".strip()

AMOUNT_UNITS = {
    "g": ("g", Decimal("1")), "gram": ("g", Decimal("1")), "grams": ("g", Decimal("1")),
    "mg": ("g", Decimal("0.001")), "milligram": ("g", Decimal("0.001")), "milligrams": ("g", Decimal("0.001")),
    "kg": ("g", Decimal("1000")), "kilogram": ("g", Decimal("1000")), "kilograms": ("g", Decimal("1000")),
    # Chemotion uses a lowercase ``l`` to identify volume as the calculation
    # basis.  It accepts uppercase ``L`` but then treats the value as a mass
    # basis in its sample-property calculations.
    "l": ("l", Decimal("1")), "liter": ("l", Decimal("1")), "liters": ("l", Decimal("1")), "litre": ("l", Decimal("1")), "litres": ("l", Decimal("1")),
    "ml": ("l", Decimal("0.001")), "milliliter": ("l", Decimal("0.001")), "milliliters": ("l", Decimal("0.001")), "millilitre": ("l", Decimal("0.001")), "millilitres": ("l", Decimal("0.001")),
    "ul": ("l", Decimal("0.000001")), "µl": ("l", Decimal("0.000001")), "μl": ("l", Decimal("0.000001")),
}

def parse_amount(value):
    """Parse a user amount and return its value normalized to Chemotion's g/L units."""
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-zµμ]+)\s*", value)
    if not match:
        raise ValueError("Enter an amount and unit, for example 250 mg or 1.5 mL.")
    try:
        number = Decimal(match.group(1).replace(",", "."))
    except InvalidOperation as error:
        raise ValueError("The amount must be a number.") from error
    if not number.is_finite() or number < 0:
        raise ValueError("The amount must be a non-negative finite number.")
    unit = AMOUNT_UNITS.get(match.group(2).casefold())
    if not unit:
        raise ValueError("Use a mass unit (g, mg, kg) or volume unit (L, mL, µL).")
    normalized_unit, multiplier = unit
    return number * multiplier, normalized_unit

def is_zero_amount(value):
    try:
        return Decimal(str(value)).is_finite() and Decimal(str(value)) == 0
    except (InvalidOperation, ValueError):
        return False

def inventory_vendor(chemical):
    for details in as_dict(chemical).get("chemical_data") or []:
        if isinstance(details, dict) and details.get("vendor"):
            return details["vendor"]
    return None

def chemical_cas(chemical, sample):
    """Return the CAS value Chemotion needs when creating a chemical record."""
    for source in (as_dict(chemical), as_dict(sample).get("xref"), as_dict(sample).get("molecule")):
        value = source.get("cas")
        if isinstance(value, list):
            value = next((item for item in value if item), None)
        if value is not None and str(value).strip():
            return str(value)
    # Chemotion's own inventory editor creates chemical records with an empty
    # CAS value when the sample has none, so retain that behaviour here.
    return ""

def editable_collection_id(sample):
    """Return the regular collection ID used by Chemotion's editable sample page."""
    direct_id = sample.get("collection_id")
    if direct_id is not None: return direct_id
    collection = as_dict(sample.get("collection"))
    if collection.get("id") is not None: return collection["id"]
    collections = sample.get("collections") or []
    if isinstance(collections, list):
        for item in collections:
            if isinstance(item, dict) and item.get("id") is not None:
                return item["id"]
    return None

def inventory_row(sample, vendor=None, collection_id=None, synchronized=False):
    molecule = as_dict(sample.get("molecule")); names = molecule.get("names") or []
    sample_svg = sample.get("sample_svg_file")
    if sample_svg and sample_svg != "***":
        structure = structure_image_request_path(sample_svg, "samples")
    else:
        molecule_svg = molecule.get("molecule_svg_file") or molecule.get("svg_file")
        structure = (structure_image_request_path(molecule_svg) if molecule_svg else
                     "/images/wild_card/no_image_180.svg" if sample_svg == "***" else "")
    formula = molecule.get("sum_formular") or molecule.get("molecular_formula")
    first_molecule_name = next((candidate for candidate in names if candidate), None)
    name = sample.get("showed_name") or sample.get("name") or sample.get("external_label") or molecule.get("iupac_name") or first_molecule_name or formula or "—"
    amount = "—"; amount_field = None; amount_value = None; amount_unit = None
    for prefix in ("real_amount", "target_amount"):
        if sample.get(f"{prefix}_value") is not None:
            amount_field = prefix; amount_value = sample[f"{prefix}_value"]; amount_unit = sample.get(f"{prefix}_unit")
            amount = "" if is_zero_amount(amount_value) else format_amount_with_unit(amount_value, amount_unit); break
    container = as_dict(sample.get("container"))
    collection_id = collection_id or editable_collection_id(sample)
    molecule_name = as_dict(sample.get("molecule_name_hash"))
    return {"id": sample.get("id"), "container": sample.get("container"), "collection_id": collection_id, "synchronized": synchronized, "label": shown(sample.get("short_label")),
            "molecule": name, "density": shown(sample.get("density")), "sample": shown(sample.get("name")),
            "molecule_id": molecule.get("id"), "molecule_name_id": molecule_name.get("value"),
            "structure": structure,
            "vendor": shown(vendor or sample.get("vendor") or sample.get("supplier")), "amount": amount,
            "amount_field": amount_field, "amount_value": amount_value, "amount_unit": amount_unit,
            # Chemotion's sample-update endpoint applies its default value of
            # false to these optional flags when they are not sent.  Keep the
            # values returned by the detail request so focused edits below do
            # not turn either option off.
            "decoupled": sample.get("decoupled") is True,
            "inventory_sample": sample.get("inventory_sample") is True,
            "location": shown(sample.get("location") or container.get("name")),
            "last_modified": format_last_modified(sample.get("updated_at") or sample.get("modified_at")),
            "last_modified_at": sample.get("updated_at") or sample.get("modified_at")}

def apply_molecule_name_update(row, name, molecule_name_id):
    """Apply a successful molecule-name edit without replacing the inventory row."""
    row["molecule"] = shown(name)
    row["molecule_name_id"] = molecule_name_id

def refresh_inventory_row(client, row, require_amount=False):
    """Fetch and validate an edited sample before replacing its displayed row."""
    refreshed = client.inventory_entry(
        row["id"], row.get("collection_id"), row.get("synchronized", False)
    )
    if require_amount and refreshed.get("amount_value") is None:
        raise ConnectionError("Chemotion did not retain the amount update.")
    row.clear()
    row.update(refreshed)

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
    def inventory(self, collection_id, synchronized=False, progress=None, cancelled=None, rows_loaded=None, sequential=False):
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
        worker_state = threading.local()
        def retrieve_sample(sample):
            check_cancelled()
            client = getattr(worker_state, "client", None)
            if client is None:
                client = ChemotionClient(); client.login(); worker_state.client = client
            check_cancelled()
            return client.inventory_entry(sample["id"], collection_id, synchronized, sample)

        rows = [None] * len(samples)
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
                    rows[futures[future]] = future.result()
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

def system_dark_mode():
    try: result = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.TimeoutExpired): return False
    return result.returncode == 0 and result.stdout.strip().casefold() == "dark"

def system_accent_color():
    """Return the current macOS accent colour, falling back to system blue."""
    colors = {-1: "#8e8e93", 0: "#ff3b30", 1: "#ff9500", 2: "#ffcc00",
              3: "#34c759", 4: "#007aff", 5: "#af52de", 6: "#ff2d55"}
    try:
        result = subprocess.run(["defaults", "read", "-g", "AppleAccentColor"], capture_output=True, text=True, timeout=1)
        return colors.get(int(result.stdout.strip()), colors[4])
    except (OSError, ValueError, subprocess.TimeoutExpired): return colors[4]

def contrasting_text_color(color):
    """Choose readable text for a solid RGB selection colour."""
    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    luminance = (0.299 * red) + (0.587 * green) + (0.114 * blue)
    return "#000000" if luminance > 160 else "#ffffff"

def status_text_color(dark):
    """Keep status messages legible against the current window appearance."""
    return "#f2f2f7" if dark else "#000000"

class StatusLabel(tk.Label):
    """Keep the progress status message readable in either appearance."""
    def configure(self, cnf=None, **kwargs):
        window = self.winfo_toplevel()
        kwargs["fg"] = status_text_color(getattr(window, "dark_mode", False))
        return super().configure(cnf, **kwargs)


def column_drop_marker_x(tree, event):
    """Measure the heading's left edge even when the first row is offscreen."""
    column = tree.identify_column(event.x)
    if not column or column == "#0":
        return None
    for y in range(event.y, tree.winfo_height()):
        row = tree.identify_row(y)
        if row:
            bounds = tree.bbox(row, column)
            if bounds:
                return bounds[0]
            break
    # Empty tables still have laid-out column widths and a scroll position.
    displayed = tree.cget("displaycolumns")
    if isinstance(displayed, str):
        displayed = tree.tk.splitlist(displayed)
    if tuple(displayed) == ("#all",):
        displayed = tree.cget("columns")
        if isinstance(displayed, str):
            displayed = tree.tk.splitlist(displayed)
    widths = [tree.column(f"#{index + 1}", "width") for index in range(len(displayed))]
    inset = next((x for x in range(tree.winfo_width())
                  if tree.identify_region(x, event.y) in {"heading", "separator"}), 0)
    tree_column_width = tree.column("#0", "width") if "tree" in tree.cget("show") else 0
    return inset + tree_column_width + sum(widths[:int(column[1:]) - 1]) - round(tree.xview()[0] * sum(widths))


class ColumnDragFeedback:
    """Show the prospective column drop point."""
    def __init__(self, tree, color):
        self.tree = tree
        self.color = color
        self.marker = tk.Frame(tree.master, bg=color, width=3)

    def show(self, event):
        """Place a visible insertion line."""
        marker_x = column_drop_marker_x(self.tree, event)
        if marker_x is None or not 0 <= marker_x < self.tree.winfo_width():
            self.marker.place_forget()
            return
        self.marker.place(x=self.tree.winfo_x() + marker_x - 1,
                          y=self.tree.winfo_y(), height=26)
        self.marker.lift()

    def hide(self):
        self.marker.place_forget()


def rows_for_location_filter(rows, location, retained_row_ids=()):
    """Apply a location filter while retaining rows just moved by the user."""
    if location == "All locations":
        return rows
    retained_row_ids = {str(row_id) for row_id in retained_row_ids}
    return [row for row in rows if row["location"] == location or str(row["id"]) in retained_row_ids]


def location_choices(rows):
    """Return the locations offered by both location editors."""
    return tuple(sorted({row["location"] for row in rows}, key=str.casefold))


class Inventorizer(tk.Tk):
    headings = ("Short label", "Location", "Structure", "Molecule name", "Density", "Sample name", "Vendor", "Amount", "Last modified", "Decoupled", "Inventory")
    keys = ("label", "location", "structure", "molecule", "density", "sample", "vendor", "amount", "last_modified", "decoupled", "inventory_sample")
    column_widths = {"label":120,"structure":85,"molecule":250,"density":100,"sample":200,"vendor":135,"amount":100,"location":260,"last_modified":145,"decoupled":95,"inventory_sample":85}
    column_min_widths = {"label":85,"structure":70,"molecule":120,"density":70,"sample":110,"vendor":90,"amount":70,"location":120,"last_modified":105,"decoupled":70,"inventory_sample":70}
    palettes = {False: {"base":"#f4f7f6","panel":"#ffffff","text":"#17211f","muted":"#52616c","accent":"#08756a","error":"#b42318"}, True: {"base":"#1c1c1e","panel":"#2c2c2e","text":"#f2f2f7","muted":"#aeaeb2","accent":"#41c7b3","error":"#ff6961"}}
    def __init__(self):
        global CHEMOTION_USERNAME, CHEMOTION_PASSWORD, CHEMOTION_URL
        username, password = saved_credentials(); CHEMOTION_USERNAME, CHEMOTION_PASSWORD = username, password
        super().__init__(); self.title("Chemotion Inventorizer"); self.minsize(1050, 620); self.rows = []; self.selected_row_ids = set(); self.location_filter_retained_row_ids = set(); self.sort_key = "label"; self.sort_reverse = False; self.location_filter = tk.StringVar(value="All locations"); self.collection_value = tk.StringVar(value="Choose collection…"); self.collection_choices = {}; self.logged_in = False; self.profile_settings = saved_profile_settings(); saved_server = self.profile_settings.get("chemotion_server", DEFAULT_CHEMOTION_URL); self.server_value = tk.StringVar(value=saved_server if isinstance(saved_server, str) else DEFAULT_CHEMOTION_URL); self.username_value = tk.StringVar(value=username); self.password_value = tk.StringVar(value=password); self.remember_credentials = tk.BooleanVar(value=True); self.amount_editor = None; self.editing_cell = None; self.edit_after_save = None; self.editing_rows = []; self.inventory_cancel_event = None; self.inventory_loading = False
        try: CHEMOTION_URL = normalized_chemotion_url(self.server_value.get())
        except ValueError: self.server_value.set(DEFAULT_CHEMOTION_URL); CHEMOTION_URL = DEFAULT_CHEMOTION_URL
        self.column_order = column_order(self.profile_settings.get("column_order"), self.keys)
        selected_columns = visible_columns(self.profile_settings.get("visible_columns"), self.column_order)
        self.column_visibility = {key: tk.BooleanVar(value=key in selected_columns) for key in self.keys}
        self.server_value.trace_add("write", self.connection_details_changed); self.username_value.trace_add("write", self.connection_details_changed); self.password_value.trace_add("write", self.connection_details_changed)
        icon_path = os.environ.get("CHEMOTION_INVENTORIZER_ICON")
        if icon_path and Path(icon_path).is_file():
            self.app_icon = tk.PhotoImage(file=icon_path)
            self.iconphoto(True, self.app_icon)
        self.dark_mode = system_dark_mode(); self.style = ttk.Style(self); self.colors = self.appearance_colors(self.dark_mode)
        self.sample_list_font = tkfont.nametofont("TkDefaultFont").copy()
        self.sample_list_font.configure(size=18)
        self.column_header_font = tkfont.nametofont("TkDefaultFont").copy()
        self.column_header_font.configure(size=12)
        self.top = tk.Frame(self, padx=24, pady=18); self.top.pack(fill="x")
        self.controls = tk.Frame(self.top); self.controls.pack(anchor="w", pady=(8, 0))
        self.login_controls = tk.Frame(self.controls); self.login_controls.pack(anchor="w")
        self.location_controls = tk.Frame(self.controls); self.location_controls.pack(anchor="w", pady=(6, 0))

        ttk.Label(self.login_controls, text="Server:").pack(side="left", padx=(4,0)); self.server_entry = ttk.Entry(self.login_controls, textvariable=self.server_value, width=28); self.server_entry.pack(side="left", padx=4); ttk.Label(self.login_controls, text="Username:").pack(side="left", padx=(4,0)); self.username_entry = ttk.Entry(self.login_controls, textvariable=self.username_value, width=9); self.username_entry.pack(side="left", padx=4); ttk.Label(self.login_controls, text="Password:").pack(side="left"); self.password_entry = ttk.Entry(self.login_controls, textvariable=self.password_value, width=21, show="•"); self.password_entry.pack(side="left", padx=4); self.remember_checkbutton = ttk.Checkbutton(self.login_controls, text="Remember", variable=self.remember_credentials, command=self.remember_credentials_changed); self.remember_checkbutton.pack(side="left", padx=(0, 4)); self.login_button = ttk.Button(self.login_controls, text="Sign in", command=self.login); self.login_button.pack(side="left", padx=(0, 12)); self.server_entry.bind("<Return>", self.login_from_return); self.username_entry.bind("<Return>", self.login_from_return); self.password_entry.bind("<Return>", self.login_from_return); ttk.Label(self.login_controls, text="Collection:").pack(side="left", padx=(4,0)); self.collection_menu = ttk.Combobox(self.login_controls, textvariable=self.collection_value, values=(), state="disabled", width=20); self.collection_menu.pack(side="left", padx=4); self.collection_menu.bind("<<ComboboxSelected>>", self.load_selected_collection); disable_combobox_scroll_selection(self.collection_menu); self.cancel_load_button = ttk.Button(self.login_controls, text="Cancel", command=self.cancel_inventory_load, state="disabled"); self.cancel_load_button.pack(side="left", padx=(0, 4)); self.refresh_button = ttk.Button(self.login_controls, text="Refresh", command=self.refresh); self.refresh_button.pack(side="left", padx=4)

        self.columns_button = ttk.Menubutton(self.location_controls, text="Columns")
        self.columns_menu = tk.Menu(self.columns_button, tearoff=False)
        self.columns_button.configure(menu=self.columns_menu); self.columns_button.pack(side="left", padx=4)
        column_labels = dict(zip(self.keys, self.headings))
        selector_keys = ("structure",) + tuple(key for key in self.keys if key != "structure")
        for key in selector_keys: self.columns_menu.add_checkbutton(label=column_labels[key], variable=self.column_visibility[key], command=lambda key=key: self.toggle_column(key))
        ttk.Label(self.location_controls, text="Location:").pack(side="left", padx=(12,0)); self.location_filter_menu = ttk.Combobox(self.location_controls, textvariable=self.location_filter, values=("All locations",), state="readonly", width=18); self.location_filter_menu.pack(side="left", padx=4); self.location_filter_menu.bind("<<ComboboxSelected>>", self.filter_by_location); disable_combobox_scroll_selection(self.location_filter_menu)
        self.print_button = ttk.Button(self.location_controls, text="Print…", command=self.printable, state="disabled"); self.print_button.pack(side="left", padx=4)
        self.structure_images = {}; self.structure_image_pending = set(); self.structure_image_queue = deque(); self.structure_image_queue_lock = threading.Lock(); self.structure_image_worker_active = False; self.structure_image_waiters = {}; self.structure_rows_pending = set()
        frame = ttk.Frame(self, padding=(12, 12, 12, 0), style="Inventory.TFrame"); frame.pack(fill="both", expand=True, padx=24); self.tree = ttk.Treeview(frame, columns=self.keys, displaycolumns=self.displayed_data_column_keys(), show="tree headings" if self.column_visibility["structure"].get() else "headings", style="Inventory.Treeview", selectmode="extended")
        self.tree.bind("<<TreeviewSelect>>", self.remember_selection)
        self.tree.bind("<Command-a>", self.select_all_visible)
        self.tree.bind("<Delete>", self.delete_selected_row)
        self.tree.bind(f"<{multi_selection_modifier(self.tk.call('tk', 'windowingsystem'))}-Button-1>", self.extend_edit_selection)
        self.tree.bind("<Shift-Button-1>", self.extend_edit_selection)
        self.tree.bind("<ButtonPress-1>", self.begin_column_drag)
        self.tree.bind("<B1-Motion>", self.move_column_drag)
        self.tree.bind("<ButtonRelease-1>", self.finish_column_drag)
        self.tree.bind("<Button-1>", self.edit_amount, add="+")
        self.tree.bind("<Double-1>", self.open_sample)
        self.tree.bind("<Double-1>", self.auto_size_column, add="+")
        # Header clicks are handled by finish_column_drag so the same gesture
        # can distinguish sorting from column reordering. Registering a
        # Treeview heading command as well would toggle the sort direction a
        # second time on a normal click.
        for heading, key in zip(self.headings, self.keys): self.tree.heading(key, text=heading)
        self.tree.heading("#0", text="Structure" if self.column_visibility["structure"].get() else "")
        self.tree.column("#0", width=85 if self.column_visibility["structure"].get() else 0,
                         minwidth=70 if self.column_visibility["structure"].get() else 0,
                         stretch=False, anchor="center")
        self.style.configure("Inventory.Treeview", font=self.sample_list_font, rowheight=56 if self.column_visibility["structure"].get() else 34, indent=0, imageanchor="center")
        # The native tree layout reserves space for an expander and left-aligns
        # item images, independently of the column's text anchor. Tk 9 also
        # needs imageanchor above because it overrides the layout's sticky.
        self.style.layout("Inventory.Treeview.Item", [
            ("Treeitem.padding", {"sticky": "nswe", "children": [
                ("Treeitem.image", {"sticky": ""}),
            ]}),
        ])
        self.update_sort_headings()
        for key, width in self.column_widths.items(): self.tree.column(key, width=width, minwidth=self.column_min_widths[key], anchor="center" if key in {"decoupled", "inventory_sample"} else "w")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview); horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview); self.vertical_scrollbar = vertical; self.horizontal_scrollbar = horizontal; self.tree.configure(yscrollcommand=self.tree_yview_changed, xscrollcommand=self.tree_xview_changed)
        self.tree.grid(row=0,column=0,sticky="nsew"); vertical.grid(row=0,column=1,sticky="ns"); horizontal.grid(row=1,column=0,sticky="ew"); frame.columnconfigure(0,weight=1); frame.rowconfigure(0,weight=1)
        self.progress_area = tk.Frame(self, padx=24); self.progress_area.pack(fill="x", pady=(0, 18))
        self.status_area = tk.Frame(self.progress_area); self.status_area.pack(side="left", padx=(0, 12))
        self.status = StatusLabel(self.status_area, text="0 samples"); self.status.pack(anchor="w")
        self.progress = ttk.Progressbar(self.progress_area, mode="determinate", maximum=100, style="Inventory.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True)
        self.cancel_load_button.destroy()
        self.cancel_load_button = ttk.Button(self.progress_area, text="Stop", command=self.cancel_inventory_load, state="disabled")
        self.cancel_load_button.pack(side="right", padx=(8, 0))
        self.bind("<Escape>", lambda _event: self.cancel_load_button.invoke(), add="+")
        self.apply_appearance(True); self.watch_system_appearance()
        self.after_idle(lambda: self.state("zoomed"))
        if username and password and CHEMOTION_URL != DEFAULT_CHEMOTION_URL:
            # Defer until the window has been built so the app can paint before
            # the network request begins.
            self.status.configure(text="Signing in to Chemotion…", fg=self.colors["accent"])
            self.after_idle(self.login)
        else:
            self.status.configure(text="Enter your Chemotion credentials and select Sign in.", fg=self.colors["muted"])
    def apply_appearance(self, force=False):
        dark = system_dark_mode()
        accent = system_accent_color()
        if not force and dark == self.dark_mode and accent == self.colors.get("accent"): return
        self.dark_mode = dark; self.colors = self.appearance_colors(dark); self.colors["accent"] = accent; c = self.colors; self.configure(bg=c["base"])
        for item in (self.top, self.status_area, self.controls, self.login_controls, self.location_controls, self.progress_area): item.configure(bg=c["base"])
        self.status.configure(bg=c["base"])
        self.style.configure("Inventory.Horizontal.TProgressbar", background=accent, troughcolor=c["panel"])
        row_height = 56 if self.column_visibility["structure"].get() else 34
        self.style.configure("Inventory.TFrame",background=c["base"]); self.style.configure("Inventory.Treeview",background=c["panel"],fieldbackground=c["panel"],foreground=c["text"],rowheight=row_height); self.style.map("Inventory.Treeview",background=[("selected",accent)],foreground=[("selected",contrasting_text_color(accent))]); self.style.configure("Inventory.Treeview.Heading",background=c["base"],foreground=c["text"],font=self.column_header_font)
        self.tree.tag_configure("alternate", background="#38383a" if dark else "#eeeeee")
    def appearance_colors(self, dark):
        """Use the native label surface for the surrounding window as well."""
        colors = self.palettes[dark].copy()
        colors["base"] = self.style.lookup("TLabel", "background") or colors["base"]
        return colors
    def watch_system_appearance(self): self.apply_appearance(); self.after(1500,self.watch_system_appearance)
    def visible_column_keys(self):
        return tuple(key for key in self.column_order if self.column_visibility[key].get())
    def displayed_data_column_keys(self):
        return tuple(key for key in self.visible_column_keys() if key != "structure")
    def configure_visible_columns(self):
        structure_visible = self.column_visibility["structure"].get()
        self.tree.configure(displaycolumns=self.displayed_data_column_keys(),
                            show="tree headings" if structure_visible else "headings")
        self.tree.heading("#0", text="Structure" if structure_visible else "")
        self.tree.column("#0", width=85 if structure_visible else 0,
                         minwidth=70 if structure_visible else 0,
                         stretch=False, anchor="center")
        self.style.configure("Inventory.Treeview", rowheight=56 if structure_visible else 34)
    def toggle_column(self, key):
        if not self.visible_column_keys():
            self.column_visibility[key].set(True)
            self.status.configure(text="Keep at least one column visible.", fg=self.colors["error"])
            return
        self.configure_visible_columns()
        if key == "structure" and self.column_visibility[key].get() and not self.inventory_loading:
            self.load_structure_images(self.rows)
        # Tk needs one idle cycle to lay out the newly displayed columns.
        self.after_idle(self.adjust_displayed_column_widths)
        self.profile_settings["visible_columns"] = list(self.visible_column_keys())
        save_profile_settings(self.profile_settings)
    def header_column_key(self, event):
        """Return the displayed column below a pointer in the heading row."""
        if self.tree.identify_region(event.x, event.y) != "heading":
            return None
        return self.displayed_column_key(event)
    def begin_column_drag(self, event):
        self.dragged_column = self.header_column_key(event)
        self.drag_target_column = self.dragged_column
        self.column_dragging = False
        self.column_drag_start = (event.x, event.y)
        return "break" if self.dragged_column else None
    def move_column_drag(self, event):
        if not getattr(self, "dragged_column", None):
            return None
        start_x, start_y = self.column_drag_start
        if max(abs(event.x - start_x), abs(event.y - start_y)) >= 5:
            self.column_dragging = True
        target = self.header_column_key(event)
        if target:
            self.drag_target_column = target
            self.column_dragging = self.column_dragging or target != self.dragged_column
            if not getattr(self, "column_drag_feedback", None):
                self.column_drag_feedback = ColumnDragFeedback(self.tree, self.colors["accent"])
            self.column_drag_feedback.show(event)
        return "break"
    def finish_column_drag(self, event):
        source = getattr(self, "dragged_column", None)
        target = self.header_column_key(event) or getattr(self, "drag_target_column", None)
        dragged = getattr(self, "column_dragging", False)
        start = getattr(self, "column_drag_start", None)
        if start and max(abs(event.x - start[0]), abs(event.y - start[1])) >= 5:
            dragged = True
        self.dragged_column = None
        self.drag_target_column = None
        self.column_dragging = False
        self.column_drag_start = None
        if getattr(self, "column_drag_feedback", None):
            self.column_drag_feedback.hide()
        if not source:
            return None
        if not dragged:
            if self.header_column_key(event) == source:
                self.sort_by(source)
            return "break"
        if not target or target == source:
            return "break"
        order = list(self.column_order)
        order.remove(source)
        order.insert(order.index(target), source)
        self.column_order = tuple(order)
        self.configure_visible_columns()
        self.profile_settings["column_order"] = list(self.column_order)
        self.profile_settings["visible_columns"] = list(self.visible_column_keys())
        save_profile_settings(self.profile_settings)
        self.after_idle(self.adjust_displayed_column_widths)
        return "break"
    def adjust_displayed_column_widths(self):
        """Fit the currently visible columns to the table, preserving their proportions."""
        visible = self.displayed_data_column_keys()
        available_width = self.tree.winfo_width() - 2
        if self.column_visibility["structure"].get():
            available_width -= self.column_widths["structure"]
        if not visible or available_width <= 1:
            return
        preferred_width = sum(self.column_widths[key] for key in visible)
        target_width = max(available_width, sum(self.column_min_widths[key] for key in visible))
        remaining_width = target_width
        for index, key in enumerate(visible[:-1]):
            remaining_minimum = sum(self.column_min_widths[next_key] for next_key in visible[index + 1:])
            width = max(self.column_min_widths[key], round(target_width * self.column_widths[key] / preferred_width))
            width = min(width, remaining_width - remaining_minimum)
            self.tree.column(key, width=width)
            remaining_width -= width
        self.tree.column(visible[-1], width=remaining_width)
    def displayed_column_key(self, event):
        """Resolve a click to a column key even when columns are hidden."""
        column = self.tree.identify_column(event.x)
        if column == "#0":
            return None
        try: return self.displayed_data_column_keys()[int(column[1:]) - 1]
        except (IndexError, ValueError): return None
    def auto_size_column(self, event):
        """Fit a column to its heading and currently displayed values."""
        if self.tree.identify_region(event.x, event.y) != "separator":
            return None
        column = self.tree.identify_column(event.x)
        key = "structure" if column == "#0" else self.displayed_column_key(event)
        if key is None:
            return None
        font_name = self.style.lookup("Inventory.Treeview", "font") or "TkDefaultFont"
        heading_font_name = self.style.lookup("Inventory.Treeview.Heading", "font") or font_name
        font = tkfont.Font(font=font_name)
        heading_font = tkfont.Font(font=heading_font_name)
        heading = self.tree.heading(column, "text")
        content_width = max([heading_font.measure(heading), *(
            font.measure(self.tree.set(item_id, key))
            for item_id in self.tree.get_children("")
        )])
        width = max(self.column_min_widths[key], content_width + 24)
        self.tree.column(key, width=width)
        return "break"
    def remember_credentials_changed(self):
        """Immediately remove saved credentials when Remember is turned off."""
        if self.remember_credentials.get():
            return
        try:
            clear_credentials()
        except Exception as error:
            # Do not leave the control unchecked when secure storage could not be cleared.
            self.remember_credentials.set(True)
            messagebox.showerror("Chemotion Inventorizer", str(error))

    def login(self):
        global CHEMOTION_USERNAME, CHEMOTION_PASSWORD, CHEMOTION_URL
        username, password = self.username_value.get().strip(), self.password_value.get()
        try: server = normalized_chemotion_url(self.server_value.get())
        except ValueError as error:
            self.status.configure(text=str(error), fg=self.colors["error"])
            return
        if not username or not password:
            self.status.configure(text="Enter both a Chemotion username and password.", fg=self.colors["error"])
            return
        try:
            if self.remember_credentials.get():
                save_credentials(username, password)
            else:
                # Also clear here in case the checkbox was changed programmatically.
                clear_credentials()
        except Exception as error:
            messagebox.showerror("Chemotion Inventorizer", str(error)); return
        CHEMOTION_URL, CHEMOTION_USERNAME, CHEMOTION_PASSWORD = server, username, password
        self.server_value.set(server)
        self.profile_settings["chemotion_server"] = server
        save_profile_settings(self.profile_settings)
        self.logged_in = False; self.login_button.configure(text="Sign in")
        self.collection_value.set("Choose collection…"); self.collection_choices = {}; self.collection_menu.configure(values=())
        self.load_collections()
    def login_from_return(self, _event=None):
        if not self.logged_in: self.login()
        return "break"
    def connection_details_changed(self, *_args):
        if self.logged_in:
            self.logged_in = False
            self.login_button.configure(text="Sign in", state="normal")
    def load_collections(self):
        self.set_controls_busy(True); self.status.configure(text="Retrieving editable Chemotion collections…",fg=self.colors["accent"]); threading.Thread(target=self._retrieve_collections, daemon=True).start()
    def _retrieve_collections(self):
        try:
            client = ChemotionClient(); client.login(); self.after(0, self._collections_loaded, client.collections())
        except Exception as error: self.after(0, self._collections_failed, str(error))
    def _collections_loaded(self, collections):
        collections = [item for item in collections if str(item.get("label") or "").casefold() != "chemotion-repository.net"]
        # Keep synchronized collections in their own group.  Within that
        # group, organize collections by the displayed source/user code first
        # so collections shared by the same account stay together.
        collections.sort(key=lambda item: (
            bool(item.get("_synchronized")),
            synchronized_collection_source(item).casefold() if item.get("_synchronized") and synchronized_collection_source(item) else "",
            shown(item.get("label")).casefold(),
        ))
        self.collection_choices = {collection_picker_label(item): item for item in collections}
        self.collection_menu.configure(values=tuple(self.collection_choices))
        self.logged_in = True; self.login_button.configure(text="Signed in")
        self.set_controls_busy(False)
        saved = self.profile_settings.get("last_collection")
        selected_label = next((label for label, item in self.collection_choices.items()
                               if isinstance(saved, dict) and item.get("id") == saved.get("id")
                               and bool(item.get("_synchronized")) == bool(saved.get("synchronized"))), None)
        if selected_label:
            self.collection_value.set(selected_label)
            self.status.configure(text="Opening the last used collection…", fg=self.colors["accent"])
            self.after_idle(self.refresh)
        else:
            self.status.configure(text="Choose a collection to retrieve its inventory.", fg=self.colors["muted"])
    def _collections_failed(self, error):
        self.set_controls_busy(False); self.status.configure(text="Chemotion collections could not be retrieved.",fg=self.colors["error"]); messagebox.showerror("Chemotion Inventorizer", error)
    def load_selected_collection(self, _event=None): self.refresh()
    def refresh(self):
        if self.collection_value.get() not in self.collection_choices:
            self.status.configure(text="Choose a collection before retrieving inventory.", fg=self.colors["error"])
            return
        collection = self.collection_choices[self.collection_value.get()]
        self.profile_settings["last_collection"] = {"id": collection["id"], "synchronized": bool(collection.get("_synchronized"))}
        save_profile_settings(self.profile_settings)
        self.rows = []; self.structure_rows_pending.clear(); self.selected_row_ids.clear(); self.location_filter_retained_row_ids.clear(); self.render_rows()
        self.sort_key = "label"; self.sort_reverse = True; self.update_sort_headings()
        cancelled = threading.Event(); self.inventory_cancel_event = cancelled; self.inventory_loading = True
        self.set_controls_busy(True); self.show_progress(); self.status.configure(text="Retrieving the current inventory from Chemotion… (Esc to stop)",fg=self.colors["accent"]); threading.Thread(target=self._retrieve, args=(collection, cancelled, self.column_visibility["structure"].get()), daemon=True).start()
    def cancel_inventory_load(self, _event=None):
        if not self.inventory_loading or not self.inventory_cancel_event or self.inventory_cancel_event.is_set():
            return
        self.inventory_cancel_event.set()
        self.cancel_load_button.configure(state="disabled")
        self.status.configure(text="Stopping inventory loading…", fg=self.colors["muted"])
        return "break"
    def _retrieve(self, collection, cancelled, with_structures=False):
        try:
            def deliver_rows(rows):
                if not with_structures:
                    self.after(0, self._rows_loaded, rows, cancelled)
                    return
                displayed = threading.Event()
                self.after(0, self._rows_loaded, rows, cancelled, displayed)
                # Only the GUI thread can confirm that the image has reached
                # the table. Stop must also release this wait immediately.
                while not displayed.wait(0.1):
                    if cancelled.is_set():
                        return
            rows = ChemotionClient().inventory(collection["id"], collection.get("_synchronized", False),
                                                lambda message: self.after(0, self._progress, message, cancelled), cancelled,
                                                deliver_rows, sequential=with_structures)
            self.after(0, self._loaded, rows, cancelled)
        except InventoryLoadCancelled as stopped:
            self.after(0, self._cancelled, cancelled, stopped.rows)
        except Exception as error:
            if not cancelled.is_set(): self.after(0, self._failed, str(error), cancelled)
    def show_progress(self):
        self.progress.configure(mode="determinate", maximum=100, value=0)
    def _rows_loaded(self, rows, cancelled, displayed=None):
        if cancelled is not self.inventory_cancel_event or not self.inventory_loading:
            if displayed is not None:
                displayed.set()
            return
        self.rows.extend(rows)
        if self.column_visibility["structure"].get():
            self.structure_rows_pending.update(
                row["id"] for row in rows
                if row.get("structure") and row["structure"] not in self.structure_images
            )
        self.update_location_filter()
        self.render_rows()
        if displayed is not None:
            source = rows[0].get("structure") if rows else None
            if (self.column_visibility["structure"].get() and source
                    and source not in self.structure_images):
                self.structure_image_waiters.setdefault(source, []).append(displayed)
            else:
                self.after_idle(displayed.set)
        if self.column_visibility["structure"].get():
            self.load_structure_images(rows)
    def hide_progress(self):
        self.progress.configure(maximum=100, value=0)
    def _progress(self, message, cancelled=None):
        if cancelled is not None and (cancelled.is_set() or cancelled is not self.inventory_cancel_event): return
        self.status.configure(text=f"{message} (Esc to stop)", fg=self.colors["accent"])
        match = re.search(r": (\d+) of (\d+)", message)
        if match and not message.startswith("Retrieving sample list:"):
            current, total = map(int, match.groups())
            self.progress.configure(maximum=max(total, 1), value=current)
    def _loaded(self, rows, cancelled=None):
        if cancelled is not None and cancelled is not self.inventory_cancel_event: return
        self.inventory_loading = False
        self.rows=rows; self.location_filter_retained_row_ids.clear(); self.update_location_filter(); self.render_rows(); self.hide_progress(); self.status.configure(text=f"{len(self.filtered_rows())} samples",fg=self.colors["muted"]); self.set_controls_busy(False); self.print_button.configure(state="normal" if rows else "disabled")
        if self.column_visibility["structure"].get():
            self.load_structure_images(self.filtered_rows())
    def _cancelled(self, cancelled, rows=None):
        if cancelled is self.inventory_cancel_event:
            # Include completed batches already rendered in the table. A Tk
            # callback for the latest batch may still be queued when Stop is
            # handled, even though the worker has already raised cancellation.
            retained_rows = list(rows or [])
            retained_ids = {row.get("id") for row in retained_rows}
            retained_rows.extend(row for row in self.rows if row.get("id") not in retained_ids)
            self._loaded(retained_rows, cancelled)
    def _failed(self,error, cancelled=None):
        if cancelled is not None and (cancelled.is_set() or cancelled is not self.inventory_cancel_event): return
        self.inventory_loading = False
        self.hide_progress(); self.status.configure(text="The Chemotion inventory could not be retrieved.",fg=self.colors["error"]); self.set_controls_busy(False); messagebox.showerror("Chemotion Inventorizer",error)
    def set_controls_busy(self, busy):
        self.refresh_button.configure(state="disabled" if busy else "normal")
        self.cancel_load_button.configure(state="normal" if busy and self.inventory_loading else "disabled")
        self.login_button.configure(state="disabled" if busy or self.logged_in else "normal")
        self.server_entry.configure(state="disabled" if busy else "normal")
        self.username_entry.configure(state="disabled" if busy else "normal")
        self.password_entry.configure(state="disabled" if busy else "normal")
        self.remember_checkbutton.configure(state="disabled" if busy else "normal")
        self.collection_menu.configure(state="disabled" if busy else ("readonly" if self.collection_choices else "disabled"))
        self.location_filter_menu.configure(state="disabled" if busy else "readonly")
    def update_sort_headings(self):
        for heading, key in zip(self.headings, self.keys):
            arrow = " ▼" if self.sort_reverse else " ▲"
            self.tree.heading(key, text=f"{heading}{arrow}" if key == self.sort_key else heading)
    def sort_by(self,key): self.sort_reverse = not self.sort_reverse if self.sort_key == key else False; self.sort_key = key; self.update_sort_headings(); self.render_rows()
    def update_location_filter(self):
        locations = location_choices(self.rows)
        choices = ("All locations", *locations); self.location_filter_menu.configure(values=choices)
        if self.location_filter.get() not in choices: self.location_filter.set("All locations")
    def filter_by_location(self, _event=None):
        self.location_filter_retained_row_ids.clear()
        self.selected_row_ids.clear()
        self.render_rows(); count = len(self.filtered_rows())
        if self.column_visibility["structure"].get() and not self.inventory_loading:
            self.load_structure_images(self.filtered_rows())
        self.status.configure(text=f"{count} samples", fg=self.colors["muted"])
    def filtered_rows(self):
        return rows_for_location_filter(self.rows, self.location_filter.get(), self.location_filter_retained_row_ids)
    def render_rows(self, preserve_display_order=False):
        existing_ids = self.tree.get_children()
        displayed_ids = existing_ids if preserve_display_order else ()
        key = self.sort_key
        def sort_value(item):
            if key == "label":
                return natural_sort_value(item[key])
            if key == "last_modified":
                return last_modified_sort_value(item.get("last_modified_at"))
            return str(item[key]).casefold()
        rows = self.filtered_rows()
        if self.column_visibility["structure"].get():
            rows = [row for row in rows if row["id"] not in self.structure_rows_pending]
        if preserve_display_order:
            # An edit can change the active sort field (or Chemotion's last
            # modified timestamp).  Keep the user's current visual order
            # until they deliberately choose a column heading again.
            rows_by_id = {str(row["id"]): row for row in rows}
            ordered_rows = [rows_by_id.pop(item_id) for item_id in displayed_ids if item_id in rows_by_id]
            ordered_rows.extend(rows_by_id.values())
        else:
            ordered_rows = sorted(rows, key=sort_value, reverse=self.sort_reverse)
        visible_ids = {str(row["id"]) for row in ordered_rows}
        editing_cell = getattr(self, "editing_cell", None)
        if editing_cell and editing_cell[0] not in visible_ids:
            self.cancel_amount_edit()
        for item_id in existing_ids:
            if item_id not in visible_ids:
                self.tree.delete(item_id)
        existing_ids = set(existing_ids)
        for index, row in enumerate(ordered_rows):
            options = {"values": self.row_values(row), "tags": ("alternate",) if index % 2 else ()}
            image = self.structure_images.get(row.get("structure"))
            if image is not None:
                options["image"] = image
            item_id = str(row["id"])
            if item_id in existing_ids:
                options.setdefault("image", "")
                self.tree.item(item_id, **options)
                self.tree.move(item_id, "", index)
            else:
                self.tree.insert("", index, iid=item_id, **options)
        if editing_cell and editing_cell[0] in visible_ids:
            self.reposition_active_editor()
    def row_values(self, row):
        checkbox = lambda value: "☑" if value else "☐"
        # Treeview fonts apply to an entire row, not an individual column.  Keep
        # inventory numbers in the native table font instead of substituting
        # Unicode mathematical "bold" digits, which macOS renders in another face.
        return (str(row["label"]), row["location"], displayed_row_value(row, "structure"), row["molecule"], displayed_row_value(row, "density"), row["sample"], row["vendor"], row["amount"], displayed_row_value(row, "last_modified"), checkbox(row["decoupled"]), checkbox(row["inventory_sample"]))
    def load_structure_images(self, rows):
        """Queue missing structures and fetch them serially as rows appear."""
        with self.structure_image_queue_lock:
            for row in rows:
                source = row.get("structure")
                if (source and source not in self.structure_images
                        and source not in self.structure_image_pending):
                    self.structure_image_pending.add(source)
                    self.structure_image_queue.append(source)
            if not self.structure_image_queue or self.structure_image_worker_active:
                return
            self.structure_image_worker_active = True
        threading.Thread(target=self._load_structure_images, daemon=True).start()
    def _load_structure_images(self):
        client = None
        try:
            import resvg_py
        except Exception as error:
            with self.structure_image_queue_lock:
                failed_sources = tuple(self.structure_image_queue)
                self.structure_image_queue.clear()
                self.structure_image_worker_active = False
            message = f"SVG renderer could not load ({error}). Restart using the application launcher to set up dependencies."
            self.after(0, self._structure_image_dependency_missing, failed_sources, message)
            return
        while True:
            with self.structure_image_queue_lock:
                if not self.structure_image_queue:
                    self.structure_image_worker_active = False
                    return
                source = self.structure_image_queue.popleft()
            try:
                if isinstance(source, str) and "<svg" in source[:4096].lower():
                    svg_data = source.encode("utf-8")
                else:
                    if client is None:
                        client = ChemotionClient()
                        client.login()
                    path = structure_image_request_path(source)
                    svg_data = client.request(path)
                if b"<svg" not in svg_data[:4096].lower():
                    raise ValueError("Chemotion did not return an SVG structure image.")
                dimensions = re.search(rb'\bviewBox=["\']\s*([\d.+-]+)[ ,]+([\d.+-]+)[ ,]+([\d.+-]+)[ ,]+([\d.+-]+)', svg_data[:4096], re.IGNORECASE)
                if dimensions:
                    _x, _y, width, height = (float(value) for value in dimensions.groups())
                    aspect_ratio = width / height if width > 0 and height > 0 else 1.0
                else:
                    width = re.search(rb'\bwidth=["\']\s*([\d.]+)', svg_data[:4096], re.IGNORECASE)
                    height = re.search(rb'\bheight=["\']\s*([\d.]+)', svg_data[:4096], re.IGNORECASE)
                    width_value = float(width.group(1)) if width else 1.0
                    height_value = float(height.group(1)) if height else 1.0
                    aspect_ratio = width_value / height_value if height_value > 0 else 1.0
                if aspect_ratio >= 75 / 50:
                    output_width, output_height = 75, max(1, round(75 / aspect_ratio))
                else:
                    output_width, output_height = max(1, round(50 * aspect_ratio)), 50
                image_data = resvg_py.svg_to_bytes(svg_string=svg_data.decode("utf-8-sig"),
                                                  width=output_width, height=output_height)
            except Exception as error:
                self.after(0, self._structure_image_failed, source, str(error))
            else:
                self.after(0, self._structure_image_loaded, source, image_data)
    def _structure_image_dependency_missing(self, sources, error):
        self.structure_image_pending.difference_update(sources)
        self.status.configure(
            text=f"Structure thumbnail rendering unavailable: {error}",
            fg=self.colors["error"],
        )
        for source in sources:
            self._structure_image_finished(source)
    def _structure_image_loaded(self, source, image_data):
        self.structure_image_pending.discard(source)
        try:
            image = tk.PhotoImage(data=base64.b64encode(image_data).decode("ascii"), format="png")
            self.structure_images[source] = image
            for row in self.rows:
                if row.get("structure") == source and self.tree.exists(str(row["id"])):
                    self.tree.item(str(row["id"]), image=image)
        except Exception as error:
            self._structure_image_failed(source, str(error))
        else:
            self._structure_image_finished(source)
    def _structure_image_failed(self, source, error):
        self.structure_image_pending.discard(source)
        message = " ".join(str(error).split())
        if len(message) > 120:
            message = message[:117] + "…"
        self.status.configure(text=f"Structure thumbnail failed: {message}", fg=self.colors["muted"])
        self._structure_image_finished(source)
    def _structure_image_finished(self, source):
        ready_ids = {row["id"] for row in self.rows if row.get("structure") == source}
        if self.structure_rows_pending.intersection(ready_ids):
            self.structure_rows_pending.difference_update(ready_ids)
            self.render_rows()
        for displayed in self.structure_image_waiters.pop(source, []):
            self.after_idle(displayed.set)
    def remember_selection(self, _event=None):
        self.selected_row_ids = set(self.tree.selection())
    def select_all_visible(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        self.selected_row_ids = set(self.tree.selection())
        return "break"
    def delete_selected_row(self, _event=None):
        """Confirm removal of the focused selected row from this collection."""
        selected_ids = set(self.tree.selection())
        item_id = self.tree.focus()
        if item_id not in selected_ids:
            item_id = next(iter(selected_ids), None)
        row = next((item for item in self.rows if str(item["id"]) == item_id), None)
        if not row:
            return "break"
        collection = self.collection_choices.get(self.collection_value.get())
        if not collection:
            self.status.configure(text="Choose a collection before removing a sample.", fg=self.colors["error"])
            return "break"
        sample = row["sample"] or f"sample {row['label']}"
        short_label = row["label"] or sample
        if not messagebox.askyesno(
                "Remove from collection?",
                f"Remove {sample} from the selected collection?\n\nIt will remain in Chemotion and any other collections.",
                parent=self):
            return "break"
        self.cancel_amount_edit()
        self.set_controls_busy(True); self.print_button.configure(state="disabled"); self.show_progress()
        self.status.configure(text=f"Removing {short_label} from the collection…", fg=self.colors["accent"])
        threading.Thread(target=self._remove_sample_from_collection,
                         args=(row, collection["id"], bool(collection.get("_synchronized")),
                               collection.get("_source_collection_id")), daemon=True).start()
        return "break"
    def _remove_sample_from_collection(self, row, collection_id, synchronized=False, source_collection_id=None):
        try:
            client = ChemotionClient(); client.login()
            result = client.remove_sample_from_collection(collection_id, row["id"], synchronized, source_collection_id)
            self.after(0, self._sample_removed_from_collection, row, result.get("locked_sample_ids") or [])
        except Exception as error:
            self.after(0, self._sample_removal_failed, str(error))
    def _sample_removed_from_collection(self, row, locked_sample_ids):
        if row["id"] in locked_sample_ids or str(row["id"]) in {str(item) for item in locked_sample_ids}:
            self.hide_progress(); self.set_controls_busy(False)
            self.print_button.configure(state="normal" if self.rows else "disabled")
            self.status.configure(text=f"{row['label']} remains in this collection because it is linked to another item.", fg=self.colors["error"])
            return
        self.rows = [item for item in self.rows if item is not row]
        self.selected_row_ids.clear(); self.update_location_filter(); self.render_rows(preserve_display_order=True)
        self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"Removed {row['label']} from the selected collection.", fg=self.colors["muted"])
    def _sample_removal_failed(self, error):
        self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text="The sample could not be removed from the collection.", fg=self.colors["error"])
        messagebox.showerror("Chemotion Inventorizer", error)
    def extend_edit_selection(self, event):
        """Extend row selection without opening an editor on a modifier-click."""
        item_id = self.tree.identify_row(event.y)
        if not item_id: return "break"
        selected_ids = set(self.tree.selection())
        if event.state & 0x0001:  # Shift: add the visible range from the focus row.
            items = self.tree.get_children()
            anchor = self.tree.focus()
            if anchor in items:
                start, end = sorted((items.index(anchor), items.index(item_id)))
                selected_ids.update(items[start:end + 1])
            else:
                selected_ids.add(item_id)
        else:  # Command: add this row while preserving the existing selection.
            selected_ids.add(item_id)
        self.tree.selection_set(tuple(selected_ids)); self.tree.focus(item_id)
        self.selected_row_ids = selected_ids
        return "break"
    def edit_amount(self, event):
        """Edit the supported inventory fields directly in the table."""
        item_id = self.tree.identify_row(event.y)
        if not item_id: return
        row = next((item for item in self.rows if str(item["id"]) == item_id), None)
        if not row: return
        column = self.displayed_column_key(event)
        if column not in {"location", "molecule", "density", "sample", "vendor", "amount", "decoupled", "inventory_sample"}: return
        # Starting an edit on an already selected row must not collapse a
        # multi-selection: the entered value is applied to that selection.
        selected_ids = self.selected_row_ids or set(self.tree.selection())
        if item_id not in selected_ids:
            selected_ids = {item_id}
            self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.selected_row_ids = selected_ids
        self.editing_rows = [candidate for candidate in self.filtered_rows() if str(candidate["id"]) in selected_ids]
        if column == "location": return self.edit_location(row, item_id)
        if column == "molecule": return self.edit_name(row, item_id, "molecule")
        if column == "density": return self.edit_name(row, item_id, "density")
        if column == "sample": return self.edit_name(row, item_id, "sample")
        if column == "vendor": return self.edit_vendor(row, item_id)
        if column == "decoupled": return self.change_selected_flags(item_id, "decoupled")
        if column == "inventory_sample": return self.change_selected_flags(item_id, "inventory_sample")
        if column != "amount": return
        self.cancel_amount_edit()
        self.editing_rows = self.selected_edit_rows(row)
        box = self.tree.bbox(item_id, "amount")
        if not box: return "break"
        x, y, width, height = box
        editor = ttk.Entry(self.tree)
        editor.insert(0, row["amount"] if row["amount"] not in {"", "—"} else "")
        self.place_editor(editor, item_id, "amount")
        self.amount_editor = editor
        editor.focus_set(); editor.selection_range(0, "end")
        editor.bind("<Return>", lambda _event: self.finish_amount_edit(row, advance=True))
        editor.bind("<Escape>", lambda _event: self.cancel_amount_edit())
        self.bind_editor_navigation(editor, item_id)
        editor.bind("<FocusOut>", lambda _event: self.finish_amount_edit(row))
        return "break"
    def edit_vendor(self, row, item_id):
        self.cancel_amount_edit()
        self.editing_rows = self.selected_edit_rows(row)
        box = self.tree.bbox(item_id, "vendor")
        if not box: return "break"
        x, y, width, height = box
        editor = ttk.Entry(self.tree); editor.insert(0, "" if row["vendor"] == "—" else row["vendor"])
        self.place_editor(editor, item_id, "vendor"); self.amount_editor = editor
        editor.focus_set(); editor.selection_range(0, "end")
        editor.bind("<Return>", lambda _event: self.finish_vendor_edit(row, advance=True))
        editor.bind("<Escape>", lambda _event: self.cancel_amount_edit())
        self.bind_editor_navigation(editor, item_id)
        editor.bind("<FocusOut>", lambda _event: self.finish_vendor_edit(row))
        return "break"
    def edit_location(self, row, item_id):
        self.cancel_amount_edit()
        self.editing_rows = self.selected_edit_rows(row)
        box = self.tree.bbox(item_id, "location")
        if not box: return "break"
        # Select a known location or type a new one directly in the cell.
        editor = ttk.Combobox(self.tree, values=location_choices(self.rows), state="normal")
        editor.set(row["location"])
        self.place_editor(editor, item_id, "location"); self.amount_editor = editor
        editor.focus_set(); editor.selection_range(0, "end")
        editor.bind("<Return>", lambda _event: self.finish_location_edit(row, advance=True))
        editor.bind("<<ComboboxSelected>>", lambda _event: self.finish_location_edit(row))
        editor.bind("<Escape>", lambda _event: self.cancel_amount_edit())
        self.bind_editor_navigation(editor, item_id)
        editor.bind("<FocusOut>", lambda _event: self.finish_location_edit(row))
        return "break"
    def edit_amount_cell(self, row, item_id):
        self.cancel_amount_edit()
        self.editing_rows = self.selected_edit_rows(row)
        box = self.tree.bbox(item_id, "amount")
        if not box: return "break"
        x, y, width, height = box
        editor = ttk.Entry(self.tree)
        editor.insert(0, row["amount"] if row["amount"] not in {"", "—"} else "")
        self.place_editor(editor, item_id, "amount")
        self.amount_editor = editor
        editor.focus_set(); editor.selection_range(0, "end")
        editor.bind("<Return>", lambda _event: self.finish_amount_edit(row, advance=True))
        editor.bind("<Escape>", lambda _event: self.cancel_amount_edit())
        self.bind_editor_navigation(editor, item_id)
        editor.bind("<FocusOut>", lambda _event: self.finish_amount_edit(row))
        return "break"
    def edit_name(self, row, item_id, field):
        self.cancel_amount_edit()
        self.editing_rows = self.selected_edit_rows(row)
        box = self.tree.bbox(item_id, field)
        if not box: return "break"
        x, y, width, height = box
        editor = ttk.Entry(self.tree); editor.insert(0, "" if row[field] == "—" else row[field])
        self.place_editor(editor, item_id, field); self.amount_editor = editor
        editor.focus_set(); editor.selection_range(0, "end")
        editor.bind("<Return>", lambda _event: self.finish_name_edit(row, field, advance=True))
        editor.bind("<Escape>", lambda _event: self.cancel_amount_edit())
        self.bind_editor_navigation(editor, item_id)
        editor.bind("<FocusOut>", lambda _event: self.finish_name_edit(row, field))
        return "break"
    def finish_name_edit(self, row, field, advance=False):
        editor = self.amount_editor
        if not editor: return "break"
        value = editor.get().strip()
        if field == "density" and value:
            try:
                number = Decimal(value.replace(",", "."))
                if not number.is_finite() or number < 0: raise InvalidOperation
            except InvalidOperation:
                self.status.configure(text="Enter a non-negative density or leave the cell empty.", fg=self.colors["error"])
                editor.focus_set(); editor.selection_range(0, "end")
                return "break"
            value = str(number)
        if not value and field == "sample":
            self.status.configure(text=f"{('Molecule' if field == 'molecule' else 'Sample')} name cannot be empty.", fg=self.colors["error"])
            editor.focus_set(); editor.selection_range(0, "end")
            return "break"
        rows = self.editing_rows or [row]
        if value == row[field] and len(rows) == 1:
            if advance: self.queue_next_edit(str(row["id"]), field, advance)
            self.cancel_amount_edit(); self.edit_saved_next_cell(); return "break"
        if advance and len(rows) == 1: self.queue_next_edit(str(row["id"]), field, advance)
        self.cancel_amount_edit()
        if field == "molecule": self._save_molecule_name(rows, value)
        elif field == "density": self._start_sample_edit(rows, "density", self._change_density, value or None)
        else: self._save_sample_name(rows, value)
        return "break"
    def finish_location_edit(self, row, advance=False):
        editor = self.amount_editor
        if not editor: return "break"
        location = editor.get().strip()
        if not location:
            self.status.configure(text="Location cannot be empty.", fg=self.colors["error"])
            editor.focus_set(); editor.selection_range(0, "end")
            return "break"
        rows = self.editing_rows or [row]
        if location == row["location"] and len(rows) == 1:
            if advance: self.queue_next_edit(str(row["id"]), "location", advance)
            self.cancel_amount_edit(); self.edit_saved_next_cell(); return "break"
        if advance and len(rows) == 1: self.queue_next_edit(str(row["id"]), "location", advance)
        self.cancel_amount_edit()
        self.set_controls_busy(True); self.print_button.configure(state="disabled"); self.show_progress()
        self.status.configure(text=f"Changing locations for {len(rows)} sample{'s' if len(rows) != 1 else ''}…", fg=self.colors["accent"])
        threading.Thread(target=self._change_locations, args=(rows, location), daemon=True).start()
        return "break"
    def change_selected_flags(self, item_id, field):
        """Toggle one displayed checkbox for all currently selected samples."""
        selected_ids = self.selected_row_ids or set(self.tree.selection())
        if item_id not in selected_ids:
            # A checkbox click should edit its own row when it is not already
            # part of a multi-selection, rather than retaining an unrelated
            # prior selection.
            selected_ids = {item_id}
            self.tree.selection_set(item_id)
        selected_rows = [row for row in self.filtered_rows() if str(row["id"]) in selected_ids]
        if not selected_rows: return "break"
        value = not next(row for row in selected_rows if str(row["id"]) == item_id)[field]
        self.selected_row_ids = {str(row["id"]) for row in selected_rows}
        label = "Decoupled" if field == "decoupled" else "Inventory"
        self.set_controls_busy(True); self.print_button.configure(state="disabled"); self.show_progress()
        self.status.configure(text=f"Updating {label} for {len(selected_rows)} sample{'s' if len(selected_rows) != 1 else ''}…", fg=self.colors["accent"])
        threading.Thread(target=self._change_flags, args=(selected_rows, field, value, label), daemon=True).start()
        return "break"
    def _change_flags(self, selected_rows, field, value, label):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(selected_rows, start=1):
                self.after(0, self._progress, f"Updating {label}: {position} of {len(selected_rows)}…")
                try:
                    changes = {"decoupled": row["decoupled"], "inventory_sample": row["inventory_sample"]}
                    changes[field] = value
                    client.set_sample_flags(row["id"], row["container"], **changes)
                except Exception as error:
                    failures.append((row, str(error)))
                else:
                    refreshed = client.inventory_entry(row["id"], row.get("collection_id"), row.get("synchronized", False))
                    row.clear(); row.update(refreshed)
            self.after(0, self._flags_changed, selected_rows, field, value, label, failures)
        except Exception as error:
            self.after(0, self._flags_change_failed, label, str(error))
    def _flags_changed(self, selected_rows, field, value, label, failures):
        self.selected_row_ids.clear(); self.render_rows(preserve_display_order=True); self.hide_progress()
        self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        changed = len(selected_rows) - len(failures)
        if failures:
            self.status.configure(text=f"Updated {label} for {changed} of {len(selected_rows)} samples; {len(failures)} could not be changed.", fg=self.colors["error"])
            messagebox.showerror("Chemotion Inventorizer", "Some samples could not be changed:\n\n" + "\n".join(f"{row['sample']}: {error}" for row, error in failures[:5]))
        else:
            self.status.configure(text=f"Set {label} to {'checked' if value else 'unchecked'} for {changed} sample{'s' if changed != 1 else ''}.", fg=self.colors["muted"])
    def _flags_change_failed(self, label, error):
        self.hide_progress(); self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"{label} could not be changed.", fg=self.colors["error"])
        messagebox.showerror("Chemotion Inventorizer", error)
    def open_sample(self, event):
        """Open a sample in Chemotion when a non-editable cell is double-clicked."""
        item_id = self.tree.identify_row(event.y)
        if not item_id or self.displayed_column_key(event) in {"amount", "decoupled", "inventory_sample"}: return
        row = next((item for item in self.rows if str(item["id"]) == item_id), None)
        if not row: return
        collection_id = row.get("collection_id")
        if not collection_id:
            self.status.configure(text="This sample has no Chemotion collection reference.", fg=self.colors["error"])
            return "break"
        route = "scollection" if row.get("synchronized") else "collection"
        webbrowser.open_new_tab(f"{CHEMOTION_URL}/mydb/{route}/{collection_id}/sample/{row['id']}")
        self.set_controls_busy(True); self.print_button.configure(state="disabled"); self.show_progress()
        self.status.configure(text=f"Opened {row['label']} in Chemotion; refreshing sample…", fg=self.colors["accent"])
        threading.Thread(target=self._refresh_opened_sample, args=(row,), daemon=True).start()
        return "break"
    def _refresh_opened_sample(self, row):
        try:
            client = ChemotionClient(); client.login()
            refreshed = client.inventory_entry(row["id"], row.get("collection_id"), row.get("synchronized", False))
            self.after(0, self._entry_refreshed, row, refreshed, f"Refreshed {row['label']} after opening Chemotion.")
        except Exception as error:
            self.after(0, self._opened_sample_refresh_failed, str(error))
    def _opened_sample_refresh_failed(self, error):
        self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text="Chemotion opened, but the sample information could not be refreshed.", fg=self.colors["error"])
        messagebox.showerror("Chemotion Inventorizer", error)
    def cancel_amount_edit(self):
        if self.amount_editor:
            editor, self.amount_editor = self.amount_editor, None
            editor.destroy()
        self.editing_cell = None
        self.editing_rows = []
        # Consume Escape before it reaches the window's stop-loading binding.
        return "break"
    def tree_yview_changed(self, first, last):
        self.vertical_scrollbar.set(first, last)
        self.reposition_active_editor()
    def tree_xview_changed(self, first, last):
        self.horizontal_scrollbar.set(first, last)
        self.reposition_active_editor()
    def place_editor(self, editor, item_id, column):
        editor.configure(font=self.sample_list_font)
        self.editing_cell = (item_id, column)
        self.reposition_active_editor(editor)
    def reposition_active_editor(self, editor=None):
        if not self.editing_cell: return
        editor = editor or self.amount_editor
        if not editor or not editor.winfo_exists(): return
        item_id, column = self.editing_cell
        box = self.tree.bbox(item_id, column)
        if not box:
            editor.place_forget()
            return
        x, y, width, height = box
        editor.place(x=x, y=y, width=width, height=height)
    def selected_edit_rows(self, fallback):
        selected_ids = self.selected_row_ids or set(self.tree.selection())
        rows = [candidate for candidate in self.filtered_rows() if str(candidate["id"]) in selected_ids]
        return rows or [fallback]
    def bind_editor_navigation(self, editor, item_id):
        """Bind commit navigation and the existing discard-and-move arrows."""
        editor.bind("<Up>", lambda _event: self.move_editor_focus(item_id, -1))
        editor.bind("<Down>", lambda _event: self.move_editor_focus(item_id, 1))
        editor.bind("<Shift-Return>", lambda _event: self.commit_editor_navigation(item_id, "up"))
        editor.bind("<Tab>", lambda _event: self.commit_editor_navigation(item_id, "right"))
        editor.bind("<Shift-Tab>", lambda _event: self.commit_editor_navigation(item_id, "left"))
        editor.bind("<ISO_Left_Tab>", lambda _event: self.commit_editor_navigation(item_id, "left"))
    def commit_editor_navigation(self, item_id, direction):
        if not self.editing_cell: return "break"
        _, field = self.editing_cell
        row = next((item for item in self.rows if str(item["id"]) == item_id), None)
        if not row: return "break"
        if field == "amount": return self.finish_amount_edit(row, advance=direction)
        if field == "vendor": return self.finish_vendor_edit(row, advance=direction)
        if field == "location": return self.finish_location_edit(row, advance=direction)
        return self.finish_name_edit(row, field, advance=direction)
    def move_editor_focus(self, item_id, direction):
        if not self.editing_cell: return "break"
        _, field = self.editing_cell
        items = self.tree.get_children()
        try: target_index = items.index(item_id) + direction
        except ValueError: return "break"
        if not 0 <= target_index < len(items): return "break"
        target = items[target_index]
        self.cancel_amount_edit()
        # Arrow navigation deliberately discards the value being typed, just
        # like the previous behaviour, but keeps the inline editor open on
        # the corresponding cell in the adjacent visible row.
        self.edit_after_save = (target, field)
        self.edit_saved_next_cell()
        return "break"
    def next_visible_item(self, item_id):
        items = self.tree.get_children()
        try: return items[items.index(item_id) + 1]
        except (ValueError, IndexError): return None
    def queue_next_edit(self, item_id, field, direction=True):
        if direction in {"left", "right"}:
            editable = {"location", "molecule", "density", "sample", "vendor", "amount"}
            columns = [column for column in self.tree["displaycolumns"] if column in editable]
            try: index = columns.index(field) + (1 if direction == "right" else -1)
            except ValueError:
                self.edit_after_save = None
                return
            self.edit_after_save = (item_id, columns[index]) if 0 <= index < len(columns) else None
            return
        if direction == "up":
            items = self.tree.get_children()
            try: index = items.index(item_id) - 1
            except ValueError: index = -1
            self.edit_after_save = (items[index], field) if index >= 0 else None
            return
        next_item = self.next_visible_item(item_id)
        self.edit_after_save = (next_item, field) if next_item else None
    def edit_saved_next_cell(self):
        target = self.edit_after_save; self.edit_after_save = None
        if not target: return
        item_id, field = target
        row = next((item for item in self.rows if str(item["id"]) == item_id), None)
        if not row or not self.tree.exists(item_id): return
        self.tree.see(item_id); self.tree.selection_set(item_id); self.tree.focus(item_id)
        self.selected_row_ids = {item_id}
        if field == "amount": self.edit_amount_cell(row, item_id)
        elif field == "vendor": self.edit_vendor(row, item_id)
        elif field == "location": self.edit_location(row, item_id)
        else: self.edit_name(row, item_id, field)
    def finish_amount_edit(self, row, advance=False):
        editor = self.amount_editor
        if not editor: return "break"
        try: value, unit = parse_amount(editor.get())
        except ValueError as error:
            self.status.configure(text=str(error), fg=self.colors["error"])
            editor.focus_set(); editor.selection_range(0, "end")
            return "break"
        rows = self.editing_rows or [row]
        if advance and len(rows) == 1: self.queue_next_edit(str(row["id"]), "amount", advance)
        self.cancel_amount_edit(); self._save_amount(rows, value, unit)
        return "break"
    def finish_vendor_edit(self, row, advance=False):
        editor = self.amount_editor
        if not editor: return "break"
        vendor = editor.get().strip()
        rows = self.editing_rows or [row]
        if advance and len(rows) == 1: self.queue_next_edit(str(row["id"]), "vendor", advance)
        self.cancel_amount_edit(); self._save_vendor(rows, vendor)
        return "break"
    def _start_sample_edit(self, rows, label, target, *values):
        self.set_controls_busy(True)
        self.print_button.configure(state="disabled")
        self.show_progress()
        self.status.configure(
            text=f"Updating the {label} for {len(rows)} sample{'s' if len(rows) != 1 else ''}…",
            fg=self.colors["accent"],
        )
        threading.Thread(target=target, args=(rows, *values), daemon=True).start()
    def _save_amount(self, rows, value, unit):
        self._start_sample_edit(rows, "amount", self._change_amount, value, unit)
    def _save_sample_name(self, rows, name):
        self._start_sample_edit(rows, "sample name", self._change_sample_name, name)
    def _change_sample_name(self, rows, name):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(rows, start=1):
                self.after(0, self._progress, f"Updating sample name: {position} of {len(rows)}…")
                try:
                    client.set_sample_name(row["id"], name, row["container"], row["decoupled"], row["inventory_sample"])
                    refresh_inventory_row(client, row)
                except Exception as error: failures.append((row, str(error)))
            self.after(0, self._batch_edit_finished, rows, failures, "sample name")
        except Exception as error:
            self.after(0, self._name_change_failed, "sample name", str(error))
    def _change_density(self, rows, density):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(rows, start=1):
                self.after(0, self._progress, f"Updating density: {position} of {len(rows)}…")
                try:
                    client.set_density(row["id"], density, row["container"], row["decoupled"], row["inventory_sample"])
                    refresh_inventory_row(client, row)
                except Exception as error: failures.append((row, str(error)))
            self.after(0, self._batch_edit_finished, rows, failures, "density")
        except Exception as error:
            self.after(0, self._name_change_failed, "density", str(error))
    def _save_molecule_name(self, rows, name):
        if any(not row.get("molecule_id") for row in rows):
            self.edit_after_save = None
            self.status.configure(text="One or more selected samples have no Chemotion molecule reference.", fg=self.colors["error"])
            return
        self._start_sample_edit(rows, "molecule name", self._change_molecule_name, name)
    def _change_molecule_name(self, rows, name):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(rows, start=1):
                self.after(0, self._progress, f"Updating molecule name: {position} of {len(rows)}…")
                try:
                    name_id = client.create_molecule_name(row["molecule_id"], name) if name else None
                    client.set_molecule_name(row["id"], name_id, row["container"], row["decoupled"], row["inventory_sample"])
                    # A molecule-name edit only changes these two values.  Do
                    # not replace the row from a follow-up request: Chemotion
                    # can briefly return an incomplete sample there, which
                    # made the displayed list disappear during the update.
                    apply_molecule_name_update(row, name, name_id)
                except Exception as error: failures.append((row, str(error)))
            self.after(0, self._batch_edit_finished, rows, failures, "molecule name")
        except Exception as error:
            self.after(0, self._name_change_failed, "molecule name", str(error))
    def _name_changed(self, row, field, name, molecule_name_id=None):
        row[field] = shown(name) if field == "molecule" else name
        if molecule_name_id is not None: row["molecule_name_id"] = molecule_name_id
        self.render_rows(preserve_display_order=True); self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"Updated the {field} name for {row['label']}.", fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _entry_refreshed(self, row, refreshed, status):
        """Replace an edited row with Chemotion's authoritative current record."""
        selected_ids = set(self.selected_row_ids)
        selected_ids.add(str(row["id"]))
        row.clear(); row.update(refreshed)
        self.render_rows(preserve_display_order=True)
        visible_ids = set(self.tree.get_children())
        selected_ids.intersection_update(visible_ids)
        if selected_ids:
            self.tree.selection_set(tuple(selected_ids))
            self.tree.focus(str(row["id"]))
            self.tree.see(str(row["id"]))
        self.selected_row_ids = selected_ids
        self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=status, fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _name_change_failed(self, label, error):
        self.edit_after_save = None
        self.hide_progress(); self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"The {label} could not be changed.", fg=self.colors["error"])
        messagebox.showerror("Chemotion Inventorizer", error)
    def _change_amount(self, rows, value, unit):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(rows, start=1):
                self.after(0, self._progress, f"Updating amount: {position} of {len(rows)}…")
                try:
                    field = row["amount_field"] or "real_amount"
                    client.set_amount(row["id"], field, value, unit, row["container"], row["decoupled"], row["inventory_sample"])
                    refresh_inventory_row(client, row, require_amount=True)
                except Exception as error: failures.append((row, str(error)))
            self.after(0, self._batch_edit_finished, rows, failures, "amount")
        except Exception as error:
            self.after(0, self._amount_change_failed, str(error))
    def _amount_changed(self, row, field, value, unit):
        row["amount_field"] = field; row["amount_value"] = value; row["amount_unit"] = unit
        row["amount"] = "" if is_zero_amount(value) else format_amount_with_unit(value, unit)
        self.render_rows(preserve_display_order=True); self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"Updated the amount for {row['label']} to {row['amount'] or '0 ' + unit}.", fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _amount_change_failed(self, error):
        self.edit_after_save = None
        self.hide_progress(); self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text="The amount could not be changed.", fg=self.colors["error"])
        messagebox.showerror("Chemotion Inventorizer", error)
    def _save_vendor(self, rows, vendor):
        self._start_sample_edit(rows, "vendor", self._change_vendor, vendor)
    def _change_vendor(self, rows, vendor):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(rows, start=1):
                self.after(0, self._progress, f"Updating vendor: {position} of {len(rows)}…")
                try:
                    client.set_vendor(row["id"], row.get("chemical_data"), vendor,
                                      row.get("chemical_id"), row.get("chemical_cas", ""))
                    refresh_inventory_row(client, row)
                except Exception as error: failures.append((row, str(error)))
            self.after(0, self._batch_edit_finished, rows, failures, "vendor")
        except Exception as error:
            self.after(0, self._vendor_change_failed, str(error))
    def _batch_edit_finished(self, rows, failures, label):
        self.render_rows(preserve_display_order=True); self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        changed = len(rows) - len(failures)
        if self.selected_row_ids: self.tree.selection_set(tuple(self.selected_row_ids & set(self.tree.get_children())))
        if failures:
            self.status.configure(text=f"Updated {label} for {changed} of {len(rows)} samples; {len(failures)} could not be changed.", fg=self.colors["error"])
            messagebox.showerror("Chemotion Inventorizer", "Some samples could not be changed:\n\n" + "\n".join(f"{row['sample']}: {error}" for row, error in failures[:5]))
        else:
            self.status.configure(text=f"Updated the {label} for {changed} sample{'s' if changed != 1 else ''}.", fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _vendor_changed(self, row, vendor):
        row["vendor"] = shown(vendor)
        data = row.get("chemical_data") or [{}]
        if not isinstance(data[0], dict): data[0] = {}
        data[0]["vendor"] = vendor; row["chemical_data"] = data
        self.render_rows(preserve_display_order=True); self.hide_progress(); self.set_controls_busy(False)
        self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text=f"Updated the vendor for {row['label']}.", fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _vendor_change_failed(self, error):
        self.edit_after_save = None
        self.hide_progress(); self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        self.status.configure(text="The vendor could not be changed.", fg=self.colors["error"])
        messagebox.showerror("Chemotion Inventorizer", error)
    def _change_locations(self, selected_rows, location):
        failures = []
        try:
            client = ChemotionClient(); client.login()
            for position, row in enumerate(selected_rows, start=1):
                self.after(0, self._progress, f"Changing locations: {position} of {len(selected_rows)}…")
                try:
                    client.set_location(row["id"], location, row["container"], row["decoupled"], row["inventory_sample"])
                    refresh_inventory_row(client, row)
                except Exception as error:
                    failures.append((row, str(error)))
            self.after(0, self._locations_changed, selected_rows, location, failures)
        except Exception as error:
            self.after(0, self._locations_change_failed, str(error))
    def _locations_changed(self, selected_rows, location, failures):
        failed_row_ids = {str(row["id"]) for row, _error in failures}
        self.location_filter_retained_row_ids.update(
            str(row["id"]) for row in selected_rows if str(row["id"]) not in failed_row_ids
        )
        self.update_location_filter(); self.selected_row_ids.clear()
        self.render_rows(preserve_display_order=True); self.hide_progress()
        self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled")
        changed = len(selected_rows) - len(failures)
        if failures:
            self.status.configure(text=f"Changed {changed} of {len(selected_rows)} locations; {len(failures)} could not be changed.", fg=self.colors["error"])
            messagebox.showerror("Chemotion Inventorizer", "Some locations could not be changed:\n\n" + "\n".join(f"{row['sample']}: {error}" for row, error in failures[:5]))
        else: self.status.configure(text=f"Changed the location of {changed} item{'s' if changed != 1 else ''} to {location}.", fg=self.colors["muted"])
        self.edit_saved_next_cell()
    def _locations_change_failed(self, error):
        self.hide_progress(); self.status.configure(text="The selected locations could not be changed.", fg=self.colors["error"]); self.set_controls_busy(False); self.print_button.configure(state="normal" if self.rows else "disabled"); messagebox.showerror("Chemotion Inventorizer", error)
    def choose_print_layout(self):
        """Show print-only column and page-break choices before opening macOS Print."""
        print_keys = tuple(key for key in self.keys if key != "structure") + PRINT_CHECKLIST_KEYS
        preset = print_preset(self.profile_settings.get("print_preset"), print_keys)
        dialog = tk.Toplevel(self)
        dialog.title("Print layout")
        dialog.transient(self)
        dialog.minsize(760, 430)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        print_order = list(preset["column_order"])
        visible = {key: tk.BooleanVar(value=key in preset["visible_columns"]) for key in print_keys}
        names = dict(zip(self.keys, self.headings))
        names.update(dict(zip(PRINT_CHECKLIST_KEYS, PRINT_CHECKLIST_HEADINGS)))
        ttk.Label(body, text="Preview (drag columns to reorder)").grid(row=0, column=0, columnspan=3, sticky="w")
        preview_frame = ttk.Frame(body)
        preview_frame.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(5, 0))
        preview = ttk.Treeview(preview_frame, columns=print_keys, show="headings", height=10)
        preview.tag_configure("alternate", background="#38383a" if self.dark_mode else "#eeeeee")
        preview.grid(row=0, column=0, sticky="nsew")
        preview_scroll = ttk.Scrollbar(preview_frame, orient="horizontal", command=preview.xview)
        preview_scroll.grid(row=1, column=0, sticky="ew")
        preview.configure(xscrollcommand=preview_scroll.set)
        preview_frame.columnconfigure(0, weight=1)
        for key in print_keys:
            preview.heading(key, text=names[key])
            preview.column(key, width=105, minwidth=65, anchor="center" if key in {"decoupled", "inventory_sample", *PRINT_CHECKLIST_KEYS} else "w")
        for index, row in enumerate(self.rows[:10]):
            preview.insert("", "end", iid=f"preview-{index}", values=tuple(displayed_row_value(row, key) if key not in PRINT_CHECKLIST_KEYS else "" for key in print_keys), tags=("alternate",) if index % 2 else ())
        def displayed():
            return tuple(key for key in print_order if visible[key].get())
        def refresh_preview():
            preview.configure(displaycolumns=displayed())
        column_button = ttk.Menubutton(body, text="Columns")
        column_menu = tk.Menu(column_button, tearoff=False)
        column_button.configure(menu=column_menu)
        column_button.grid(row=2, column=0, sticky="w", pady=(10, 0))
        def toggle(key):
            if not displayed():
                visible[key].set(True)
                return
            refresh_preview()
        for key in print_keys:
            column_menu.add_checkbutton(label=names[key], variable=visible[key], command=lambda key=key: toggle(key))
        drag = {"source": None, "target": None, "moving": False}
        feedback = {"view": None}
        def header_key(event):
            if preview.identify_region(event.x, event.y) != "heading":
                return None
            column = preview.identify_column(event.x)
            try:
                return displayed()[int(column[1:]) - 1]
            except (IndexError, ValueError):
                return None
        def begin_drag(event):
            drag.update(source=header_key(event), target=None, moving=False)
            return "break" if drag["source"] else None
        def move_drag(event):
            if not drag["source"]:
                return None
            target = header_key(event)
            if target:
                drag["target"] = target
                drag["moving"] = target != drag["source"]
                if feedback["view"] is None:
                    feedback["view"] = ColumnDragFeedback(preview, self.colors["accent"])
                feedback["view"].show(event)
            return "break"
        def finish_drag(event):
            source = drag["source"]
            target = header_key(event) or drag["target"]
            if source and drag["moving"] and target and source != target:
                print_order.remove(source)
                print_order.insert(print_order.index(target), source)
                refresh_preview()
            drag.update(source=None, target=None, moving=False)
            if feedback["view"] is not None:
                feedback["view"].hide()
            return "break" if source else None
        preview.bind("<ButtonPress-1>", begin_drag)
        preview.bind("<B1-Motion>", move_drag)
        preview.bind("<ButtonRelease-1>", finish_drag)
        refresh_preview()

        split_column = tk.StringVar(value=names.get(preset["split_column"], "Disabled"))
        split_threshold = tk.StringVar(value=str(preset["split_threshold"]))
        split = ttk.Frame(body, padding=10)
        split.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        ttk.Label(split, text="Split pages by attribute:").grid(row=0, column=0, sticky="w")
        column_box = ttk.Combobox(split, textvariable=split_column, values=["Disabled"] + [names[key] for key in self.keys], state="readonly", width=19)
        column_box.grid(row=0, column=1, sticky="w", padx=(7, 0))
        ttk.Label(split, text="Minimum entries:").grid(row=0, column=2, sticky="w", padx=(18, 0))
        ttk.Entry(split, textvariable=split_threshold, width=8).grid(row=0, column=3, sticky="w", padx=(7, 0))

        result = {"value": None}
        def print_with_layout():
            selected = tuple(key for key in print_order if visible[key].get())
            if not selected:
                messagebox.showerror("Print layout", "Choose at least one column.", parent=dialog)
                return
            selected_name = split_column.get()
            selected_key = next((key for key, name in names.items() if name == selected_name), None)
            threshold = 10
            if selected_key:
                try:
                    threshold = int(split_threshold.get())
                    if threshold < 1:
                        raise ValueError
                except ValueError:
                    messagebox.showerror("Print layout", "Minimum entries must be a whole number of at least 1.", parent=dialog)
                    return
            result["value"] = {"column_order": print_order, "visible_columns": selected,
                               "split_column": selected_key,
                               "split_threshold": threshold}
            dialog.destroy()
        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        print_action = ttk.Button(buttons, text="Print…", command=print_with_layout, default="active")
        print_action.pack(side="right", padx=(0, 8))
        dialog.bind("<Return>", lambda _event: print_with_layout())
        dialog.bind("<KP_Enter>", lambda _event: print_with_layout())
        print_action.focus_set()
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()
        self.wait_window(dialog)
        return result["value"]
    def printable(self):
        """Create a location-separated A4 inventory verification checklist."""
        layout = self.choose_print_layout()
        if not layout:
            return
        self.profile_settings["print_preset"] = layout
        save_profile_settings(self.profile_settings)
        path = Path(tempfile.gettempdir()) / "Chemotion-inventory.pdf"
        try:
            rows = self.rows
            write_inventory_pdf(path, rows, layout)
        except OSError as error:
            messagebox.showerror("Chemotion Inventorizer", f"The PDF could not be created:\n\n{error}")
            return
        self.status.configure(text=f"Created a checklist with {len(rows)} items. Choose a printer in the print dialog.", fg=self.colors["muted"])
        # Ask PDFKit for a native NSPrintOperation. This presents the macOS
        # system print panel directly, without opening Preview or submitting a
        # job until the user confirms Print.
        script = f'''ObjC.import("AppKit");
ObjC.import("PDFKit");
const app = $.NSApplication.sharedApplication;
app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
const documentURL = $.NSURL.fileURLWithPath({json.dumps(str(path))});
const document = $.PDFDocument.alloc.initWithURL(documentURL);
const operation = document.printOperationForPrintInfoScalingModeAutoRotate(
    $.NSPrintInfo.sharedPrintInfo, 1, true
);
operation.setShowsPrintPanel(true);
operation.setShowsProgressPanel(true);
app.activateIgnoringOtherApps(true);
operation.runOperation;
'''
        if os.name == "nt":
            # Windows opens the generated PDF in the user's configured PDF
            # app, whose print command provides the system print dialog.
            os.startfile(str(path))
        else:
            subprocess.Popen(["osascript", "-l", "JavaScript", "-e", script])


def pdf_text(value):
    """Encode text for a standard PDF font without letting PDF syntax leak in."""
    value = str(value).encode("cp1252", "replace").decode("cp1252")
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def location_group(location):
    normalized = " ".join(str(location).casefold().split())
    if normalized.startswith("0.563"):
        return "0.563"
    if normalized.startswith("0.830") and "cabinet 138" in normalized:
        return "0.830 cabinet 138"
    if normalized.startswith("0.830") and "cabinet 635" in normalized:
        return "0.830 cabinet 635"
    if normalized.startswith("0.830"):
        return "0.830 others"
    return "Other locations"


def write_inventory_pdf(path, rows, layout=None):
    """Write a compact, dependency-free, portrait A4 PDF checklist."""
    page_width, page_height = 595.28, 841.89  # A4 portrait, in PostScript points.
    margin, line_height = 28, 12
    all_columns = tuple(key for key in Inventorizer.keys if key != "structure") + PRINT_CHECKLIST_KEYS
    layout = print_preset(layout, all_columns)
    printed_keys = tuple(key for key in layout["column_order"] if key in layout["visible_columns"])
    columns = inventory_pdf_columns(rows, page_width - 2 * margin, printed_keys)

    def sort_label(row):
        return natural_sort_value(row["label"])

    pages = []
    split_key = layout["split_column"]
    if split_key:
        groups = {}
        for row in rows:
            group = shown(row.get(split_key))
            groups.setdefault(group, []).append(row)
        unsplit_rows = []
        for group in sorted(groups, key=str.casefold):
            group_rows = sorted(groups[group], key=sort_label)
            threshold = layout["split_threshold"]
            if len(group_rows) < threshold:
                unsplit_rows.extend(group_rows)
                continue
            if unsplit_rows:
                pages.extend(pdf_table_pages("", unsplit_rows, columns, page_width, page_height, margin, line_height, printed_keys))
                unsplit_rows = []
            pages.extend(pdf_table_pages(f" — {group}", group_rows, columns, page_width, page_height, margin, line_height, printed_keys))
        if unsplit_rows:
            pages.extend(pdf_table_pages("", unsplit_rows, columns, page_width, page_height, margin, line_height, printed_keys))
    elif rows:
        pages.extend(pdf_table_pages("", sorted(rows, key=lambda row: (str(row["location"]).casefold(), sort_label(row))), columns, page_width, page_height, margin, line_height, printed_keys))
    if not pages:
        pages = [pdf_table_pages("", [], columns, page_width, page_height, margin, line_height, printed_keys)[0]]

    catalog = "<< /Type /Catalog /Pages 2 0 R >>"
    page_objects, content_objects, page_ids = [], [], []
    for index, stream in enumerate(pages):
        content_id, page_id = 5 + index * 2, 6 + index * 2
        content_objects.append((content_id, f"<< /Length {len(stream.encode('cp1252', 'replace'))} >>\nstream\n{stream}\nendstream"))
        page_objects.append((page_id, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width:.2f} {page_height:.2f}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"))
        page_ids.append(page_id)
    # Text is encoded as Windows-1252 (WinAnsi in PDF terminology).  Declare
    # that explicitly: the base PDF font's default StandardEncoding maps some
    # extended characters, including ®, to different glyphs.
    pdf_objects = [catalog, f"<< /Type /Pages /Kids [{' '.join(f'{ref} 0 R' for ref in page_ids)}] /Count {len(page_ids)} >>",
                   "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                   "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
    for (_content_id, content), (_page_id, page) in zip(content_objects, page_objects): pdf_objects.extend((content, page))
    output = "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"; offsets = [0]
    for number, obj in enumerate(pdf_objects, 1):
        offsets.append(len(output.encode("cp1252"))); output += f"{number} 0 obj\n{obj}\nendobj\n"
    startxref = len(output.encode("cp1252")); output += f"xref\n0 {len(pdf_objects) + 1}\n0000000000 65535 f \n" + "".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:]) + f"trailer\n<< /Size {len(pdf_objects) + 1} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n"
    path.write_bytes(output.encode("cp1252", "replace"))


def inventory_pdf_columns(rows, available_width, printed_keys=("location", "label", "molecule", "amount")):
    """Size the variable PDF columns from the values they actually contain."""
    headings = dict(zip(Inventorizer.keys, Inventorizer.headings))
    headings.update(dict(zip(PRINT_CHECKLIST_KEYS, PRINT_CHECKLIST_HEADINGS)))
    fields = tuple((headings[key], key, 42 if key in {"label", "amount", "decoupled", "inventory_sample"} else 65,
                    190 if key in {"molecule", "sample", "location"} else 125)
                   for key in printed_keys)
    # Helvetica at 9pt averages roughly 4.5pt per character.  The caps retain
    # useful wrapping for unusually long names instead of letting one value
    # consume the entire page.
    desired = []
    for heading, key, minimum, maximum in fields:
        longest = max((len(displayed_row_value(row, key)) for row in rows), default=0)
        desired.append(min(maximum, max(minimum, (max(longest, len(heading)) * 4.5) + 6)))
    variable_width = available_width
    total = sum(desired)
    if total > variable_width:
        minimums = [field[2] for field in fields]
        shrinkable = total - sum(minimums)
        reduction = total - variable_width
        if shrinkable:
            desired = [minimum + (width - minimum) * max(0, 1 - reduction / shrinkable)
                       for width, minimum in zip(desired, minimums)]
        else:
            desired = [variable_width * width / total for width in desired]
    elif total < variable_width:
        # Use spare width for the text-heavy fields rather than leaving a blank
        # strip beside the table, without exceeding a sensible readable cap.
        spare = variable_width - total
        weights = tuple(3 if key in {"molecule", "sample", "location"} else 1
                        for _heading, key, _minimum, _maximum in fields)
        while spare > 0.01:
            eligible = [index for index, field in enumerate(fields) if desired[index] < field[3] - 0.01]
            if not eligible: break
            weight_total = sum(weights[index] for index in eligible)
            used = 0
            for index in eligible:
                maximum = fields[index][3]
                added = min(maximum - desired[index], spare * weights[index] / weight_total)
                desired[index] += added; used += added
            spare -= used
    sized = tuple((heading, width) for (heading, _key, _minimum, _maximum), width in zip(fields, desired))
    return sized


def pdf_table_pages(group, rows, columns, page_width, page_height, margin, line_height, printed_keys):
    x_positions, current = [], margin
    for _title, width in columns: x_positions.append(current); current += width
    table_width = current - margin
    pages, page, y = [], [], page_height - margin
    def text(x, y, value, font="F1", size=9): page.append(f"BT /{font} {size} Tf {x:.1f} {y:.1f} Td ({pdf_text(value)}) Tj ET")
    def new_page(continued=False):
        nonlocal page, y
        if page: pages.append("\n".join(page))
        page, y = [], page_height - margin
        text(margin, y, f"CHEMICAL INVENTORY{group}" + (" (continued)" if continued else ""), "F2", 14); y -= 19
        y -= 15
        page.append(f"0.45 G 0.6 w {margin:.1f} {y - 14:.1f} {table_width:.1f} 16 re S")
        for x, (title, _width) in zip(x_positions, columns): text(x + 2, y - 10, title, "F2", 8)
        y -= 16
    new_page()
    for row_index, row in enumerate(rows):
        values = tuple("" if key in PRINT_CHECKLIST_KEYS else
                       ("Used up" if key == "amount" and "used up" in row["sample"].casefold() else displayed_row_value(row, key))
                       for key in printed_keys)
        height = line_height + 5
        if y - height < margin: new_page(True)
        if row_index % 2:
            page.append(f"0.94 g {margin:.1f} {y - height:.1f} {table_width:.1f} {height:.1f} re f 0 g")
        page.append(f"0.45 G 0.5 w {margin:.1f} {y - height:.1f} {table_width:.1f} {height:.1f} re S")
        for x in x_positions[1:]: page.append(f"0.45 G {x:.1f} {y - height:.1f} m {x:.1f} {y:.1f} l S")
        for index, (x, value, (_heading, width)) in enumerate(zip(x_positions, values, columns)):
            page.append(f"q {x:.1f} {y - height:.1f} {width:.1f} {height:.1f} re W n")
            text(x + 2, y - 10, value, "F2" if printed_keys[index:index + 1] == ("label",) else "F1")
            page.append("Q")
        y -= height
    pages.append("\n".join(page))
    return pages

def main(): Inventorizer().mainloop()
if __name__ == "__main__": main()
