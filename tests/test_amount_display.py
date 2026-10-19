"""Regression tests for displaying Chemotion sample amounts."""

import importlib.util
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.error import HTTPError

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "src" / "chemotion_inventorizer" / "app.py"
SPEC = importlib.util.spec_from_file_location("inventorizer_app", SOURCE)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


def test_density_column_uses_sample_value_and_follows_molecule_name():
    row = app.inventory_row({"density": 0.789, "name": "Ethanol"})
    index = app.Inventorizer.keys.index("molecule") + 1
    assert app.Inventorizer.keys[index] == "density"
    assert app.Inventorizer.headings[index] == "Density"
    assert app.Inventorizer.row_values(None, row)[index] == "0.789"
    assert app.inventory_row({"density": 0})["density"] == "0"
    assert app.inventory_row({})["density"] == "—"
    previous_order = tuple(key for key in app.Inventorizer.keys if key != "density")
    assert app.column_order(previous_order, app.Inventorizer.keys) == app.Inventorizer.keys
    assert app.visible_columns(previous_order, app.Inventorizer.keys) == previous_order


@pytest.mark.parametrize("source, expected", [
    ("molecule-123.svg", "/images/molecules/molecule-123.svg"),
    ("a molecule.svg", "/images/molecules/a%20molecule.svg"),
    ("/images/molecules/molecule-123.svg", "/images/molecules/molecule-123.svg"),
    ("https://eln.example/images/molecules/molecule-123.svg",
     "https://eln.example/images/molecules/molecule-123.svg"),
])
def test_structure_image_path_resolves_chemotion_svg_sources(source, expected):
    assert app.structure_image_request_path(source) == expected


def test_samples_with_the_same_molecule_keep_their_own_structure_images():
    molecule = {"id": 42, "molecule_svg_file": "general.svg"}
    first = app.inventory_row({"id": 1, "molecule": molecule,
                               "sample_svg_file": "sample one.svg"})
    second = app.inventory_row({"id": 2, "molecule": molecule,
                                "sample_svg_file": "sample-two.svg"})
    assert first["structure"] == "/images/samples/sample%20one.svg"
    assert second["structure"] == "/images/samples/sample-two.svg"


@pytest.mark.parametrize("sample_svg", [None, "", "***"])
def test_structure_falls_back_to_molecule_when_sample_image_is_unavailable(sample_svg):
    row = app.inventory_row({"sample_svg_file": sample_svg,
                             "molecule": {"molecule_svg_file": "general.svg"}})
    assert row["structure"] == "/images/molecules/general.svg"


def test_restricted_structure_uses_chemotion_placeholder_without_a_molecule_image():
    assert app.inventory_row({"sample_svg_file": "***"})["structure"] == "/images/wild_card/no_image_180.svg"
    assert app.inventory_row({})["structure"] == ""


@pytest.mark.parametrize("value", [None, "", " ", "—", 0, 0.0, "0.0", "0.000"])
def test_undefined_density_displays_blank(value):
    row = app.inventory_row({"density": value})
    assert app.displayed_row_value(row, "density") == ""
    index = app.Inventorizer.keys.index("density")
    assert app.Inventorizer.row_values(None, row)[index] == ""


def test_windows_credentials_are_loaded_from_credential_manager(monkeypatch):
    monkeypatch.setattr(app.os, "name", "nt")
    monkeypatch.setattr(app, "windows_saved_credentials", lambda: ("chemist", "secret"))

    assert app.saved_credentials() == ("chemist", "secret")


def test_windows_credentials_are_saved_to_credential_manager(monkeypatch):
    saved = []
    monkeypatch.setattr(app.os, "name", "nt")
    monkeypatch.setattr(app, "windows_save_credentials", lambda username, password: saved.append((username, password)))

    app.save_credentials("chemist", "secret")

    assert saved == [("chemist", "secret")]


def test_windows_credentials_are_cleared_from_credential_manager(monkeypatch):
    cleared = []
    monkeypatch.setattr(app.os, "name", "nt")
    monkeypatch.setattr(app, "windows_clear_credentials", lambda: cleared.append(True))

    app.clear_credentials()

    assert cleared == [True]


def test_multi_selection_uses_control_on_windows_and_command_on_macos():
    assert app.multi_selection_modifier("win32") == "Control"
    assert app.multi_selection_modifier("aqua") == "Command"


