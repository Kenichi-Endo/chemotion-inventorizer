"""Entry point and non-graphical dependency check for standalone bundles."""

import json
import faulthandler
from importlib.metadata import version
import os
from pathlib import Path
import sys
import traceback

import chemotion_inventorizer


def launch_app():
    from chemotion_inventorizer.__main__ import main
    main()


def self_test(report_path):
    # Import application dependencies inside the diagnostic handler so a
    # windowed executable reports startup errors without an invisible dialog.
    from chemotion_inventorizer import gui
    from chemotion_inventorizer.client import ChemotionClient
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
        with Path(sys.argv[2]).with_suffix(".log").open("w", encoding="utf-8") as log:
            log.write("Loading application dependencies and running the bundle check…\n")
            log.flush()
            faulthandler.dump_traceback_later(45, file=log, exit=True)
            try:
                self_test(sys.argv[2])
            except Exception:
                traceback.print_exc(file=log)
                raise SystemExit(1) from None
            finally:
                faulthandler.cancel_dump_traceback_later()
        return
    icon = Path(chemotion_inventorizer.__file__).resolve().parent / "assets" / "icon.png"
    if icon.is_file():
        os.environ["CHEMOTION_INVENTORIZER_ICON"] = str(icon)
    launch_app()


if __name__ == "__main__":
    main()
