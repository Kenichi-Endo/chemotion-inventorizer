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
