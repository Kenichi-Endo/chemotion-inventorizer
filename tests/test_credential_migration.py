"""Exercise credential migration without accessing real secure storage."""

from types import SimpleNamespace

import pytest

from chemotion_sample_manager import settings


@pytest.fixture(params=["posix", "nt"])
def store(request, monkeypatch):
    entries = {}
    events = []
    failures = set()
    current = settings.KEYCHAIN_SERVICE
    monkeypatch.setattr(settings, "os", SimpleNamespace(name=request.param))

    def read(service=current):
        events.append(("read", service))
        if "read" in failures:
            raise RuntimeError("Store unavailable")
        return entries.get(service, ("", ""))

    def save(username, password):
        events.append(("save", current))
        if "save" in failures:
            raise RuntimeError("Store unavailable")
        entries[current] = (username, password)

    def clear(service=current):
        events.append(("clear", service))
        if "clear" in failures:
            raise RuntimeError("Store unavailable")
        entries.pop(service, None)

    if request.param == "nt":
        monkeypatch.setattr(settings, "windows_saved_credentials", read)
        monkeypatch.setattr(settings, "windows_save_credentials", save)
        monkeypatch.setattr(settings, "windows_clear_credentials", clear)
    else:
        def run(command, **kwargs):
            service = command[command.index("-s") + 1]
            if command[1] == "find-generic-password":
                username, password = read(service)
                return SimpleNamespace(returncode=0 if username else 44,
                                       stdout=f'"acct"<blob>="{username}"',
                                       stderr=f'password: "{password}"')
            if command[1] == "add-generic-password":
                assert service == current
                save(command[command.index("-a") + 1], command[command.index("-w") + 1])
            else:
                assert command[1] == "delete-generic-password"
                clear(service)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(settings.subprocess, "run", run)
    return SimpleNamespace(entries=entries, events=events, failures=failures)


def test_migration_saves_new_entry_before_removing_old_entry(store):
    credentials = ("chemist", "test password")
    store.entries[settings.LEGACY_CREDENTIAL_SERVICE] = credentials

    assert settings.saved_credentials() == credentials
    assert store.entries == {settings.KEYCHAIN_SERVICE: credentials}
    assert store.events.index(("save", settings.KEYCHAIN_SERVICE)) < store.events.index(
        ("clear", settings.LEGACY_CREDENTIAL_SERVICE))


def test_new_entry_takes_precedence(store):
    store.entries.update({settings.KEYCHAIN_SERVICE: ("new", "new password"),
                          settings.LEGACY_CREDENTIAL_SERVICE: ("old", "old password")})

    assert settings.saved_credentials() == ("new", "new password")
    assert store.events == [("read", settings.KEYCHAIN_SERVICE)]


def test_failed_migration_keeps_legacy_credentials_usable(store):
    credentials = ("chemist", "test password")
    store.entries[settings.LEGACY_CREDENTIAL_SERVICE] = credentials
    store.failures.add("save")

    assert settings.saved_credentials() == credentials
    assert store.entries == {settings.LEGACY_CREDENTIAL_SERVICE: credentials}
    assert not any(event[0] == "clear" for event in store.events)


def test_failed_cleanup_preserves_both_entries_and_allows_startup(store):
    credentials = ("chemist", "test password")
    store.entries[settings.LEGACY_CREDENTIAL_SERVICE] = credentials
    store.failures.add("clear")

    assert settings.saved_credentials() == credentials
    assert store.entries == {settings.KEYCHAIN_SERVICE: credentials,
                             settings.LEGACY_CREDENTIAL_SERVICE: credentials}


def test_explicit_save_uses_new_identity_and_removes_legacy_entry(store):
    store.entries[settings.LEGACY_CREDENTIAL_SERVICE] = ("old", "old password")

    settings.save_credentials("new", "new password")

    assert store.entries == {settings.KEYCHAIN_SERVICE: ("new", "new password")}


def test_clear_removes_both_identities_to_prevent_legacy_fallback(store):
    store.entries.update({settings.KEYCHAIN_SERVICE: ("new", "new password"),
                          settings.LEGACY_CREDENTIAL_SERVICE: ("old", "old password")})

    settings.clear_credentials()

    assert store.entries == {}
    assert settings.saved_credentials() == ("", "")


def test_clear_attempts_both_identities_even_after_a_failure(store):
    store.failures.add("clear")

    with pytest.raises(RuntimeError):
        settings.clear_credentials()

    assert store.events == [("clear", settings.KEYCHAIN_SERVICE),
                            ("clear", settings.LEGACY_CREDENTIAL_SERVICE)]


def test_unavailable_store_does_not_prevent_startup(store):
    store.failures.add("read")

    assert settings.saved_credentials() == ("", "")
    assert not any(event[0] in {"save", "clear"} for event in store.events)
