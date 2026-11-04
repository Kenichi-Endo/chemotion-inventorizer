# Chemotion Sample Manager

Chemotion Sample Manager is a desktop app based on [Chemotion API](https://github.com/Chemotion/chemotionapi) for viewing, bulk-editing, and printing [Chemotion ELN](https://chemotion.net/) sample information.

![Chemotion Sample Manager with anonymized locations and connection details](docs/images/inventory-anonymized.png)

*App interface with molecule structures and selected columns. Identifying text has been anonymized.*

## Features
- Browse samples from multiple collections in a customizable table with molecule structures and sample details.
- Sort by column and filter by location or regular expression.
- Edit sample details in bulk.
- Remove samples from collections.
- Open a sample in Chemotion for further inspection.
- Create A4 PDF checklists for sample verification.
- Export a collection.

## Supported environments
- macOS 11+ (Intel or Apple Silicon).
- Windows 10+ (64-bit Intel/AMD).

## Setup
- **Standalone app:** Ready to use without installing Python.
- **Customizable launcher:** Runs the editable Python source and requires Python 3.14. Choose this option to customize the app.

### Standalone app
1. Download the ZIP for your platform from [Releases](https://github.com/Kenichi-Endo/chemotion-sample-manager/releases): **macos-apple-silicon**, **macos-intel**, or **windows-x64**.
2. Extract the ZIP and open the app.
3. On macOS, dismiss the verification warning, then open **System Settings → Privacy & Security → Open Anyway** and confirm.
4. On Windows, when **Windows protected your PC** appears, select **More info → Run anyway**.

### Customizable launcher
1. Install Python 3.14, if not installed: [Python for macOS](https://www.python.org/downloads/macos/) or [Python for Windows](https://www.python.org/downloads/windows/). On Windows, select **Add python.exe to PATH** and keep **tcl/tk and IDLE** selected during installation.
2. Download **Source code (zip)** from [Releases](https://github.com/Kenichi-Endo/chemotion-sample-manager/releases) and extract it, or clone the repository. Keep the project folder intact when moving or sharing the app.
3. Open `Chemotion Sample Manager.app` on macOS or double-click `Chemotion Sample Manager.cmd` on Windows. Keep an internet connection available during automatic dependency installation. The first launch may take a few minutes.

If macOS says **The application “Chemotion Sample Manager.app” cannot be opened**:

1. Open Terminal, type `cd ` (including the space), drag the extracted project folder into Terminal, and press Return.
2. Run:

   ```sh
   chmod +x "Chemotion Sample Manager.app/Contents/MacOS/Chemotion Sample Manager"
   ```

3. Reopen the app.

### Common setup
1. Enter the Chemotion server root URL (for example, `https://eln.example.org`), your username, and password, then select **Sign in**.
2. Select collections (use Cmd/Ctrl/Shift-click for multiple selection).
3. Click **Go**.
4. Use **Columns** to choose visible columns. Drag column headings to reorder them, or click a heading to sort.
5. Adjust **Font size** and **Structure size**.

## Use
- Launch the app.
- If you want to change collections or need only the loaded samples, press Esc or click **Stop**.
- Adjust **Location** or **Filter** to filter the displayed samples.
- Type while outside an input field to select rows by the sorted column.
- To edit one sample, click a cell. For **Location**, an existing location can be chosen from the dropdown. For **Amount**, include a unit such as `250 mg`. Press (Shift+)Return/Enter or Tab to save directly to Chemotion and move to the next cell, or Esc to cancel.
- To edit multiple samples, use Command/Ctrl-click to select multiple samples, or Shift-click to select a range. Edit a cell in the selection to apply the value to all selected rows.
- To check or edit a sample in Chemotion, double-click a structure or short label.
- To remove a sample from all loaded collections, select it and press Delete.
- To verify samples, click **Print…** to create an A4 checklist. Choose the columns and their order, page grouping, and optional **Correct** and **Incorrect** check columns.
- To export a collection, click **Export collection…**. You can continue working while **Export queued** is shown; keep the app open until the completion dialog appears. This can be useful as a backup. Synchronized collections cannot be exported.

![Sample verification checklist with anonymized locations](docs/images/checklist-anonymized.png)

*A4 sample checklist with verification columns. Locations have been anonymized.*

## Customization
Edit the Python files in `src/chemotion_sample_manager/` manually or with a coding agent, then restart the customizable launcher.

## Data handling
Most communication with Chemotion uses the built-in functions of [chemotionapi](https://github.com/Chemotion/chemotionapi) version **312.0.2**: sign-in (`Instance.login()`), loading collections (`Instance.get_root_collection()`, which also calls `RootCollection.load_sync_collection()`), sample listing (`ElementSet.load_elements()` and `ElementSet.next_page()`), sample details (`Instance.get_sample()`), sample edits (`Sample.save()`), and removing samples from collections (`Collection.remove_elements()`). Vendor metadata, creating molecule names, structure images, and collection exports use custom requests through the same authenticated connection (`Connection.get()`, `Connection.post()`, and `Connection.put()`).

Sample information is retrieved over HTTPS and held in memory while the app is open.

With **Remember** enabled, your username and password are saved locally in macOS Keychain or Windows Credential Manager under `io.github.kenichi-endo.chemotion-sample-manager`.

Server and display preferences are saved separately in the current user's profile:

- macOS: `~/Library/Application Support/Chemotion Sample Manager/settings.json`
- Windows: `%APPDATA%\Chemotion Sample Manager\settings.json`

Customizable launcher logs are in `~/Library/Logs/Chemotion Sample Manager/launcher.log` on macOS or `%LOCALAPPDATA%\Chemotion Sample Manager\Logs\launcher.log` on Windows.

## Source files
Files in `src/chemotion_sample_manager/`:

| Source file | Purpose |
| --- | --- |
| `__init__.py` | Package identity and application name. |
| `__main__.py` | Module and installed-command entry point; starts the GUI. |
| `gui.py` | Window, table, dialogs, cell editing, and structure thumbnails. |
| `client.py` | Chemotion sign-in, sample retrieval, sample updates, and collection export. |
| `samples.py` | Sample rows, column definitions, amount and date formatting, sorting, and location filtering. |
| `settings.py` | Saved preferences, column and print settings, and secure credential storage. |
| `printing.py` | PDF checklist layout and generation. |
| `bootstrap.py` | Customizable launcher environment and dependency setup. |
| `standalone.py` | Standalone app entry point and dependency checks. |

## License
This project is licensed under the [MIT License](LICENSE).
