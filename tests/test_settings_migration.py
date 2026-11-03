"""Preserve existing preferences when upgrading to Sample Manager."""

import json

from chemotion_sample_manager import settings


def test_legacy_preferences_are_read_and_saved_under_new_name(tmp_path, monkeypatch):
    legacy = tmp_path / "Chemotion Inventorizer" / "settings.json"
    current = tmp_path / "Chemotion Sample Manager" / "settings.json"
    legacy.parent.mkdir()
    legacy.write_text('{"font_size": 12}')
    monkeypatch.setattr(settings, "PROFILE_SETTINGS_PATH", current)
    monkeypatch.setattr(settings, "LEGACY_PROFILE_SETTINGS_PATH", legacy)

    preferences = settings.saved_profile_settings()
    assert preferences == {"font_size": 12}
    settings.save_profile_settings(preferences)
    assert json.loads(current.read_text()) == preferences
    assert json.loads(legacy.read_text()) == preferences


def test_new_preferences_take_precedence_over_legacy_settings(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    current = tmp_path / "current.json"
    legacy.write_text('{"font_size": 12}')
    current.write_text('{"font_size": 14}')
    monkeypatch.setattr(settings, "PROFILE_SETTINGS_PATH", current)
    monkeypatch.setattr(settings, "LEGACY_PROFILE_SETTINGS_PATH", legacy)

    assert settings.saved_profile_settings() == {"font_size": 14}
