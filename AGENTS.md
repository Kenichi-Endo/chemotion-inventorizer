# Chemotion Inventorizer

- Keep application code in `src/chemotion_inventorizer/` and Python dependencies in `pyproject.toml`.
- The macOS and Windows launchers use `bootstrap.py` to manage a private virtual environment automatically, following PXRD Studio's setup behavior.
- Preserve streamed inventory rows and structure thumbnails when loading is stopped.
- Validate launcher changes without opening the GUI. Use targeted tests and Python compilation checks.
