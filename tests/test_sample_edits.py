"""Check edit refreshes and partial failures without opening the application."""

from types import SimpleNamespace

import pytest

from test_amount_display import app


def test_location_filter_deselects_hidden_samples_without_restoring_them():
    selection = {"1", "2"}
    visible = [{"id": 1}]
    viewer = SimpleNamespace(
        location_filter_retained_row_ids={2}, selected_row_ids={"1", "2"},
        tree=SimpleNamespace(selection=lambda: tuple(selection),
                             selection_set=lambda ids: (selection.clear(), selection.update(ids))),
        filtered_rows=lambda: visible, render_rows=lambda: None,
        column_visibility={"structure": SimpleNamespace(get=lambda: False)},
        status=SimpleNamespace(configure=lambda **kwargs: None), colors={"muted": "gray"},
    )
    app.Inventorizer.filter_by_location(viewer)
    assert selection == viewer.selected_row_ids == {"1"}
    assert not viewer.location_filter_retained_row_ids
    visible.append({"id": 2})
    app.Inventorizer.filter_by_location(viewer)
    assert selection == viewer.selected_row_ids == {"1"}


@pytest.mark.parametrize("failure", [False, True])
def test_opened_sample_refresh_fetches_current_record(monkeypatch, failure):
    row = {"id": 2, "label": "Sample", "collection_id": 7, "synchronized": True}
    refreshed = dict(row, location="New location")
    calls = []

    class Client:
        def login(self):
            calls.append("login")

        def inventory_entry(self, *args):
            calls.append(args)
            if failure:
                raise RuntimeError("Unavailable")
            return refreshed

    monkeypatch.setattr(app, "ChemotionClient", Client)
    viewer = SimpleNamespace(
        after=lambda *args: calls.append(args),
        _entry_refreshed=object(), _opened_sample_refresh_failed=object(),
    )
    app.Inventorizer._refresh_opened_sample(viewer, row)
    assert calls[:2] == ["login", (2, 7, True)]
    if failure:
        assert calls[2] == (0, viewer._opened_sample_refresh_failed, "Unavailable")
    else:
        assert calls[2][:4] == (0, viewer._entry_refreshed, row, refreshed)
    assert "location" not in row  # Row changes must wait for the UI callback.


@pytest.mark.parametrize("item_id, field, direction, expected", [
    ("2", "vendor", "up", ("1", "vendor")),
    ("1", "vendor", "up", None),
    ("2", "vendor", "right", ("2", "sample")),
    ("2", "sample", "left", ("2", "vendor")),
    ("2", "vendor", "left", None),
    ("2", "sample", "right", None),
    ("2", "vendor", True, ("3", "vendor")),
])
def test_commit_navigation_uses_visible_row_and_column_order(item_id, field, direction, expected):
    class Tree:
        def get_children(self):
            return ("1", "2", "3")

        def __getitem__(self, key):
            assert key == "displaycolumns"
            return ("vendor", "label", "sample")

    viewer = SimpleNamespace(tree=Tree())
    viewer.next_visible_item = lambda item: app.Inventorizer.next_visible_item(viewer, item)
    app.Inventorizer.queue_next_edit(viewer, item_id, field, direction)
    assert viewer.edit_after_save == expected


@pytest.mark.parametrize("field", ["amount", "vendor", "location", "sample", "density", "molecule"])
def test_navigation_commits_through_field_validation(field):
    row = {"id": 2}
    committed = []
    viewer = SimpleNamespace(rows=[row], editing_cell=("2", field))
    for name in ("amount", "vendor", "location", "name"):
        setattr(viewer, f"finish_{name}_edit", lambda *args, **kwargs: committed.append((args, kwargs)))
    app.Inventorizer.commit_editor_navigation(viewer, "2", "left")
    args = (row,) if field in {"amount", "vendor", "location"} else (row, field)
    assert committed == [(args, {"advance": "left"})]


@pytest.mark.parametrize("value, expected", [("1,25", "1.25"), ("", None), ("0", "0")])
def test_density_commit_normalizes_value(value, expected):
    rows = [{"id": 1, "density": "—"}, {"id": 2, "density": "2"}]
    saved = []
    viewer = SimpleNamespace(
        amount_editor=SimpleNamespace(get=lambda: value), editing_rows=rows,
        cancel_amount_edit=lambda: None, _change_density=object(),
        _start_sample_edit=lambda *args: saved.append(args),
    )
    app.Inventorizer.finish_name_edit(viewer, rows[0], "density")
    assert saved == [(rows, "density", viewer._change_density, expected)]


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "abc"])
def test_density_commit_rejects_invalid_values(value):
    messages = []
    viewer = SimpleNamespace(
        amount_editor=SimpleNamespace(get=lambda: value, focus_set=lambda: None,
                                      selection_range=lambda *args: None),
        status=SimpleNamespace(configure=lambda **kwargs: messages.append(kwargs)),
        colors={"error": "red"},
    )
    assert app.Inventorizer.finish_name_edit(viewer, {}, "density") == "break"
    assert messages


def test_density_request_preserves_flags():
    import json

    requests = []
    client = SimpleNamespace(request=lambda *args, **kwargs: requests.append((args, kwargs)))
    app.ChemotionClient.set_density(client, 7, "1.25", {"id": 3}, True, False)
    args, kwargs = requests[0]
    assert args[0] == "/api/v1/samples/7"
    assert json.loads(args[1]) == {"density": "1.25", "container": {"id": 3},
                                   "decoupled": True, "inventory_sample": False}
    assert kwargs == {"method": "PUT"}


