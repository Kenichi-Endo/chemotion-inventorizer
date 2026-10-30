"""Prepare the private Python environment before starting the desktop app."""

import argparse
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import traceback


APP_NAME = "Chemotion Inventorizer"
RUNTIME_CHECK = "import tkinter; import resvg_py; from chemotion_api import Instance"


def state_directory():
    if sys.platform == "win32":
        parent = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(parent) if parent else Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def environment_directory(project_root, state_root):
    project_key = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()
    python_tag = f"{platform.machine()}-{sys.implementation.cache_tag}"
    return state_root / "Environments" / f"{project_key}-{python_tag}"


def ensure_libmagic(python, log):
    """Install macOS libmagic when python-magic cannot load it."""
    if sys.platform != "darwin":
        return
    command = [str(python), "-c", "import magic"]
    if subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode == 0:
        return
    brew = shutil.which("brew")
    if not brew:
        brew = next((str(path) for path in (Path("/opt/homebrew/bin/brew"),
                                           Path("/usr/local/bin/brew")) if path.is_file()), None)
    if not brew:
        raise RuntimeError("libmagic is missing. Install Homebrew from https://brew.sh, "
                           "then reopen Chemotion Inventorizer to install libmagic automatically.")
    log.write("Installing libmagic with Homebrew…\n")
    log.flush()
    subprocess.run([brew, "install", "libmagic"], stdout=log,
                   stderr=subprocess.STDOUT, check=True)
    subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)


def prepare_environment(project_root, state_root, log):
    """Install declared dependencies on first use or after configuration changes."""
    project_file = project_root / "pyproject.toml"
    current_hash = hashlib.sha256(project_file.read_bytes()).hexdigest()
    environment = environment_directory(project_root, state_root)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python3")
    marker = environment / ".chemotion-project-sha256"
    created = not python.is_file()

    if created:
        environment.parent.mkdir(parents=True, exist_ok=True)
        log.write("Creating the private Python environment…\n")
        log.flush()
        subprocess.run([sys.executable, "-m", "venv", str(environment)],
                       stdout=log, stderr=subprocess.STDOUT, check=True)

    installed_hash = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    needs_install = created or installed_hash != current_hash
    repair = False
    if not needs_install:
        ensure_libmagic(python, log)
        repair = subprocess.run([str(python), "-c", RUNTIME_CHECK],
                                stdout=log, stderr=subprocess.STDOUT).returncode != 0
        needs_install = repair

    if needs_install:
        log.write("Installing project dependencies…\n")
        log.flush()
        command = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
        if repair:
            command.append("--force-reinstall")
        command.extend(["-e", str(project_root)])
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        ensure_libmagic(python, log)
        subprocess.run([str(python), "-c", RUNTIME_CHECK],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
        marker.write_text(current_hash + "\n", encoding="utf-8")
        log.write("Setup complete.\n")
        log.flush()
    return python


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup-only", action="store_true",
                        help="install dependencies without opening the application")
    options, app_arguments = parser.parse_known_args(argv)
    project_root = Path(__file__).resolve().parents[2]
    state_root = state_directory()
    log_directory = (Path.home() / "Library" / "Logs" / APP_NAME
                     if sys.platform == "darwin" else state_root / "Logs")
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / "launcher.log"
    with log_path.open("a", encoding="utf-8") as log:
        try:
            if sys.version_info[:2] != (3, 14):
                raise RuntimeError("Python 3.14 with Tk support is required.")
            python = prepare_environment(project_root, state_root, log)
            if options.setup_only:
                print(f"Environment ready: {python.parent.parent}")
                return 0
            command_arguments = ["-m", "chemotion_inventorizer", *app_arguments]
            if sys.platform == "win32":
                pythonw = python.with_name("pythonw.exe")
                if pythonw.is_file():
                    subprocess.Popen([str(pythonw), *command_arguments],
                                     stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, cwd=project_root)
                    return 0
            return subprocess.run([str(python), *command_arguments],
                                  stdout=log, stderr=subprocess.STDOUT,
                                  cwd=project_root).returncode
        except Exception:
            traceback.print_exc(file=log)
            print(f"{APP_NAME} could not start. See {log_path} for details.", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
