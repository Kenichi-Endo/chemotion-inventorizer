"""Keep minimum window dimensions within small and scaled screen sizes."""

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from test_amount_display import app


@pytest.mark.parametrize("screen", [(1024, 600), (1280, 720), (853, 480), (1920, 1080)])
def test_minimum_window_leaves_room_for_system_ui(screen):
    width, height = app.minimum_window_size(*screen)

    assert width <= screen[0] - 80
    assert height <= screen[1] - 160
    assert width <= 1050
    assert height <= 620


def test_large_screen_preserves_previous_minimum():
    assert app.minimum_window_size(1920, 1080) == (1050, 620)


def test_macos_maximizes_on_positioned_display(monkeypatch):
    monkeypatch.setattr(app.sys, "platform", "darwin")
    window = SimpleNamespace(attributes=Mock(), state=Mock())
    app.Inventorizer.maximize_window(window)
    window.attributes.assert_called_once_with("-zoomed", True)
    window.state.assert_not_called()


def test_maximize_fallback_preserves_launch_display(monkeypatch):
    monkeypatch.setattr(app.sys, "platform", "darwin")
    window = SimpleNamespace(
        attributes=Mock(side_effect=app.tk.TclError()),
        geometry=Mock(), _startup_geometry="1920x1030+-1920+25",
    )
    app.Inventorizer.maximize_window(window)
    window.geometry.assert_called_once_with(window._startup_geometry)
