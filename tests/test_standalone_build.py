"""Check single-file Windows packaging without a Windows host or a GUI."""

import importlib.util
import json
from pathlib import Path
import zipfile


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "build_standalone.py"
SPEC = importlib.util.spec_from_file_location("standalone_builder", SOURCE)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_windows_archive_contains_only_executable_and_checks_it_in_isolation(tmp_path, monkeypatch):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "chemotion-inventorizer-icon.png").write_bytes(b"icon")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "PyInstaller" in command:
            assert "--onefile" in command
            assert "--onedir" not in command
            output = Path(command[command.index("--distpath") + 1])
            output.mkdir(parents=True)
            (output / f"{builder.NAME}.exe").write_bytes(b"fake executable")
            # Simulate stale output from a previous directory-based build.
            (output / "_internal").mkdir()
            (output / "_internal" / "dependency.dll").write_bytes(b"dependency")
        else:
            checked_executable = Path(command[0])
            assert checked_executable.read_bytes() == b"fake executable"
            assert checked_executable.parent == Path(kwargs["cwd"])
            assert not (checked_executable.parent / "_internal").exists()
            Path(command[2]).write_text(json.dumps({"frozen": True}))

    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "RESOURCES", resources)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.1"\n')
    monkeypatch.setattr(builder.sys, "platform", "win32")
    monkeypatch.setattr(builder.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(builder.subprocess, "run", run)

    builder.build()

    assert len(commands) == 2
    archive = tmp_path / "dist" / "standalone" / "chemotion-inventorizer-0.1.1-windows-x64.zip"
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.namelist() == [f"{builder.NAME}.exe"]
        assert zipped.read(f"{builder.NAME}.exe") == b"fake executable"
