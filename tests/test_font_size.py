"""Check table font preferences without opening a GUI."""
from chemotion_inventorizer import settings

from types import SimpleNamespace

import pytest

from test_amount_display import app


@pytest.mark.parametrize("value, expected", [("24", 24), ("8", 8), ("48", 48),
                                                   ("", None), ("abc", None),
                                                   ("7", None), ("49", None)])
def test_font_size_changes_and_profile_round_trip(value, expected, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROFILE_SETTINGS_PATH", tmp_path / "settings.json")
    sizes = []
    styles = []
    view = SimpleNamespace(
        font_size_value=SimpleNamespace(get=lambda: value),
        sample_list_font=SimpleNamespace(configure=lambda **kwargs: sizes.append(kwargs["size"])),
        style=SimpleNamespace(configure=lambda *args, **kwargs: styles.append(kwargs)),
        table_row_height=lambda: 56,
        profile_settings={"visible_columns": ["label"]},
    )
    app.Inventorizer.change_font_size(view)
    if expected is None:
        assert sizes == styles == []
        assert not settings.PROFILE_SETTINGS_PATH.exists()
    else:
        assert sizes == [expected]
        assert styles == [{"rowheight": 56}]
        assert settings.saved_profile_settings() == {
            "visible_columns": ["label"], "table_font_size": expected}


@pytest.mark.parametrize("structure, linespace, expected", [
    (True, 24, 56), (False, 24, 34), (True, 64, 74), (False, 64, 74),
])
def test_row_height_fits_text_and_structures(structure, linespace, expected):
    view = SimpleNamespace(
        structure_size=50,
        column_visibility={"structure": SimpleNamespace(get=lambda: structure)},
        sample_list_font=SimpleNamespace(metrics=lambda name: linespace),
    )
    assert app.Inventorizer.table_row_height(view) == expected
