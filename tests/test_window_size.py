"""Keep minimum window dimensions within small and scaled screen sizes."""

import pytest

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