@pytest.mark.parametrize("fail_first", [False, True])
def test_sample_name_batch_preserves_rows_and_continues_after_failure(monkeypatch, fail_first):
    rows = [
        {"id": sample_id, "collection_id": 42, "synchronized": True,
         "sample": "Original", "container": {}, "decoupled": True,
         "inventory_sample": False, "structure": "original.svg"}
        for sample_id in (1, 2)
    ]
    original = rows[0].copy()
    edits = []
    requests = []
    callbacks = []

    def inventory_entry(sample_id, collection_id, synchronized):
        requests.append((sample_id, collection_id, synchronized))
        if sample_id == 1 and fail_first:
            raise ConnectionError("Refresh failed")
        return {**original, "id": sample_id, "sample": "Renamed"}

    client = SimpleNamespace(
        login=lambda: None,
        set_sample_name=lambda *args: edits.append(args),
        inventory_entry=inventory_entry,
    )
    monkeypatch.setattr(app, "ChemotionClient", lambda: client)
    viewer = SimpleNamespace(
        after=lambda *args: callbacks.append(args),
        _progress=object(), _batch_edit_finished=object(), _name_change_failed=object(),
    )

    app.Inventorizer._change_sample_name(viewer, rows, "Renamed")

    assert requests == [(1, 42, True), (2, 42, True)]
    assert [args[0] for args in edits] == [1, 2]
    assert rows[0] == (original if fail_first else {**original, "sample": "Renamed"})
    assert rows[1]["sample"] == "Renamed"
    assert callbacks[-1][1] is viewer._batch_edit_finished
    failures = callbacks[-1][3]
    assert failures == ([(rows[0], "Refresh failed")] if fail_first else [])


def test_amount_refresh_rejects_missing_value_without_losing_row():
    row = {"id": 1, "amount_value": "2", "structure": "molecule.svg"}
    original = row.copy()
    client = SimpleNamespace(inventory_entry=lambda *args: {"id": 1, "amount_value": None})

    with pytest.raises(ConnectionError, match="did not retain the amount update"):
        app.refresh_inventory_row(client, row, require_amount=True)

    assert row == original


def test_amount_refresh_accepts_zero():
    row = {"id": 1, "amount_value": "2"}
    client = SimpleNamespace(inventory_entry=lambda *args: {"id": 1, "amount_value": 0})

    app.refresh_inventory_row(client, row, require_amount=True)

    assert row == {"id": 1, "amount_value": 0}


@pytest.mark.parametrize("clicked_id, expected_ids", [(1, [1, 2]), (4, [4])])
def test_location_cell_edit_targets_selected_visible_rows(clicked_id, expected_ids):
    rows = [{"id": number, "location": "Old"} for number in (1, 2, 3, 4)]
    opened = []
    viewer = SimpleNamespace(
        rows=rows, selected_row_ids={"1", "2", "3"},
        filtered_rows=lambda: [rows[0], rows[1], rows[3]],
        displayed_column_key=lambda event: "location",
        tree=SimpleNamespace(
            identify_row=lambda y: str(clicked_id), selection=lambda: (),
            selection_set=lambda ids: None, focus=lambda item: None,
        ),
        edit_location=lambda row, item_id: opened.append((row, item_id)),
    )

    app.Inventorizer.edit_amount(viewer, SimpleNamespace(y=10))

    assert [row["id"] for row in viewer.editing_rows] == expected_ids
    assert opened == [(rows[clicked_id - 1], str(clicked_id))]


def test_location_cell_commit_updates_selection_and_preserves_flags(monkeypatch):
    rows = [
        {"id": number, "location": "Old", "container": {"id": number},
         "decoupled": number == 1, "inventory_sample": number == 2,
         "collection_id": 42, "synchronized": True}
        for number in (1, 2)
    ]
    updates = []
    callbacks = []
    client = SimpleNamespace(
        login=lambda: None,
        set_location=lambda *args: updates.append(args),
        inventory_entry=lambda sample_id, *args: {**rows[sample_id - 1], "location": "New"},
    )
    monkeypatch.setattr(app, "ChemotionClient", lambda: client)
    monkeypatch.setattr(
        app.threading, "Thread",
        lambda target, args, daemon: SimpleNamespace(start=lambda: target(*args)),
    )
    viewer = SimpleNamespace(
        amount_editor=SimpleNamespace(get=lambda: " New "), editing_rows=rows.copy(),
        colors={"accent": "blue"}, status=SimpleNamespace(configure=lambda **kwargs: None),
        print_button=SimpleNamespace(configure=lambda **kwargs: None),
        set_controls_busy=lambda busy: None, show_progress=lambda: None,
        after=lambda *args: callbacks.append(args),
        _progress=object(), _locations_changed=object(), _locations_change_failed=object(),
    )

    def cancel_edit():
        viewer.amount_editor = None
        viewer.editing_rows = []

    viewer.cancel_amount_edit = cancel_edit
    viewer._change_locations = lambda *args: app.Inventorizer._change_locations(viewer, *args)

    assert app.Inventorizer.finish_location_edit(viewer, rows[0], advance=True) == "break"

    assert updates == [
        (1, "New", {"id": 1}, True, False),
        (2, "New", {"id": 2}, False, True),
    ]
    assert [row["location"] for row in rows] == ["New", "New"]
    assert callbacks[-1] == (0, viewer._locations_changed, rows, "New", [])
