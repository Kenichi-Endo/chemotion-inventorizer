"""Collection retrieval order and partial-result stopping, without opening a GUI."""
from chemotion_inventorizer import client as api_client
import json
from collections import deque
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from requests import Response

from chemotion_inventorizer import gui as app


def mock_sample_list(monkeypatch, numbers):
    monkeypatch.setattr(api_client, "CHEMOTION_URL", "https://eln.example.org")
    def send(self, request, **kwargs):
        response = Response()
        response.status_code = 200
        if request.url.endswith("/about"):
            response._content = b"Version: 3.1.2"
            return response
        if request.url.endswith("/api/v1/segments/klasses.json"):
            response._content = b'{"klass": []}'
            return response
        assert request.url.endswith("/api/v1/samples.json")
        response._content = json.dumps({
            "samples": [{"id": number, "type": "sample", "molecule": {}, "short_label": f"S{number}"}
                        for number in numbers],
        }).encode()
        response.headers["X-Total"] = str(len(numbers))
        response.headers["X-Total-Pages"] = "1"
        return response

    monkeypatch.setattr(api_client.InventoryConnection, "send", send)


def prepare_client(monkeypatch):
    monkeypatch.setattr(api_client.ChemotionClient, "login", lambda self: None)
    mock_sample_list(monkeypatch, (2, 10, 1))
    monkeypatch.setattr(api_client.ChemotionClient, "inventory_entry",
                        lambda self, sample_id, *args: {"id": sample_id})
    monkeypatch.setattr(api_client, "MAX_CONCURRENT_SAMPLE_REQUESTS", 1)
    return api_client.ChemotionClient()


def test_inventory_returns_descending_numeric_label_order(monkeypatch):
    client = prepare_client(monkeypatch)
    assert [row["id"] for row in client.inventory(42)] == [10, 2, 1]


def test_sample_list_status_shows_total_without_progress(monkeypatch):
    client = prepare_client(monkeypatch)
    messages = []

    client.inventory(42, progress=messages.append)

    assert "Retrieving the list of 3 samples…" in messages
    assert not any(message.startswith("Retrieving sample list:") for message in messages)


def test_loading_status_shows_escape_stop_hint():
    viewer = app.Inventorizer.__new__(app.Inventorizer)
    updates = []
    viewer.inventory_cancel_event = None
    viewer.colors = {"accent": "accent"}
    viewer.status = SimpleNamespace(configure=lambda **kwargs: updates.append(kwargs))
    viewer.progress = SimpleNamespace(configure=lambda **kwargs: None)

    viewer._progress("Retrieving sample information: 1 of 3…")

    assert updates == [{"text": "Retrieving sample information: 1 of 3… (Esc to stop)", "fg": "accent"}]


def test_escape_cancels_edit_without_stopping_inventory_loading():
    cancelled = threading.Event()
    destroyed = []
    viewer = SimpleNamespace(
        inventory_cancel_event=cancelled, inventory_loading=True,
        amount_editor=SimpleNamespace(destroy=lambda: destroyed.append(True)),
        editing_cell=("10", "sample"), editing_rows=[{"id": 10}],
    )

    assert app.Inventorizer.cancel_amount_edit(viewer) == "break"
    assert destroyed == [True]
    assert viewer.amount_editor is None
    assert viewer.editing_cell is None
    assert viewer.editing_rows == []
    assert viewer.inventory_loading
    assert not cancelled.is_set()


def test_stop_preserves_completed_details(monkeypatch):
    client = prepare_client(monkeypatch)
    cancelled = threading.Event()
    release = threading.Event()

    def entry(self, sample_id, *args):
        if sample_id != 10:
            release.wait(2)
        return {"id": sample_id}

    monkeypatch.setattr(api_client.ChemotionClient, "inventory_entry", entry)
    def progress(message):
        if message.startswith("Retrieving sample information: 1"):
            cancelled.set()

    try:
        with pytest.raises(api_client.InventoryLoadCancelled) as stopped:
            client.inventory(42, progress=progress, cancelled=cancelled)
        assert stopped.value.rows == [{"id": 10, "_inventory_position": 0}]
    finally:
        release.set()


