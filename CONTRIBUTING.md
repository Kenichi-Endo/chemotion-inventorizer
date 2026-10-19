# Contributing

Contributions are welcome. Please open an issue before substantial changes so the proposed direction can be discussed.

## Local checks

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
python -m compileall -q src tests
python -m build
```

On Windows, activate the environment with `.venv\Scripts\activate` instead. Python must include Tk support. Tests run without opening the application or connecting to a Chemotion server.

Do not commit laboratory inventory data. Use anonymized data for examples and test fixtures.

## Standalone builds

Build on each target platform with Python including Tk support:

```sh
python -m pip install -e '.[dev,standalone]'
python scripts/build_standalone.py
```

The ZIP is written to `dist/standalone/`. On GitHub, run **Actions → Standalone apps → Run workflow** to build Apple Silicon, Intel Mac, and Windows x64 downloads. Download the resulting workflow artifacts for testing before adding them to a release.

macOS ZIPs contain a self-contained `.app`; the Windows ZIP contains a single `.exe` with no accompanying dependency folder. Extract before opening. Python is not required on the receiving machine.

Check each extracted app on a machine without Python, including sign-in, structures, edits, and printing. The automated bundle check does not open the GUI. macOS builds are ad hoc signed, without Developer ID signing or notarization.
