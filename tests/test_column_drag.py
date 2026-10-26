"""Exercise heading gestures without creating a GUI window."""

from types import SimpleNamespace

import pytest

from test_amount_display import app


@pytest.mark.parametrize("path, expected_sorts, expected_order", [
    ([10, 11], ["label"], ("label", "location")),
    ([10, 30, 10], [], ("label", "location")),
    ([10, 110, 10], [], ("label", "location")),
    ([110, 10], [], ("location", "label")),
])
def test_heading_click_and_drag(path, expected_sorts, expected_order, monkeypatch):
    sorts = []
    saved = []
    monkeypatch.setattr(app, "save_profile_settings", lambda settings: saved.append(dict(settings)))
    view = SimpleNamespace(
        header_column_key=lambda event: "label" if event.x < 100 else "location",
        sort_by=sorts.append,
        column_order=("label", "location"),
        column_drag_feedback=SimpleNamespace(show=lambda *args: None, hide=lambda: None),
        tree=SimpleNamespace(configure=lambda **kwargs: None, identify_region=lambda x, y: "heading"),
        configure_visible_columns=lambda: None,
        profile_settings={},
        after_idle=lambda callback: None,
        adjust_displayed_column_widths=lambda: None,
    )
    view.visible_column_keys = lambda: view.column_order
    events = [SimpleNamespace(x=x, y=10) for x in path]
    app.Inventorizer.begin_column_drag(view, events[0])
    for event in events[1:]:
        app.Inventorizer.move_column_drag(view, event)
    assert app.Inventorizer.finish_column_drag(view, events[-1]) == "break"
    assert sorts == expected_sorts
    assert view.column_order == expected_order
    assert bool(saved) == (expected_order != ("label", "location"))


def test_drop_marker_uses_a_visible_row():
    tree = SimpleNamespace(
        identify_column=lambda x: "#2",
        winfo_height=lambda: 100,
        identify_row=lambda y: "visible-row" if y >= 26 else "",
        bbox=lambda row, column: (81, 26, 100, 26),
    )
    assert app.column_drop_marker_x(tree, SimpleNamespace(x=130, y=10)) == 81


@pytest.mark.parametrize("scroll, expected", [(0, 101), (0.2, 41)])
def test_empty_table_drop_marker_uses_column_edge(scroll, expected):
    tree = SimpleNamespace(
        identify_column=lambda x: "#2",
        winfo_height=lambda: 100,
        winfo_width=lambda: 200,
        identify_row=lambda y: "",
        cget=lambda option: ("label", "location"),
        column=lambda column, option: {"#1": 100, "#2": 200}[column],
        identify_region=lambda x, y: "heading" if x >= 1 else "nothing",
        xview=lambda: (scroll, 1),
    )
    assert app.column_drop_marker_x(tree, SimpleNamespace(x=130, y=10)) == expected


def test_double_clicking_a_header_separator_fits_heading_and_values(monkeypatch):
    widths = []
    tree = SimpleNamespace(
        identify_region=lambda x, y: "separator",
        identify_column=lambda x: "#2",
        heading=lambda column, option: "Location",
        get_children=lambda parent: ("1", "2"),
        set=lambda item_id, key: {"1": "Box 1", "2": "Long location name"}[item_id],
        column=lambda key, **options: widths.append((key, options["width"])),
    )

    class MeasuredFont:
        def __init__(self, font):
            assert font == "TkDefaultFont"

        def measure(self, text):
            return len(text) * 10

    monkeypatch.setattr(app.tkfont, "Font", MeasuredFont)
    view = SimpleNamespace(
        tree=tree,
        style=SimpleNamespace(lookup=lambda style, option: ""),
        displayed_data_column_keys=lambda: ("label", "location"),
        displayed_column_key=lambda event: "location",
        column_min_widths=app.Inventorizer.column_min_widths,
        remember_column_widths=lambda: None,
    )
    event = SimpleNamespace(x=150, y=10)

    assert app.Inventorizer.auto_size_column(view, event) == "break"
    assert widths == [("location", len("Long location name") * 10 + 24)]


def test_double_clicking_outside_a_header_separator_does_not_resize():
    tree = SimpleNamespace(
        identify_region=lambda x, y: "heading",
        identify_column=lambda x: "#1",
    )
    view = SimpleNamespace(tree=tree)

    assert app.Inventorizer.auto_size_column(view, SimpleNamespace(x=10, y=10)) is None


def test_widths_round_trip_in_profile_including_hidden_columns(monkeypatch):
    saved = []
    monkeypatch.setattr(app, "save_profile_settings", lambda settings: saved.append(dict(settings)))
    widths = {"#0": 140, "label": 180, "location": 310}

    def column(key, option=None, **options):
        if options:
            widths[key] = options["width"]
        return 70 if option == "minwidth" else widths[key]

    view = SimpleNamespace(
        profile_settings={"column_widths": {"location": 310}},
        tree=SimpleNamespace(column=column),
        visible_column_keys=lambda: ("structure", "label"),
        column_visibility={"structure": SimpleNamespace(get=lambda: True)},
        column_min_widths=app.Inventorizer.column_min_widths,
    )
    app.Inventorizer.remember_column_widths(view)
    assert saved[0]["column_widths"] == {"structure": 140, "label": 180, "location": 310}
    widths.update({"#0": 85, "label": 120, "location": 260})
    app.Inventorizer.restore_column_widths(view)
    assert widths == {"#0": 140, "label": 180, "location": 310}


def test_separator_release_saves_after_native_resize():
    callbacks = []
    view = SimpleNamespace(resizing_column=True, after_idle=callbacks.append,
                           remember_column_widths=lambda: None)
    assert app.Inventorizer.finish_column_drag(view, SimpleNamespace()) is None
    assert callbacks == [view.remember_column_widths]
