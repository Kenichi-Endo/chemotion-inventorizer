"""Build and check the current platform's standalone application without a GUI."""

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NAME = "Chemotion Inventorizer"
RESOURCES = ROOT / f"{NAME}.app" / "Contents" / "Resources"


def build():
    if sys.platform not in {"darwin", "win32"}:
        raise RuntimeError("Build standalone applications on macOS or Windows.")
    if sys.platform == "win32" and platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("The Windows build requires an x64 Python runtime.")
    platform_name = (f"macos-{'apple-silicon' if platform.machine() == 'arm64' else 'intel'}"
                     if sys.platform == "darwin" else "windows-x64")
    output = ROOT / "dist" / "standalone"
    bundle_output = output / platform_name
    work = ROOT / "build" / "standalone" / platform_name
    work.mkdir(parents=True, exist_ok=True)
    app_version = version("chemotion-inventorizer")
    icon_dir = work / "assets"
    icon_dir.mkdir(exist_ok=True)
    shutil.copy2(RESOURCES / "chemotion-inventorizer-icon.png", icon_dir / "icon.png")
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir" if sys.platform == "darwin" else "--onefile",
        "--windowed", "--noupx", "--name", NAME,
        "--distpath", str(bundle_output), "--workpath", str(work / "work"),
        "--specpath", str(work), "--paths", str(ROOT / "src"),
        "--collect-all", "resvg_py",
        "--copy-metadata", "chemotion-api",
        "--add-data", f"{icon_dir / 'icon.png'}:chemotion_inventorizer/assets",
        "--add-data", f"{ROOT / 'LICENSE'}:.",
    ]
    if sys.platform == "darwin":
        # chemotionapi imports python-magic at startup. Ship its native library
        # so the standalone app does not require a Homebrew installation.
        import magic
        magic_library = magic.libmagic._name
        if not Path(magic_library).is_file():
            raise RuntimeError("Install libmagic with 'brew install libmagic' before building.")
        command.extend([
            "--add-binary", f"{magic_library}:.",
            "--icon", str(RESOURCES / "ChemotionInventorizer.icns"),
            "--osx-bundle-identifier", "io.github.kenichi-endo.chemotion-inventorizer",
            "--target-arch", platform.machine(),
        ])
        # Universal Python otherwise starts native Apple Silicon subprocesses
        # even when this build was requested through Rosetta.
        command = ["arch", f"-{platform.machine()}", *command]
    command.append(str(ROOT / "src" / "chemotion_inventorizer" / "standalone.py"))
    subprocess.run(command, cwd=ROOT, check=True)

    if sys.platform == "darwin":
        bundle = bundle_output / f"{NAME}.app"
        executable = bundle / "Contents" / "MacOS" / NAME
        plist_path = bundle / "Contents" / "Info.plist"
        with plist_path.open("rb") as stream:
            info = plistlib.load(stream)
        info["CFBundleShortVersionString"] = app_version
        info["CFBundleVersion"] = app_version
        with plist_path.open("wb") as stream:
            plistlib.dump(info, stream)
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(bundle)], check=True)
    else:
        executable = bundle_output / f"{NAME}.exe"

    # Run from an unrelated folder with source import overrides removed. A
    # successful check must come from the bundle, not the development checkout.
    with tempfile.TemporaryDirectory(prefix="inventorizer-check-") as directory:
        report = Path(directory) / "self-test.json"
        checked_executable = executable
        if sys.platform == "win32":
            # Check the EXE alone, so leftover build files cannot hide a
            # dependency on an adjacent _internal folder.
            checked_executable = Path(directory) / executable.name
            shutil.copy2(executable, checked_executable)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        try:
            subprocess.run([str(checked_executable), "--self-test", str(report)],
                           cwd=directory, env=environment, check=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            diagnostic = report.with_suffix(".log")
            if diagnostic.is_file():
                print(diagnostic.read_text(encoding="utf-8"), flush=True)
            raise
        result = json.loads(report.read_text(encoding="utf-8"))
        if result.get("frozen") is not True:
            raise RuntimeError("Dependency check did not run in a frozen application.")
        print(json.dumps(result, indent=2))

    archive = output / f"chemotion-inventorizer-{app_version}-{platform_name}.zip"
    if sys.platform == "darwin":
        # ditto retains bundle symlinks and executable permissions in the ZIP.
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(bundle), str(archive)], check=True)
    else:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            zipped.write(executable, executable.name)
    print(f"Standalone archive: {archive}")


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    build()
