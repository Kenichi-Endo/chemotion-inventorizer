"""Preferences and platform credential storage."""
import json
import os
import re
import subprocess
from pathlib import Path
from .inventory import (
    PRINT_CHECKLIST_KEYS,
    as_dict,
)


KEYCHAIN_SERVICE = "io.github.kenichi-endo.chemotion-inventorizer"
WINDOWS_CREDENTIAL_TARGET = KEYCHAIN_SERVICE


def profile_settings_path():
    """Return the per-user settings location for the current platform."""
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        if app_data:
            return Path(app_data) / "Chemotion Inventorizer" / "settings.json"
        return Path.home() / "AppData" / "Roaming" / "Chemotion Inventorizer" / "settings.json"
    return Path.home() / "Library" / "Application Support" / "Chemotion Inventorizer" / "settings.json"


PROFILE_SETTINGS_PATH = profile_settings_path()


def saved_profile_settings():
    """Read display preferences saved for the signed-in Mac user."""
    try:
        with PROFILE_SETTINGS_PATH.open(encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def save_profile_settings(settings):
    """Persist non-sensitive app preferences in the Mac user's profile."""
    try:
        PROFILE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    except OSError:
        # Preferences are optional; an unavailable profile directory must not
        # interrupt use of the inventory.
        pass


def visible_columns(saved_columns, all_columns):
    """Return a safe, ordered selection of columns from saved preferences."""
    # Settings loaded from JSON use lists, while a layout that has just been
    # accepted in the print dialog uses a tuple.  Accept both so the PDF made
    # immediately after pressing Print uses exactly the selected columns.
    if not isinstance(saved_columns, (list, tuple)):
        return tuple(all_columns)
    selected = tuple(key for key in saved_columns if key in all_columns)
    return selected or tuple(all_columns)


def column_order(saved_order, all_columns):
    """Return a safe complete column order, retaining any new columns."""
    if not isinstance(saved_order, (list, tuple)):
        return tuple(all_columns)
    ordered = tuple(dict.fromkeys(key for key in saved_order if key in all_columns))
    for key in all_columns:
        if key not in ordered:
            if key in {"density", "structure"} and "molecule" in ordered:
                position = ordered.index("molecule") + 1
                if key == "structure":
                    position -= 1
                ordered = ordered[:position] + (key,) + ordered[position:]
            else:
                ordered += (key,)
    return ordered


def print_preset(saved_preset, all_columns):
    """Return a safe, independent set of saved print-layout preferences."""
    preset = as_dict(saved_preset)
    default_columns = ("location", "label", "molecule", "sample", "amount") + PRINT_CHECKLIST_KEYS
    order = column_order(preset.get("column_order"), all_columns)
    visible = visible_columns(preset.get("visible_columns"), order)
    if not isinstance(saved_preset, dict):
        visible = tuple(key for key in default_columns if key in all_columns)
    elif not any(key in preset.get("column_order", ()) for key in PRINT_CHECKLIST_KEYS):
        # Upgrade the first layout saved before the checklist columns existed,
        # while preserving later intentional choices to hide either column.
        visible += tuple(key for key in PRINT_CHECKLIST_KEYS if key in all_columns and key not in visible)
    split_column = preset.get("split_column")
    if not isinstance(saved_preset, dict) and "location" in all_columns:
        split_column = "location"
    if split_column not in all_columns:
        split_column = None
    try:
        split_threshold = max(1, int(preset.get("split_threshold", 10)))
    except (TypeError, ValueError):
        split_threshold = 10
    return {"column_order": order, "visible_columns": visible,
            "split_column": split_column, "split_threshold": split_threshold}


def windows_saved_credentials():
    """Read credentials from the current Windows user's Credential Manager."""
    # Keep the import local: ctypes exposes ``windll`` only on Windows.
    import ctypes
    from ctypes import wintypes
    credential_api = ctypes.WinDLL("Advapi32", use_last_error=True)

    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                   ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                   ("LastWritten", ctypes.c_byte * 8), ("CredentialBlobSize", wintypes.DWORD),
                   ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                   ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                   ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                   ("UserName", wintypes.LPWSTR)]

    credential_pointer = ctypes.POINTER(Credential)()
    credential_type_generic = 1
    if not credential_api.CredReadW(WINDOWS_CREDENTIAL_TARGET, credential_type_generic, 0,
                                    ctypes.byref(credential_pointer)):
        return "", ""
    try:
        credential = credential_pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return credential.UserName or "", blob.decode("utf-16-le")
    except (UnicodeDecodeError, ValueError):
        return "", ""
    finally:
        credential_api.CredFree(credential_pointer)