def test_combobox_scroll_events_are_consumed_without_changing_its_value():
    assert app.prevent_combobox_scroll_selection() == "break"


def test_chemotion_server_url_is_normalized_for_api_paths():
    assert app.normalized_chemotion_url(" https://eln.example.org/ ") == "https://eln.example.org"


def test_default_server_prefix_requires_a_host_name():
    with pytest.raises(ValueError):
        app.normalized_chemotion_url("https://")


def test_location_filter_keeps_rows_just_moved_outside_the_active_location():
    rows = [
        {"id": 1, "location": "Shelf A"},
        {"id": 2, "location": "Shelf B"},
        {"id": 3, "location": "Shelf A"},
    ]

    displayed = app.rows_for_location_filter(rows, "Shelf A", retained_row_ids={2})

    assert [row["id"] for row in displayed] == [1, 2, 3]


def test_location_filter_does_not_keep_moved_rows_after_the_user_changes_filter():
    rows = [
        {"id": 1, "location": "Shelf A"},
        {"id": 2, "location": "Shelf B"},
    ]

    displayed = app.rows_for_location_filter(rows, "Shelf A")

    assert [row["id"] for row in displayed] == [1]


def test_location_choices_are_sorted_and_shared_by_location_editors():
    rows = [
        {"location": "Shelf B"},
        {"location": "shelf a"},
        {"location": "Shelf B"},
    ]

    assert app.location_choices(rows) == ("shelf a", "Shelf B")


@pytest.mark.parametrize("server", ["eln.example.org", "https://eln.example.org/path", "ftp://eln.example.org"])
def test_chemotion_server_url_requires_an_http_root(server):
    with pytest.raises(ValueError):
        app.normalized_chemotion_url(server)


def test_safe_read_retries_temporary_server_errors(monkeypatch):
    client = app.ChemotionClient()
    calls = 0

    def open_request(request, timeout):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise HTTPError(request.full_url, 500, "Internal Server Error", None,
                            BytesIO(b'{"status":500}'))
        return _Response(b'{"samples": []}')

    monkeypatch.setattr(client.opener, "open", open_request)
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)

    assert client.request("/api/v1/samples") == b'{"samples": []}'
    assert calls == 3


def test_write_does_not_retry_temporary_server_errors(monkeypatch):
    client = app.ChemotionClient()
    calls = 0

    def open_request(request, timeout):
        nonlocal calls
        calls += 1
        raise HTTPError(request.full_url, 500, "Internal Server Error", None,
                        BytesIO(b'{"status":500}'))

    monkeypatch.setattr(client.opener, "open", open_request)

    with pytest.raises(ConnectionError, match="Chemotion returned 500"):
        client.request("/api/v1/samples/1", b"{}", method="PUT")
    assert calls == 1


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def test_small_millilitre_amount_is_not_converted_or_rounded():
    assert app.format_amount_with_unit("0.00168", "mL") == "0.00168 mL"


def test_normal_litre_amount_is_converted_to_millilitres():
    assert app.format_amount_with_unit("0.00168", "L") == "1.68 mL"


def test_volume_input_uses_chemotions_lowercase_l_basis_unit():
    assert app.parse_amount("2 mL") == (app.Decimal("0.002"), "l")


def test_inventory_row_keeps_the_amount_returned_by_chemotion():
    row = app.inventory_row({
        "id": 114,
        "short_label": "INV-114",
        "name": "INV-114",
        "real_amount_value": "0.00168",
        "real_amount_unit": "mL",
    })
    assert row["amount"] == "0.00168 mL"


def test_inventory_row_keeps_the_complete_short_label():
    row = app.inventory_row({"id": 114, "short_label": "INV-114"})

    assert row["label"] == "INV-114"


def test_molecule_name_update_keeps_the_existing_inventory_row():
    row = {
        "id": 114,
        "label": "INV-114",
        "sample": "Sample 114",
        "location": "Shelf A",
        "amount": "2 g",
        "molecule": "Old name",
        "molecule_name_id": 7,
    }

    app.apply_molecule_name_update(row, "New name", 9)

    assert row == {
        "id": 114,
        "label": "INV-114",
        "sample": "Sample 114",
        "location": "Shelf A",
        "amount": "2 g",
        "molecule": "New name",
        "molecule_name_id": 9,
    }


