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
