"""Removal targets the loaded inventory and preserves rows after partial failure."""
from types import SimpleNamespace

import pytest

from chemotion_sample_manager import gui as app


@pytest.mark.parametrize("failure,locked", [(False, False), (True, False), (False, True)])
def test_removal_attempts_every_loaded_collection(monkeypatch, failure, locked):
    calls, completed, errors = [], [], []
    row = {"id": 7, "label": "S7"}

    def remove(collection_id, sample_id, synchronized, source_id):
        calls.append((collection_id, sample_id, synchronized, source_id))
        if failure and collection_id == 1:
            raise ConnectionError("Unavailable")
        return {"locked_sample_ids": ["7"] if locked and collection_id == 1 else []}

    monkeypatch.setattr(app, "ChemotionClient", lambda: SimpleNamespace(
        login=lambda: None, remove_sample_from_collection=remove))
    viewer = SimpleNamespace(
        after=lambda delay, callback, *args: callback(*args),
        _sample_removed_from_collection=lambda *args: completed.append(args),
        _sample_removal_failed=errors.append)
    app.SampleManager._remove_sample_from_collection(viewer, row, [
        {"id": 1, "label": "First"}, {"id": 2, "_synchronized": True, "_source_collection_id": 3}])
    assert calls == [(1, 7, False, None), (2, 7, True, 3)]
    if failure:
        assert not completed
        assert "First: Unavailable" in errors[0]
    else:
        assert not errors
        assert completed == [(row, ["7"] if locked else [])]


def test_delete_uses_loaded_collections_and_confirmation(monkeypatch):
    workers, prompts = [], []
    loaded = [{"id": 1}, {"id": 2, "_synchronized": True}]
    monkeypatch.setattr(app.messagebox, "askyesno", lambda title, message, **kwargs: prompts.append(message) or True)
    monkeypatch.setattr(app.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: workers.append(kwargs)))
    row = {"id": 7, "label": "S7", "sample": "Sample"}
    viewer = SimpleNamespace(
        tree=SimpleNamespace(selection=lambda: ("7",), focus=lambda: "7"), rows=[row],
        loaded_collections=loaded, selected_collections=lambda: [{"id": 99}],
        cancel_amount_edit=lambda: None, set_controls_busy=lambda busy: None,
        print_button=SimpleNamespace(configure=lambda **kwargs: None), show_progress=lambda: None,
        status=SimpleNamespace(configure=lambda **kwargs: None), colors={"accent": "blue"},
        _remove_sample_from_collection=lambda *args: None)
    assert app.SampleManager.delete_selected_row(viewer) == "break"
    assert "all 2 loaded collections" in prompts[0]
    assert workers[0]["args"] == (row, loaded)
    assert workers[0]["args"][1] is not loaded


@pytest.mark.parametrize("locked", [[], ["7"]])
def test_row_is_removed_only_when_no_loaded_collection_keeps_it(locked):
    row = {"id": 7, "label": "S7"}
    viewer = SimpleNamespace(
        rows=[row], selected_row_ids={"7"}, hide_progress=lambda: None,
        set_controls_busy=lambda busy: None, update_location_filter=lambda: None,
        render_rows=lambda **kwargs: None,
        print_button=SimpleNamespace(configure=lambda **kwargs: None),
        status=SimpleNamespace(configure=lambda **kwargs: None), colors={"error": "red", "muted": "gray"})
    app.SampleManager._sample_removed_from_collection(viewer, row, locked)
    assert viewer.rows == ([row] if locked else [])