def test_natural_sort_value_orders_numbers_inside_short_labels():
    labels = ["INV-10", "INV-2", "INV-1"]

    assert sorted(labels, key=app.natural_sort_value) == ["INV-1", "INV-2", "INV-10"]


def test_inventory_row_displays_last_modified_in_iso_format():
    row = app.inventory_row({"id": 114, "updated_at": "2026-09-09T08:30:00"})

    assert row["last_modified"] == "2026-09-09 08:30:00"


def test_last_modified_display_uses_iso_offset_format():
    display = app.format_last_modified("2026-09-09T08:30:00+0000")

    assert " " in display
    assert re.search(r"[+-]\d{2}:\d{2}$", display)


def test_last_modified_display_converts_chemotions_localized_timestamp_to_iso():
    assert app.format_last_modified("09.09.2026, 11:48:44") == "2026-09-09 11:48:44"


def test_last_modified_display_converts_localized_timestamp_with_nbsp_and_suffix():
    value = "09.09.2026,\u00a011:48:44 MESZ"

    assert app.format_last_modified(value) == "2026-09-09 11:48:44"


def test_last_modified_is_formatted_at_the_final_display_step():
    row = {
        "last_modified": "09.09.2026, 11:48:44",
        "last_modified_at": "09.09.2026, 11:48:44",
    }

    assert app.displayed_row_value(row, "last_modified") == "2026-09-09 11:48:44"


def test_last_modified_sort_value_orders_dates_chronologically():
    oldest = app.last_modified_sort_value("2025-12-31T23:59:00Z")
    newest = app.last_modified_sort_value("2026-01-01T00:01:00Z")

    assert oldest < newest


def test_last_modified_sort_value_orders_localized_dates_with_suffixes():
    oldest = app.last_modified_sort_value("09.09.2026,\u00a011:18:31 MESZ")
    newest = app.last_modified_sort_value("09.09.2026,\u00a011:48:44 MESZ")

    assert oldest < newest


def test_last_modified_sort_value_places_missing_or_invalid_dates_last():
    valid = app.last_modified_sort_value("2026-09-09T08:30:00Z")

    assert valid < app.last_modified_sort_value(None)
    assert valid < app.last_modified_sort_value("not a date")


def test_status_text_is_white_in_dark_mode():
    assert app.status_text_color(True) == "#f2f2f7"
    assert app.status_text_color(False) == "#000000"


def test_saved_visible_columns_preserve_user_order_and_reject_an_empty_selection():
    columns = ("location", "amount", "last_modified")
    assert app.visible_columns(["last_modified", "location"], columns) == ("last_modified", "location")
    assert app.visible_columns([], columns) == columns


def test_saved_column_order_is_safe_complete_and_keeps_new_columns():
    columns = ("location", "amount", "last_modified")
    assert app.column_order(["last_modified", "location", "unknown", "location"], columns) == (
        "last_modified", "location", "amount"
    )


def test_print_preset_is_independent_from_the_main_table_preferences():
    preset = app.print_preset({
        "column_order": ["amount", "location"],
        "visible_columns": ["amount"],
        "split_column": "location",
        "split_threshold": "12",
    }, ("location", "amount", "last_modified"))

    assert preset == {
        "column_order": ("amount", "location", "last_modified"),
        "visible_columns": ("amount",),
        "split_column": "location",
        "split_threshold": 12,
    }


def test_print_preset_defaults_to_the_legacy_checklist_columns():
    preset = app.print_preset(None, ("label", "location", "molecule", "sample", "amount", "vendor", "correct", "incorrect"))

    assert preset["visible_columns"] == ("location", "label", "molecule", "sample", "amount", "correct", "incorrect")
    assert preset["split_column"] == "location"
    assert preset["split_threshold"] == 10


def test_print_preset_keeps_tuple_columns_from_the_print_dialog():
    preset = app.print_preset({
        "column_order": ("amount", "location", "last_modified"),
        "visible_columns": ("amount", "last_modified"),
    }, ("location", "amount", "last_modified"))

    assert preset["visible_columns"] == ("amount", "last_modified")


def test_synchronized_collection_picker_label_identifies_its_source_account():
    label = app.collection_picker_label({
        "label": "Central inventory",
        "_synchronized": True,
        "sharer": {"name_abbreviation": "inventory-admin"},
    })

    assert label == "(inventory-admin) Central inventory"