def windows_save_credentials(username, password):
    """Store credentials in the current Windows user's Credential Manager."""
    import ctypes
    from ctypes import wintypes
    credential_api = ctypes.WinDLL("Advapi32", use_last_error=True)

    class Credential(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                   ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                   ("LastWritten", ctypes.c_byte * 8), ("CredentialBlobSize", wintypes.DWORD),
                   ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                   ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                   ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                   ("UserName", wintypes.LPWSTR)]

    encoded_password = password.encode("utf-16-le")
    password_blob = ctypes.create_string_buffer(encoded_password)
    credential = Credential(Type=1, TargetName=WINDOWS_CREDENTIAL_TARGET,
                            CredentialBlobSize=len(encoded_password),
                            CredentialBlob=ctypes.cast(password_blob, ctypes.POINTER(ctypes.c_byte)),
                            Persist=2, UserName=username)
    if not credential_api.CredWriteW(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise RuntimeError(f"The credentials could not be saved in Windows Credential Manager (error {error}).")


def windows_clear_credentials():
    """Remove the app credential from the current Windows user's Credential Manager."""
    import ctypes
    credential_api = ctypes.WinDLL("Advapi32", use_last_error=True)
    if not credential_api.CredDeleteW(WINDOWS_CREDENTIAL_TARGET, 1, 0):
        # ERROR_NOT_FOUND means there was simply no credential left to remove.
        error = ctypes.get_last_error()
        if error != 1168:
            raise RuntimeError(f"The credentials could not be removed from Windows Credential Manager (error {error}).")


def saved_credentials():
    """Read the app's saved credentials from the platform's secure credential store."""
    if os.name == "nt":
        try:
            return windows_saved_credentials()
        # A missing or unavailable credential must never prevent the app from opening.
        except Exception:
            return "", ""
    if os.name != "posix" or not Path("/usr/bin/security").is_file():
        return "", ""
    try:
        result = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-g"], capture_output=True, text=True, timeout=3)
        account = re.search(r'"acct"<blob>="([^"]*)"', result.stdout)
        password = re.search(r'password: "(.*)"', result.stderr)
        if result.returncode == 0 and account and password: return account.group(1), password.group(1)
    # A missing, locked, or unavailable Keychain must never stop the app from
    # opening; the user can still enter credentials manually.
    except Exception: pass
    return "", ""


def save_credentials(username, password):
    """Store one current set of credentials in the platform's secure credential store."""
    if os.name == "nt":
        windows_save_credentials(username, password)
        return
    if os.name != "posix" or not Path("/usr/bin/security").is_file():
        return
    previous_username, _previous_password = saved_credentials()
    if previous_username and previous_username != username:
        # Keychain identifies generic passwords by both service and account.
        # Remove the former account so later launches cannot retrieve stale
        # credentials when the username has changed.
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", previous_username], capture_output=True, text=True, timeout=5)
    result = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", username, "-w", password], capture_output=True, text=True, timeout=5)
    if result.returncode != 0: raise RuntimeError(result.stderr.strip() or "The credentials could not be saved in Keychain.")


def clear_credentials():
    """Remove the app's saved credentials from the platform's secure store."""
    if os.name == "nt":
        windows_clear_credentials()
        return
    if os.name != "posix" or not Path("/usr/bin/security").is_file():
        return
    result = subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE],
                            capture_output=True, text=True, timeout=5)
    # Keychain's "not found" failure is harmless; any other failure is useful to report.
    if result.returncode != 0 and "could not be found" not in result.stderr.casefold():
        raise RuntimeError(result.stderr.strip() or "The credentials could not be removed from Keychain.")

