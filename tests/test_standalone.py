"""Exercise standalone diagnostics without opening a window or using a server."""

import json

from chemotion_inventorizer import standalone


def test_self_test_cli_checks_dependencies_without_launching_gui(tmp_path, monkeypatch):
    report = tmp_path / "bundle check.json"
    monkeypatch.setattr(standalone.sys, "argv", ["inventorizer", "--self-test", str(report)])

    def unexpected_gui():
        raise AssertionError("The dependency check must not open the application.")

    monkeypatch.setattr(standalone, "launch_app", unexpected_gui)
    standalone.main()

    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["frozen"] is False
    assert result["renderer"] == "ok"
    assert result["https_client"] == "ok"
    assert result["tcl"]


def test_frozen_entry_point_uses_package_icon(tmp_path, monkeypatch):
    package = tmp_path / "chemotion_inventorizer"
    assets = package / "assets"
    assets.mkdir(parents=True)
    icon = assets / "icon.png"
    icon.write_bytes(b"icon")
    monkeypatch.setattr(standalone, "__file__", str(tmp_path / "standalone.py"))
    monkeypatch.setattr(standalone.chemotion_inventorizer, "__file__", str(package / "__init__.py"))
    monkeypatch.setattr(standalone.sys, "frozen", True, raising=False)
    for variable, filename in (("TCL_LIBRARY", "init.tcl"), ("TK_LIBRARY", "tk.tcl")):
        directory = tmp_path / variable
        directory.mkdir()
        (directory / filename).touch()
        monkeypatch.setenv(variable, str(directory))

    report = tmp_path / "self-test.json"
    standalone.self_test(report)
    assert json.loads(report.read_text())["frozen"] is True

    monkeypatch.setattr(standalone.sys, "argv", ["inventorizer"])
    monkeypatch.delenv("CHEMOTION_INVENTORIZER_ICON", raising=False)
    launched = []
    monkeypatch.setattr(standalone, "launch_app", lambda: launched.append(True))
    standalone.main()
    assert standalone.os.environ["CHEMOTION_INVENTORIZER_ICON"] == str(icon)
    assert launched == [True]
