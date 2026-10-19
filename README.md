# Chemotion Inventorizer

Chemotion Inventorizer is a desktop app for viewing, bulk-editing, and printing Chemotion sample information.

![Chemotion Inventorizer with anonymized locations and connection details](docs/images/inventory-anonymized.png)

*Inventory view with molecule structures and sample details. Identifying text has been anonymized.*

## Features

- Browse collection inventories in a customizable table with molecule structures and sample details.
- Sort by column and filter by location.
- Edit sample details individually or in bulk directly in Chemotion.
- Remove samples from a collection without deleting them from Chemotion.
- Open samples in Chemotion for further inspection.
- Create A4 PDF checklists for inventory verification.

## Supported environments

- macOS 10.12 or newer (Intel), or macOS 11 or newer (Apple Silicon).
- Windows 10 or newer (64-bit Intel/AMD).

## Setup

1. Install Python 3.10, if needed: [Python for macOS](https://www.python.org/downloads/macos/) or [Python for Windows](https://www.python.org/downloads/windows/). On Windows, select **Add python.exe to PATH** and keep **tcl/tk and IDLE** selected during installation.
2. Clone the repository or download it using **Code → Download ZIP** and extract it. Keep the project folder intact when moving or sharing the app.
3. Open `Chemotion Inventorizer.app` on macOS or double-click `Chemotion Inventorizer.cmd` on Windows. Keep an internet connection available during automatic dependency installation. The first launch may take a few minutes.
4. Enter the Chemotion server root URL (for example, `https://eln.example.org`), your username, and password, then select **Sign in**.
5. Choose a collection.
6. Use **Columns** to choose visible columns and **Location** to filter samples. Drag column headings to reorder them, or click a heading to sort.

## Use

- Press Esc to stop loading samples and keep those already retrieved.
- Click a cell to edit it. An existing location can be chosen from the dropdown. Include a unit with amounts, such as `250 mg`. Press Return/Enter to save directly to Chemotion or Esc to cancel. Move to the next cell with Enter, Shift+Enter, Tab, or  Shift+Tab.
- Use Command/Ctrl-click to select multiple samples, or Shift-click to select a range. Edit a cell in the selection to apply the value to all selected visible rows.
- Double-click a structure or short label to open the sample in your browser.
- Select a sample and press Delete to remove it from the current collection after confirmation. The sample remains in Chemotion and in its other collections.
- Click **Print…** to create an A4 inventory checklist. Choose the columns and their order, page grouping, and optional **Correct** and **Incorrect** check columns. Printing includes all loaded samples, regardless of the filter or selection. On macOS, confirm in the print dialog. On Windows, use the print command in the PDF app that opens.

![Inventory verification checklist with anonymized locations](docs/images/checklist-anonymized.png)

*A4 inventory checklist with verification columns. Locations have been anonymized.*

## Troubleshooting

If macOS reports that the launcher cannot be opened, open Terminal in the extracted project folder and run:

```sh
chmod +x "Chemotion Inventorizer.app/Contents/MacOS/Chemotion Inventorizer"
```

Then reopen the app.

If setup fails, check the internet connection and reopen the launcher. Diagnostic logs are in `~/Library/Logs/Chemotion Inventorizer/launcher.log` on macOS or `%LOCALAPPDATA%\Chemotion Inventorizer\Logs\launcher.log` on Windows.

## Customization

To customize the app, edit the Python files in `src/chemotion_inventorizer/` manually or with a coding agent, then restart the app.

## Run from source

```sh
python3 src/chemotion_inventorizer/bootstrap.py
```

### Install as a command (macOS)

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
inventorizer
```

## Data handling

Sample information is retrieved over HTTPS and held in memory while the app is open. Saved credentials use Keychain on macOS or Credential Manager on Windows. Server and display preferences are saved separately in the current user's profile:

- macOS: `~/Library/Application Support/Chemotion Inventorizer/settings.json`
- Windows: `%APPDATA%\Chemotion Inventorizer\settings.json`

## License

This project is licensed under the [MIT License](LICENSE).
