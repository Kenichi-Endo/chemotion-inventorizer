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
