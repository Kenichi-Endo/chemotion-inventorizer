"""Regex filtering uses the text shown in visible inventory columns."""
from types import SimpleNamespace

import pytest

from chemotion_inventorizer.gui import Inventorizer


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
    assert [row["id"] for row in Inventorizer.filtered_rows(view)] == expected
