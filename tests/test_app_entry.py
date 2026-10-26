"""Check both launch entry points without opening the GUI."""
from pathlib import Path
import runpy
import sys

import pytest

from chemotion_inventorizer import app, gui


@pytest.mark.parametrize("entry", ["script", "module"])
def test_entry_starts_gui_event_loop(monkeypatch, entry):
    started = []

    class Window:
        def mainloop(self):
            started.append(True)

    monkeypatch.setattr(gui, "Inventorizer", Window)
    if entry == "script":
        runpy.run_path(str(Path(app.__file__)), run_name="__main__")
    else:
        monkeypatch.delitem(sys.modules, "chemotion_inventorizer.app")
        runpy.run_module("chemotion_inventorizer.app", run_name="__main__")

    assert started == [True]