def test_completed_rows_are_delivered_before_loading_finishes(monkeypatch):
    client = prepare_client(monkeypatch)
    cancelled = threading.Event()
    delivered = []
    release = threading.Event()

    def entry(self, sample_id, *args):
        if sample_id != 10:
            release.wait(2)
        return {"id": sample_id}

    monkeypatch.setattr(api_client.ChemotionClient, "inventory_entry", entry)

    def rows_loaded(rows):
        delivered.extend(rows)
        cancelled.set()

    try:
        with pytest.raises(api_client.InventoryLoadCancelled) as stopped:
            client.inventory(42, cancelled=cancelled, rows_loaded=rows_loaded)
        assert delivered == [{"id": 10, "_inventory_position": 0}]
        assert delivered == stopped.value.rows
    finally:
        release.set()


def test_table_updates_during_loading_and_ignores_old_loads():
    current = threading.Event()
    updates = []
    viewer = SimpleNamespace(
        inventory_cancel_event=current, inventory_loading=True, rows=[],
        structure_rows_pending=set(), structure_images={},
        column_visibility={"structure": SimpleNamespace(get=lambda: True)},
        update_location_filter=lambda: updates.append("locations"),
        render_rows=lambda: updates.append("table"),
        load_structure_images=lambda rows: updates.append(("structures", rows)),
    )
    app.Inventorizer._rows_loaded(viewer, [{"id": 10}], current)
    assert viewer.rows == [{"id": 10}]
    assert updates == ["locations", "table", ("structures", [{"id": 10}])]
    app.Inventorizer._rows_loaded(viewer, [{"id": 2}], threading.Event())
    viewer.inventory_loading = False
    app.Inventorizer._rows_loaded(viewer, [{"id": 1}], current)
    assert viewer.rows == [{"id": 10}]


def test_streamed_rows_and_thumbnails_preserve_selection_and_active_edit():
    current = threading.Event()
    items = {"10": {"values": ("S10",)}}
    order = ["10"]
    selected = {"10"}
    editor = SimpleNamespace(text="Uncommitted name", cursor=5)
    repositioned = []

    def insert(parent, index, iid, **options):
        items[iid] = options
        order.insert(index, iid)

    def move(iid, parent, index):
        order.remove(iid)
        order.insert(index, iid)

    def delete(iid):
        items.pop(iid)
        order.remove(iid)
        selected.discard(iid)

    viewer = SimpleNamespace(
        inventory_cancel_event=current, inventory_loading=True,
        rows=[{"id": 10, "label": "S10"}],
        selected_row_ids=selected, amount_editor=editor,
        editing_cell=("10", "sample"), editing_rows=[],
        column_visibility={"structure": SimpleNamespace(get=lambda: True)},
        structure_rows_pending=set(), structure_images={}, structure_image_waiters={},
        update_location_filter=lambda: None, load_structure_images=lambda rows: None,
        sort_key="label", sort_reverse=True,
        row_values=lambda row: (row["label"],),
        cancel_amount_edit=lambda: pytest.fail("Loading cancelled the active edit"),
        reposition_active_editor=lambda: repositioned.append(viewer.editing_cell),
        tree=SimpleNamespace(
            get_children=lambda: tuple(order), delete=delete, insert=insert,
            move=move, item=lambda iid, **options: items[iid].update(options),
        ),
    )
    viewer.filtered_rows = lambda: viewer.rows
    viewer.render_rows = lambda: app.Inventorizer.render_rows(viewer)
    app.Inventorizer._rows_loaded(viewer, [{"id": 20, "label": "S20", "structure": "new.svg"}], current)
    assert order == ["10"]
    viewer.structure_images["new.svg"] = object()
    app.Inventorizer._structure_image_finished(viewer, "new.svg")

    assert order == ["20", "10"]
    assert selected == {"10"}
    assert viewer.amount_editor is editor
    assert (editor.text, editor.cursor) == ("Uncommitted name", 5)
    assert repositioned == [("10", "sample"), ("10", "sample")]


