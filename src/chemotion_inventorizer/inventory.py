"""Inventory row conversion, formatting, and column definitions."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from urllib.parse import urlsplit


INVENTORY_KEYS = ("label", "location", "structure", "molecule", "density", "sample", "vendor", "amount", "last_modified", "decoupled", "inventory_sample")
INVENTORY_HEADINGS = ("Short label", "Location", "Structure", "Molecule name", "Density", "Sample name", "Vendor", "Amount", "Last modified", "Decoupled", "Inventory")

PRINT_CHECKLIST_KEYS = ("correct", "incorrect")


PRINT_CHECKLIST_HEADINGS = ("Correct", "Incorrect")


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


def rows_for_location_filter(rows, location, retained_row_ids=()):
    """Apply a location filter while retaining rows just moved by the user."""
    if location == "All locations":
        return rows
    retained_row_ids = {str(row_id) for row_id in retained_row_ids}
    return [row for row in rows if row["location"] == location or str(row["id"]) in retained_row_ids]


def location_choices(rows):
    """Return the locations offered by both location editors."""
    return tuple(sorted({row["location"] for row in rows}, key=str.casefold))

