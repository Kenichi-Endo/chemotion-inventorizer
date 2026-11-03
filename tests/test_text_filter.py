"""Regex filtering uses the text shown in visible inventory columns."""
from types import SimpleNamespace

import pytest

from chemotion_sample_manager.gui import SampleManager
from chemotion_sample_manager import settings


@pytest.mark.parametrize("pattern, columns, location, expected", [
    ("", ("sample",), "All locations", [1, 2]),
    ("^eth.*ol$", ("sample",), "All locations", [1]),
    ("ethanol|acetone", ("sample",), "All locations", [1, 2]),
    ("vendor", ("sample",), "All locations", []),
    ("vendor", ("vendor",), "All locations", [1]),
    (".*", ("sample",), "Shelf A", [1]),
    ("[", ("sample",), "All locations", []),
])
def test_regex_filter(pattern, columns, location, expected):
    view = SimpleNamespace(
        rows=[
            {"id": 1, "sample": "Ethanol", "vendor": "Vendor", "location": "Shelf A"},
            {"id": 2, "sample": "Acetone", "vendor": "Other", "location": "Shelf B"},
        ],
        location_filter=SimpleNamespace(get=lambda: location),
        location_filter_retained_row_ids=set(),
        text_filter=SimpleNamespace(get=lambda: pattern),
        keys=("sample", "vendor"),
        visible_column_keys=lambda: columns,
        row_values=lambda row: (row["sample"], row["vendor"]),
    )
    assert [row["id"] for row in SampleManager.filtered_rows(view)] == expected


@pytest.mark.parametrize("pattern, location", [("^eth.*ol$", "Shelf B"), ("", "All locations")])
def test_filters_profile_round_trip(pattern, location, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROFILE_SETTINGS_PATH", tmp_path / "settings.json")
    view = SimpleNamespace(
        profile_settings={"table_font_size": 18},
        text_filter=SimpleNamespace(get=lambda: pattern),
        location_filter=SimpleNamespace(get=lambda: location),
    )
    SampleManager.save_filters(view)
    assert settings.saved_profile_settings() == {
        "table_font_size": 18, "text_filter": pattern, "location_filter": location,
    }


def test_saved_location_survives_partial_loading():
    menus = []
    view = SimpleNamespace(
        rows=[],
        location_filter=SimpleNamespace(get=lambda: "Shelf B"),
        location_filter_menu=SimpleNamespace(configure=lambda **kwargs: menus.append(kwargs["values"])),
    )
    SampleManager.update_location_filter(view)
    view.rows = [{"location": "Shelf A"}]
    SampleManager.update_location_filter(view)
    view.rows.append({"location": "Shelf B"})
    SampleManager.update_location_filter(view)
    assert menus == [
        ("All locations", "Shelf B"),
        ("All locations", "Shelf A", "Shelf B"),
        ("All locations", "Shelf A", "Shelf B"),
    ]


def test_edit_refresh_keeps_filter_membership_until_filter_changes():
    rows = [{"id": 1, "sample": "Changed", "location": "Shelf B"},
            {"id": 2, "sample": "Ethanol", "location": "Shelf A"}]
    displayed = {"1": {}}
    view = SimpleNamespace(
        rows=rows, sort_key="sample", sort_reverse=False,
        tree=SimpleNamespace(
            get_children=lambda: tuple(displayed),
            item=lambda item_id, **options: displayed.update({item_id: options}),
            move=lambda *args: None,
            selection=lambda: (), selection_set=lambda *args: None,
        ),
        location_filter=SimpleNamespace(get=lambda: "Shelf A"),
        location_filter_retained_row_ids=set(),
        text_filter=SimpleNamespace(get=lambda: "Ethanol"),
        column_visibility={"structure": SimpleNamespace(get=lambda: False)},
        structure_images={}, keys=("sample",),
        visible_column_keys=lambda: ("sample",), row_values=lambda row: (row["sample"],),
    )
    view.filtered_rows = lambda: SampleManager.filtered_rows(view)
    SampleManager.render_rows(view, preserve_display_order=True)
    assert tuple(displayed) == ("1",)
    assert displayed["1"]["values"] == ("Changed",)
    assert view.filtered_rows() == [rows[0]]
    view.selected_row_ids = set()
    view.status = SimpleNamespace(configure=lambda **kwargs: None)
    view.colors = {"muted": "gray"}
    view.render_rows = lambda: None
    SampleManager.filter_by_location(view)
    assert view.filtered_rows() == [rows[1]]
