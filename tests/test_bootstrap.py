"""Check private environment setup without installing packages or opening Tk."""

import importlib.util
from io import StringIO
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "src" / "chemotion_inventorizer" / "bootstrap.py"
SPEC = importlib.util.spec_from_file_location("inventorizer_bootstrap", SOURCE)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def fake_setup(tmp_path, monkeypatch):
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "pyproject.toml").write_text("dependencies = ['example']\n")
    state = tmp_path / "private state"
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            python = Path(command[3]) / ("Scripts/python.exe" if bootstrap.sys.platform == "win32"
                                         else "bin/python3")
            python.parent.mkdir(parents=True)
            python.touch()
        return SimpleNamespace(returncode=0)

    # Windows platform detection can itself run a subprocess. Keep it outside
    # the fake environment-setup commands, regardless of platform cache state.
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "test-machine")
    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    return root, state, commands


def test_install_once_and_update_when_project_configuration_changes(tmp_path, monkeypatch):
    root, state, commands = fake_setup(tmp_path, monkeypatch)
    python = bootstrap.prepare_environment(root, state, StringIO())
    assert python.is_file()
    assert len([cmd for cmd in commands if "pip" in cmd]) == 1
    commands.clear()

    assert bootstrap.prepare_environment(root, state, StringIO()) == python
    assert not any("pip" in cmd or "venv" in cmd for cmd in commands)
    (root / "pyproject.toml").write_text("dependencies = ['example', 'new']\n")
    bootstrap.prepare_environment(root, state, StringIO())
    assert len([cmd for cmd in commands if "pip" in cmd]) == 1
    assert not any("venv" in cmd for cmd in commands)


def test_failed_install_does_not_mark_environment_as_ready(tmp_path, monkeypatch):
    root, state, commands = fake_setup(tmp_path, monkeypatch)
    successful_run = bootstrap.subprocess.run

    def run(command, **kwargs):
        if "pip" in command:
            raise subprocess.CalledProcessError(1, command)
        return successful_run(command, **kwargs)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.prepare_environment(root, state, StringIO())
    marker = bootstrap.environment_directory(root, state) / ".chemotion-project-sha256"
    assert not marker.exists()


@pytest.mark.parametrize("platform_name", ["darwin", "win32"])
def test_missing_renderer_in_existing_environment_is_repaired(tmp_path, monkeypatch, platform_name):
    monkeypatch.setattr(bootstrap.sys, "platform", platform_name)
    root, state, commands = fake_setup(tmp_path, monkeypatch)
    bootstrap.prepare_environment(root, state, StringIO())
    commands.clear()
    successful_run = bootstrap.subprocess.run

    def run(command, **kwargs):
        if command[-1] == bootstrap.RUNTIME_CHECK and not kwargs.get("check"):
            return SimpleNamespace(returncode=1)
        return successful_run(command, **kwargs)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    bootstrap.prepare_environment(root, state, StringIO())
    pip_commands = [cmd for cmd in commands if "pip" in cmd]
    assert len(pip_commands) == 1
    assert "--force-reinstall" in pip_commands[0]


@pytest.mark.parametrize("platform_name, windowless", [("darwin", False), ("win32", False),
                                                       ("win32", True)])
def test_launcher_runs_package_module(tmp_path, monkeypatch, platform_name, windowless):
    python = tmp_path / "environment" / ("python.exe" if platform_name == "win32" else "python3")
    python.parent.mkdir()
    python.touch()
    if windowless:
        python.with_name("pythonw.exe").touch()
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bootstrap.sys, "platform", platform_name)
    monkeypatch.setattr(bootstrap.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "state_directory", lambda: tmp_path / "state")
    monkeypatch.setattr(bootstrap, "prepare_environment", lambda *args: python)
    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    monkeypatch.setattr(bootstrap.subprocess, "Popen", run)

    assert bootstrap.main(["example argument"]) == 0
    executable = python.with_name("pythonw.exe") if windowless else python
    assert commands[0][0] == [str(executable), "-m", "chemotion_inventorizer", "example argument"]
    assert commands[0][1]["cwd"] == SOURCE.parents[2]


@pytest.mark.skipif(bootstrap.sys.platform == "win32", reason="Requires a POSIX shell")
def test_macos_wrapper_accepts_current_source_layout():
    launcher = SOURCE.parents[2] / "Chemotion Inventorizer.app" / "Contents" / "MacOS" / "Chemotion Inventorizer"
    # Run the wrapper's source checks, stopping before environment setup or GUI launch.
    preflight = launcher.read_text().split("\nexport ", 1)[0]
    result = subprocess.run(["/bin/sh", "-c", preflight, str(launcher)],
                            capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("platform_name, available", [("win32", True), ("darwin", True),
                                                      ("linux", True)])
def test_libmagic_setup_skips_install_when_not_needed(monkeypatch, platform_name, available):
    calls = []
    monkeypatch.setattr(bootstrap.sys, "platform", platform_name)

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0 if available else 1)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    bootstrap.ensure_libmagic(Path("python"), StringIO())
    assert len(calls) == (0 if platform_name == "linux" else 1)
    assert not any("install" in command for command in calls)


@pytest.mark.parametrize("hung", [False, True])
def test_windows_libmagic_repairs_conflicting_binding_before_runtime_check(monkeypatch, hung):
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            assert kwargs["timeout"] == 15
            if hung:
                raise subprocess.TimeoutExpired(command, 15)
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    bootstrap.ensure_libmagic(Path("python.exe"), StringIO())

    assert len(calls) == 3
    assert calls[1][0][-3:] == ["--force-reinstall", "--no-deps", "python-magic-bin==0.4.14"]
    assert calls[1][1]["check"] is True
    assert calls[2][0] == calls[0][0]
    assert calls[2][1]["check"] is True
    assert calls[2][1]["timeout"] == 15


def test_missing_libmagic_is_installed_and_verified(monkeypatch):
    calls = []
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/test/brew")

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0)

    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    bootstrap.ensure_libmagic(Path("python"), StringIO())
    assert calls == [["python", "-c", "import magic"],
                     ["/test/brew", "install", "libmagic"],
                     ["python", "-c", "import magic"]]


def test_missing_homebrew_explains_setup_requirement(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setattr(bootstrap.Path, "is_file", lambda path: False)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError, match="Install Homebrew"):
        bootstrap.ensure_libmagic(Path("python"), StringIO())


def test_failed_libmagic_install_does_not_mark_environment_ready(tmp_path, monkeypatch):
    root, state, commands = fake_setup(tmp_path, monkeypatch)

    def fail(*args):
        raise subprocess.CalledProcessError(1, ["brew", "install", "libmagic"])

    monkeypatch.setattr(bootstrap, "ensure_libmagic", fail)
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.prepare_environment(root, state, StringIO())
    assert not (bootstrap.environment_directory(root, state) / ".chemotion-project-sha256").exists()
