"""Entry point and non-graphical dependency check for standalone bundles."""

import json
from importlib.metadata import version
import os
from pathlib import Path
import sys

import chemotion_inventorizer
from chemotion_inventorizer.__main__ import main as launch_app
from chemotion_inventorizer.client import ChemotionClient


def self_test(report_path):
    import resvg_py
    import tkinter

    # Tcl can be initialized without creating a window or contacting Chemotion.
    interpreter = tkinter.Tcl()
    png = resvg_py.svg_to_bytes(
        svg_string='<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
                   '<rect width="8" height="8" fill="black"/></svg>'
    )
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("The bundled structure renderer did not produce a PNG.")
    if getattr(sys, "frozen", False):
        for variable, filename in (("TCL_LIBRARY", "init.tcl"), ("TK_LIBRARY", "tk.tcl")):
            if not (Path(os.environ.get(variable, "")) / filename).is_file():
                raise RuntimeError(f"The bundle is missing {filename}.")
        if not (Path(chemotion_inventorizer.__file__).resolve().parent / "assets" / "icon.png").is_file():
            raise RuntimeError("The bundle is missing the application icon.")
    ChemotionClient()
    Path(report_path).write_text(json.dumps({
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "tcl": interpreter.eval("info patchlevel"),
        "renderer": "ok",
        "https_client": "ok",
        "chemotion_api": version("chemotion-api"),
    }, indent=2) + "\n", encoding="utf-8")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        self_test(sys.argv[2])
        return
    icon = Path(chemotion_inventorizer.__file__).resolve().parent / "assets" / "icon.png"
    if icon.is_file():
        os.environ["CHEMOTION_INVENTORIZER_ICON"] = str(icon)
    launch_app()


if __name__ == "__main__":
    main()
