"""Explicit loading and combined inventories from collection selections."""
import threading
from types import SimpleNamespace

import pytest

from chemotion_inventorizer import gui as app


def test_changing_selection_only_updates_controls():
    selected = []
    viewer = SimpleNamespace(
        collection_menu=SimpleNamespace(curselection=lambda: (0, 2), get=lambda index: str(index)),
        collection_value=SimpleNamespace(set=selected.append), inventory_loading=False,
        set_controls_busy=lambda busy: selected.append(busy),
    )
    app.Inventorizer.load_selected_collection(viewer)
    assert selected == ["", False]


@pytest.mark.parametrize("startup, saved_id", [(True, 42), (False, 42), (True, 99)])
def test_restores_selection_and_loads_saved_collections_only_on_startup(startup, saved_id):
    labels = []
    selected = []
    scheduled = []
    refresh = lambda: None
    viewer = SimpleNamespace(
        collection_menu=SimpleNamespace(
            configure=lambda **kwargs: None, delete=lambda *args: None,
            insert=lambda where, label: labels.append(label),
            selection_set=selected.append, see=lambda index: None, curselection=lambda: tuple(selected)),
        login_button=SimpleNamespace(configure=lambda **kwargs: None),
        set_controls_busy=lambda busy: None, load_selected_collection=lambda: None,
        status=SimpleNamespace(configure=lambda **kwargs: None), colors={"muted": "gray"},
        profile_settings={"last_collection": {"id": saved_id, "synchronized": False}},
        startup_load_pending=startup, after_idle=scheduled.append, refresh=refresh,
    )
    app.Inventorizer._collections_loaded(viewer, [{"id": 42, "label": "Inventory"}])
    assert len(labels) == 1
    assert selected == ([0] if saved_id == 42 else [])
    assert scheduled == ([refresh] if startup and saved_id == 42 else [])
    assert not viewer.startup_load_pending


@pytest.mark.parametrize("stop", [False, True])
def test_combined_inventory_deduplicates_streams_and_preserves_stop(monkeypatch, stop):
    calls, streamed, completed = [], [], []
    cancelled = threading.Event()

    def inventory(collection_id, synchronized, progress, event, **kwargs):
        calls.append(kwargs["collections"])
        rows = [{"id": sample_id, "_inventory_position": index}
                for index, sample_id in enumerate((1, 2, 3))]
        kwargs["rows_loaded"](rows)
        if stop:
            raise app.InventoryLoadCancelled(rows)
        return rows

    monkeypatch.setattr(app, "ChemotionClient", lambda: SimpleNamespace(inventory=inventory))
    viewer = SimpleNamespace(
        after=lambda delay, callback, *args: callback(*args),
        _rows_loaded=lambda rows, event: streamed.extend(rows), _progress=lambda *args: None,
        _loaded=lambda rows, event: completed.extend(rows),
        _cancelled=lambda event, rows: completed.extend(rows),
        _failed=lambda *args: pytest.fail(str(args)),
    )
    app.Inventorizer._retrieve(viewer, [{"id": 42}, {"id": 43, "_synchronized": True}], cancelled)
    assert calls == [[{"id": 42}, {"id": 43, "_synchronized": True}]]
    assert [row["id"] for row in streamed] == [1, 2, 3]
    assert [row["id"] for row in completed] == [1, 2, 3]
    assert [row["_inventory_position"] for row in streamed] == [0, 1, 2]
