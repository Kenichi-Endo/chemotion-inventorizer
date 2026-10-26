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
