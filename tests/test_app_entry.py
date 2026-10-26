"""Check package and installed-command entry points without opening the GUI."""
import runpy
import sys

import pytest

from chemotion_inventorizer import __main__ as entry_point, gui


@pytest.mark.parametrize("entry", ["command", "module"])
def test_entry_starts_gui_event_loop(monkeypatch, entry):
    started = []

    class Window:
        def mainloop(self):
            started.append(True)

    monkeypatch.setattr(gui, "Inventorizer", Window)
    if entry == "command":
        entry_point.main()
    else:
        monkeypatch.delitem(sys.modules, "chemotion_inventorizer.__main__")
        runpy.run_module("chemotion_inventorizer", run_name="__main__")

    assert started == [True]