def test_structure_sources_queue_once_in_arrival_order(monkeypatch):
    workers = []

    class Worker:
        def __init__(self, *, target, daemon):
            workers.append((target, daemon))

        def start(self):
            pass

    monkeypatch.setattr(app.threading, "Thread", Worker)
    viewer = SimpleNamespace(
        structure_image_queue_lock=threading.Lock(),
        structure_image_queue=deque(),
        structure_image_pending=set(),
        structure_images={"cached.svg": object()},
        structure_image_worker_active=False,
        _load_structure_images=lambda: None,
    )

    app.Inventorizer.load_structure_images(viewer, [
        {"structure": "first.svg"}, {"structure": "cached.svg"},
    ])
    app.Inventorizer.load_structure_images(viewer, [
        {"structure": "second.svg"}, {"structure": "first.svg"},
    ])

    assert list(viewer.structure_image_queue) == ["first.svg", "second.svg"]
    assert viewer.structure_image_pending == {"first.svg", "second.svg"}
    assert len(workers) == 2
    assert all(daemon for _, daemon in workers)
    app.Inventorizer.load_structure_images(viewer, [
        {"structure": f"extra-{number}.svg"} for number in range(20)
    ])
    assert len(workers) == 8


def test_stop_retains_rows_already_rendered_before_cancellation():
    current = threading.Event()
    completed = []
    viewer = SimpleNamespace(
        inventory_cancel_event=current,
        rows=[{"id": 10}, {"id": 2}],
        _loaded=lambda rows, _cancelled: completed.extend(rows),
    )

    app.Inventorizer._cancelled(viewer, current, [{"id": 10}])

    assert completed == [{"id": 10}, {"id": 2}]


def test_sequential_inventory_waits_for_thumbnail_acknowledgement(monkeypatch):
    client = prepare_client(monkeypatch)
    started = []
    delivered = threading.Event()
    displayed = threading.Event()
    result = []

    def entry(self, sample_id, *args):
        started.append(sample_id)
        return {"id": sample_id}

    def rows_loaded(rows):
        if rows[0]["id"] == 10:
            delivered.set()
            assert displayed.wait(2)

    monkeypatch.setattr(api_client.ChemotionClient, "inventory_entry", entry)
    worker = threading.Thread(target=lambda: result.extend(
        client.inventory(42, rows_loaded=rows_loaded, sequential=True)))
    worker.start()
    try:
        assert delivered.wait(2)
        assert started == [10]
    finally:
        displayed.set()
        worker.join(2)
    assert not worker.is_alive()
    assert [row["id"] for row in result] == [10, 2, 1]


def test_eight_samples_wait_for_structures_before_more_samples_start(monkeypatch):
    client = prepare_client(monkeypatch)
    monkeypatch.setattr(api_client, "MAX_CONCURRENT_SAMPLE_REQUESTS", 8)
    mock_sample_list(monkeypatch, range(12))
    ready = threading.Event()
    displayed = threading.Event()
    lock = threading.Lock()
    arrivals = []
    result = []

    def row_ready(rows):
        with lock:
            arrivals.append(rows[0]["id"])
            if len(arrivals) == 8:
                ready.set()
        assert displayed.wait(2)

    worker = threading.Thread(target=lambda: result.extend(client.inventory(42, row_ready=row_ready)))
    worker.start()
    try:
        assert ready.wait(2)
        assert len(arrivals) == 8
    finally:
        displayed.set()
        worker.join(2)
    assert not worker.is_alive()
    assert [row["id"] for row in result] == list(reversed(range(12)))


def test_stop_releases_wait_for_thumbnail_and_keeps_sample(monkeypatch):
    prepare_client(monkeypatch)
    cancelled = threading.Event()
    delivered = threading.Event()
    retained = []
    errors = []
    started = []

    def entry(self, sample_id, *args):
        started.append(sample_id)
        return {"id": sample_id}

    monkeypatch.setattr(api_client.ChemotionClient, "inventory_entry", entry)
    viewer = SimpleNamespace(
        after=lambda delay, callback, *args: callback(*args),
        _rows_loaded=lambda rows, current, displayed: delivered.set(),
        _progress=lambda *args: None,
        _cancelled=lambda current, rows: retained.extend(rows),
        _loaded=lambda *args: errors.append("unexpected completion"),
        _failed=lambda *args: errors.append(args),
    )
    worker = threading.Thread(target=app.Inventorizer._retrieve,
                              args=(viewer, {"id": 42}, cancelled, True))
    worker.start()
    try:
        assert delivered.wait(2)
    finally:
        cancelled.set()
        worker.join(2)
    assert not worker.is_alive()
    assert not errors
    assert started == [10]
    assert retained == [{"id": 10, "_inventory_position": 0}]


