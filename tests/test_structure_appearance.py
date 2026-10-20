"""Check thumbnail colors and theme switching without opening Tk windows."""

from types import SimpleNamespace
import zlib

import pytest
import resvg_py

from test_amount_display import app


def pixel(png):
    offset = 8
    compressed = b""
    while offset < len(png):
        size = int.from_bytes(png[offset:offset + 4], "big")
        if png[offset + 4:offset + 8] == b"IDAT":
            compressed += png[offset + 8:offset + 8 + size]
        offset += size + 12
    # A single RGBA pixel has no preceding pixel or row, so all PNG filter
    # predictors are zero and its stored bytes are its channel values.
    raw = zlib.decompress(compressed)
    assert len(raw) == 5
    return tuple(raw[1:])


@pytest.mark.parametrize("color, opacity, expected", [
    ("black", "1", (255, 255, 255, 255)),
    ("white", "1", (0, 0, 0, 255)),
    ("red", "0.5", (0, 255, 255, 128)),
])
def test_inversion_preserves_alpha(color, opacity, expected):
    png = resvg_py.svg_to_bytes(svg_string=(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">'
        f'<rect width="1" height="1" fill="{color}" opacity="{opacity}"/></svg>'
    ))

    assert pixel(app.inverted_structure_png(png, 1, 1)) == expected


def test_switching_theme_reuses_images_and_keeps_stopped_rows():
    normal, inverted = object(), object()
    rows = [{"id": 1, "structure": "molecule.svg"}]
    assigned = []
    viewer = SimpleNamespace(
        dark_mode=True, rows=rows,
        structure_images={"molecule.svg": normal},
        structure_image_variants={"molecule.svg": (normal, inverted)},
        tree=SimpleNamespace(exists=lambda item: True,
                             item=lambda item, **kwargs: assigned.append(kwargs["image"])),
    )

    app.Inventorizer.refresh_structure_appearance(viewer)
    viewer.dark_mode = False
    app.Inventorizer.refresh_structure_appearance(viewer)

    assert assigned == [inverted, normal]
    assert viewer.rows is rows
    assert viewer.structure_images["molecule.svg"] is normal


@pytest.mark.parametrize("value, expected", [("100", 100), ("20", 20), ("200", 200),
                                           ("", None), ("19", None), ("201", None)])
def test_structure_size_updates_cached_images_and_profile(value, expected, monkeypatch):
    saved, rendered, columns = [], [], []
    monkeypatch.setattr(app, "save_profile_settings", lambda settings: saved.append(dict(settings)))
    viewer = SimpleNamespace(
        structure_size=50, structure_size_value=SimpleNamespace(get=lambda: value),
        structure_svg_data={"molecule.svg": b"svg"},
        column_visibility={"structure": SimpleNamespace(get=lambda: True)},
        tree=SimpleNamespace(column=lambda *args, **kwargs: columns.append(kwargs)),
        style=SimpleNamespace(configure=lambda *args, **kwargs: None),
        table_row_height=lambda: 106,
        _structure_image_loaded=lambda *args: rendered.append(args), profile_settings={},
    )
    app.Inventorizer.change_structure_size(viewer)
    assert viewer.structure_size == (expected or 50)
    assert saved == ([{"structure_size": expected}] if expected else [])
    assert rendered == ([("molecule.svg", b"svg")] if expected else [])
    assert columns == ([{"width": round(expected * 1.5) + 10,
                         "minwidth": round(expected * 1.5) + 10}] if expected else [])


def test_resizing_renders_cached_svg_at_current_size(monkeypatch):
    images = []
    monkeypatch.setattr(app.tk, "PhotoImage", lambda **kwargs: images.append(kwargs) or object())
    viewer = SimpleNamespace(
        structure_size=100, structure_image_pending=set(),
        structure_svg_data={"source": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 75 50"/>'},
        structure_image_variants={}, structure_images={}, rows=[], dark_mode=False,
        _structure_image_finished=lambda source: None,
        _structure_image_failed=lambda *args: pytest.fail(str(args)),
    )
    app.Inventorizer._structure_image_loaded(viewer, "source", b"ignored")
    png = app.base64.b64decode(images[0]["data"])
    assert int.from_bytes(png[16:20], "big") == 150
    assert int.from_bytes(png[20:24], "big") == 100
    assert len(images) == 2