def test_synchronized_collection_picker_uses_sharer_not_receiving_user():
    label = app.collection_picker_label({
        "label": "Central inventory",
        "_synchronized": True,
        "user": {"name_abbreviation": "signed-in-user"},
        "shared_by": {"name": "Dr. Smith", "name_abbreviation": "dsmith"},
    })

    assert label == "(dsmith) Central inventory"


def test_synchronized_collection_picker_uses_legacy_chemotion_initials_for_the_user_code():
    label = app.collection_picker_label({
        "label": "Central inventory",
        "_synchronized": True,
        "shared_by": {"name": "Dr. Smith", "initials": "dsmith"},
    })

    assert label == "(dsmith) Central inventory"


def test_collection_picker_omits_nested_collection_containers():
    client = app.ChemotionClient()

    def request(path, data=None, headers=None, method=None):
        if path == "/api/v1/collections":
            return json.dumps({"own": [], "shared_with_me": [{
                "id": 1,
                "label": "with AK-KE",
                "sharer": {"initials": "ZAR"},
                "children": [{"id": 2, "label": "Inventory"}],
            }]}).encode()
        if path == "/api/v1/syncCollections/sync_remote_roots.json":
            return b'{}'
        raise AssertionError(f"Unexpected request: {path}")

    client.request = request

    assert [(item["id"], item["label"]) for item in client.collections()] == [(2, "Inventory")]


def test_collection_picker_sorts_synchronized_collections_by_source_code_then_name():
    collections = [
        {"id": 1, "label": "Zulu", "_synchronized": True, "sharer": {"initials": "BB"}},
        {"id": 2, "label": "Alpha", "_synchronized": True, "sharer": {"initials": "AA"}},
        {"id": 3, "label": "Beta", "_synchronized": True, "sharer": {"initials": "AA"}},
        {"id": 4, "label": "Regular", "_synchronized": False},
    ]

    collections.sort(key=lambda item: (
        bool(item.get("_synchronized")),
        app.synchronized_collection_source(item).casefold() if item.get("_synchronized") and app.synchronized_collection_source(item) else "",
        app.shown(item.get("label")).casefold(),
    ))

    assert [item["id"] for item in collections] == [4, 2, 3, 1]


def test_removing_from_a_synchronized_collection_uses_the_legacy_sync_delete_endpoint():
    client = app.ChemotionClient()
    calls = []

    def request(path, data=None, headers=None, method=None):
        calls.append((path, method, json.loads(data) if data else None))
        return b""

    client.request = request
    client.remove_sample_from_collection(999, 42, synchronized=True)

    assert calls == [("/api/v1/collections/elements/", "DELETE", {
        "ui_state": {
            "currentCollection": {"id": 999, "is_sync_to_me": True},
            "sample": {"checkedAll": False, "checkedIds": [42], "uncheckedIds": []},
        },
    })]


def test_vendor_for_a_new_chemical_record_uses_chemotion_create_endpoint():
    client = app.ChemotionClient()
    calls = []
    client.request = lambda path, body, headers, method: calls.append((path, json.loads(body), method))

    client.set_vendor(173, [], "Merck", chemical_id=None, cas="50-00-0")

    assert calls == [("/api/v1/chemicals/create", {
        "sample_id": 173, "chemical_data": [{"vendor": "Merck"}], "cas": "50-00-0"
    }, "POST")]


def test_vendor_for_an_existing_chemical_record_uses_update_endpoint():
    client = app.ChemotionClient()
    calls = []
    client.request = lambda path, body, headers, method: calls.append((path, json.loads(body), method))

    client.set_vendor(173, [{"order_number": "ABC-1"}], "Merck", chemical_id=42)

    assert calls == [("/api/v1/chemicals", {
        "sample_id": 173, "chemical_data": [{"order_number": "ABC-1", "vendor": "Merck"}]
    }, "PUT")]


def test_removing_a_sample_only_deletes_its_collection_membership():
    client = app.ChemotionClient()
    calls = []
    client.request = lambda path, data=None, headers=None, method=None: calls.append((path, json.loads(data), headers, method)) or b""

    client.remove_sample_from_collection(42, 173)

    assert calls == [("/api/v1/collection_elements/42", {
        "collection_id": 42,
        "ui_state": {
            "currentCollection": {"id": 42},
            "sample": {"checkedAll": False, "checkedIds": [173], "uncheckedIds": []},
        },
    }, {"Accept": "application/json", "Content-Type": "application/json"}, "DELETE")]
