"""High resolution wheel gestures retain motion and stay on the intended axis."""
from types import SimpleNamespace

import pytest

from chemotion_inventorizer.gui import TableScrollAnimation, enable_table_smooth_scrolling


def drain(callbacks):
    while callbacks:
        callbacks.pop(0)()


@pytest.mark.parametrize("system, delta", [("aqua", 2), ("win32", 24)])
def test_small_deltas_accumulate_and_reverse(system, delta):
    bindings = {}
    movements = []
    tree = SimpleNamespace(
        tk=SimpleNamespace(call=lambda *args: system),
        bind=lambda sequence, callback: bindings.update({sequence: callback}),
        yview_scroll=lambda *args: movements.append(("y", *args)),
        xview_scroll=lambda *args: movements.append(("x", *args)),
    )
    enable_table_smooth_scrolling(tree)
    wheel = bindings["<MouseWheel>"]
    for _ in range(4):
        assert wheel(SimpleNamespace(delta=delta)) == "break"
    assert movements == []
    wheel(SimpleNamespace(delta=delta))
    assert movements == [("y", -1, "units")]
    wheel(SimpleNamespace(delta=delta))
    wheel(SimpleNamespace(delta=-delta * 5))
    assert movements[-1] == ("y", 1, "units")
    bindings["<Shift-MouseWheel>"](SimpleNamespace(delta=delta * 5))
    assert movements[-1] == ("x", -1, "units")
    if system == "aqua":
        bindings["<Option-MouseWheel>"](SimpleNamespace(delta=-delta * 5))
        assert movements[-1] == ("x", 1, "units")


@pytest.mark.parametrize("system, delta, expected", [
    ("aqua", -120, 30), ("win32", -120, 10),
])
def test_pixel_wheel_moves_less_than_one_row(system, delta, expected):
    bindings = {}
    movements = []
    callbacks = []
    tree = SimpleNamespace(
        pixel_scrolling=True,
        tk=SimpleNamespace(call=lambda *args: system),
        bind=lambda sequence, callback, **kwargs: bindings.update({sequence: callback}),
        after=lambda delay, callback: callbacks.append(callback) or len(callbacks),
        yview_scroll=lambda *args: movements.append(args),
    )
    enable_table_smooth_scrolling(tree)
    bindings["<MouseWheel>"](SimpleNamespace(delta=delta))
    assert movements == []
    drain(callbacks)
    assert sum(step for step, unit in movements) == expected
    assert max(step for step, unit in movements) < expected
    assert "<TouchpadScroll>" in bindings


def test_pixel_wheel_preserves_fractional_motion():
    bindings = {}
    movements = []
    callbacks = []
    tree = SimpleNamespace(
        pixel_scrolling=True,
        tk=SimpleNamespace(call=lambda *args: "aqua"),
        bind=lambda sequence, callback, **kwargs: bindings.update({sequence: callback}),
        after=lambda delay, callback: callbacks.append(callback) or len(callbacks),
        yview_scroll=lambda *args: movements.append(args),
    )
    enable_table_smooth_scrolling(tree)
    for _ in range(4):
        bindings["<MouseWheel>"](SimpleNamespace(delta=-1))
    drain(callbacks)
    assert movements == [(1, "units")]


def test_animation_reverses_without_old_motion_and_cancels_on_destroy():
    callbacks = []
    movements = []
    cancelled = []
    tree = SimpleNamespace(
        after=lambda delay, callback: callbacks.append(callback) or len(callbacks),
        after_cancel=cancelled.append,
        yview_scroll=lambda step, unit: movements.append(step),
    )
    animation = TableScrollAnimation(tree)
    animation.add("y", 100)
    animation.add("y", -10)
    drain(callbacks)
    assert sum(movements) == -10
    animation.add("y", 20)
    animation.destroy(SimpleNamespace(widget=tree))
    drain(callbacks)
    assert cancelled
    assert sum(movements) == -10