def test_ready_rows_wait_for_all_preceding_details_and_thumbnails():
    inserted = {}
    viewer = SimpleNamespace(
        rows=[], inventory_loading=True,
        column_visibility={"structure": SimpleNamespace(get=lambda: True)},
        structure_rows_pending=set(), structure_images={},
        sort_key="label", sort_reverse=True,
        row_values=lambda row: (row["label"],),
        tree=SimpleNamespace(
            get_children=lambda: tuple(inserted),
            delete=lambda item: inserted.pop(item),
            insert=lambda parent, index, iid, **kwargs: inserted.update({iid: kwargs}),
            item=lambda iid, **kwargs: inserted[iid].update(kwargs),
            move=lambda *args: None,
        ),
    )
    viewer.filtered_rows = lambda: viewer.rows
    second = {"id": 2, "label": "S2", "_inventory_position": 1}
    first = {"id": 10, "label": "S10", "_inventory_position": 0}
    viewer.rows.append(second)
    app.Inventorizer.render_rows(viewer)
    assert not inserted
    viewer.rows.append(first)
    viewer.structure_rows_pending.add(10)
    app.Inventorizer.render_rows(viewer)
    assert not inserted
    viewer.structure_rows_pending.clear()
    app.Inventorizer.render_rows(viewer)
    assert list(inserted) == ["10", "2"]


def test_thumbnail_acknowledgement_follows_image_assignment_and_idle_redraw(monkeypatch):
    source = "/images/samples/own.svg"
    displayed = threading.Event()
    idle_callbacks = []
    assigned = []
    image = object()
    monkeypatch.setattr(app.tk, "PhotoImage", lambda **kwargs: image)
    viewer = SimpleNamespace(
        structure_image_pending={source}, structure_images={},
        structure_rows_pending=set(),
        structure_image_waiters={source: [displayed]},
        rows=[{"id": 10, "structure": source}],
        tree=SimpleNamespace(exists=lambda item: True,
                             item=lambda item, **kwargs: assigned.append((item, kwargs))),
        status=SimpleNamespace(configure=lambda **kwargs: None),
        colors={"muted": "gray"}, after_idle=idle_callbacks.append,
    )
    viewer._structure_image_finished = lambda source: app.Inventorizer._structure_image_finished(viewer, source)
    app.Inventorizer._structure_image_loaded(viewer, source, b"png bytes")
    assert assigned == [("10", {"image": image})]
    assert not displayed.is_set()
    assert len(idle_callbacks) == 1
    idle_callbacks[0]()
    assert displayed.is_set()


def test_row_appears_with_its_thumbnail_without_changing_retrieval_status(monkeypatch):
    current = threading.Event()
    displayed = threading.Event()
    image = object()
    inserted = {}
    idle_callbacks = []
    status_updates = []
    monkeypatch.setattr(app.tk, "PhotoImage", lambda **kwargs: image)
    viewer = SimpleNamespace(
        inventory_cancel_event=current, inventory_loading=True, rows=[],
        column_visibility={"structure": SimpleNamespace(get=lambda: True)},
        structure_rows_pending=set(), structure_images={},
        structure_image_pending=set(), structure_image_waiters={},
        update_location_filter=lambda: None, load_structure_images=lambda rows: None,
        cancel_amount_edit=lambda: None, sort_key="label", sort_reverse=False,
        row_values=lambda row: (row["label"],),
        status=SimpleNamespace(configure=lambda **kwargs: status_updates.append(kwargs)),
        after_idle=idle_callbacks.append,
        tree=SimpleNamespace(
            get_children=lambda: tuple(inserted),
            delete=lambda item: inserted.pop(item),
            insert=lambda parent, where, iid, **kwargs: inserted.update({iid: kwargs}),
            exists=lambda item: item in inserted,
        ),
    )
    viewer.filtered_rows = lambda: viewer.rows
    viewer.render_rows = lambda: app.Inventorizer.render_rows(viewer)
    viewer._structure_image_finished = lambda source: app.Inventorizer._structure_image_finished(viewer, source)
    row = {"id": 10, "label": "S10", "structure": "/images/samples/sample.svg"}

    app.Inventorizer._rows_loaded(viewer, [row], current, displayed)
    assert not inserted
    assert not displayed.is_set()
    app.Inventorizer._structure_image_loaded(viewer, row["structure"], b"png bytes")
    assert inserted["10"]["image"] is image
    assert not status_updates
    assert not displayed.is_set()
    idle_callbacks[0]()
    assert displayed.is_set()
