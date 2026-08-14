"""Bringing config.toml up to the packaged template (#67).

This rewrites a file the user hand-edited, so the tests are mostly about what
must survive that: every value they set, keys this build has never heard of, and
the file itself when anything goes wrong.
"""

from __future__ import annotations

import pytest

from lazyslurm import config as persistent_config
from lazyslurm.__main__ import deprecated_config_keys, unknown_config_keys


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point the module at a throwaway config directory."""
    monkeypatch.setattr(persistent_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(persistent_config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(persistent_config, "BACKUP_FILE", tmp_path / "config.toml.bak")
    return tmp_path


def _write(config_dir, text: str):
    (config_dir / "config.toml").write_text(text)


def _read(config_dir) -> str:
    return (config_dir / "config.toml").read_text()


# --- when it runs ----------------------------------------------------------


def test_a_current_file_is_left_alone(config_dir):
    version = persistent_config.template_version()
    _write(config_dir, f"config_version = {version}\nrefresh = 9.0\n")
    before = _read(config_dir)

    assert persistent_config.migrate() == []
    assert _read(config_dir) == before
    assert not (config_dir / "config.toml.bak").exists()


def test_no_config_file_is_nothing_to_migrate(config_dir):
    assert persistent_config.migrate() == []


def test_a_file_from_a_newer_lazyslurm_is_not_downgraded(config_dir):
    """A shared config directory must not lose settings this build lacks."""
    future = persistent_config.template_version() + 5
    _write(config_dir, f"config_version = {future}\nrefresh = 9.0\nfrom_the_future = 1\n")
    before = _read(config_dir)

    notes = persistent_config.migrate()

    assert _read(config_dir) == before
    assert any("newer LazySlurm" in n for n in notes)


def test_an_unparsable_file_is_reported_not_rewritten(config_dir):
    _write(config_dir, "refresh = = = broken\n")
    before = _read(config_dir)

    notes = persistent_config.migrate()

    assert _read(config_dir) == before
    assert any("could not be read" in n for n in notes)


def test_migrating_twice_changes_nothing_the_second_time(config_dir):
    _write(config_dir, "refresh = 15.0\n")
    assert persistent_config.migrate() != []
    after_first = _read(config_dir)
    assert persistent_config.migrate() == []
    assert _read(config_dir) == after_first


# --- what survives it ------------------------------------------------------


def test_every_value_the_user_set_survives(config_dir):
    _write(config_dir, """
refresh = 15.0
days = 3
editor = "nano"
abbreviate_states = true
partition_order = ["gpu", "cpu"]

[partition_colors]
gpu = "green"
""")
    persistent_config.migrate()
    saved = persistent_config.load()

    assert saved["refresh"] == 15.0
    assert saved["days"] == 3
    assert saved["editor"] == "nano"
    assert saved["abbreviate_states"] is True
    assert saved["partition_order"] == ["gpu", "cpu"]
    assert saved["partition_colors"] == {"gpu": "green"}


def test_the_defaults_are_not_frozen_into_the_file(config_dir):
    """Writing the effective config back would pin every default forever."""
    _write(config_dir, "refresh = 15.0\n")
    persistent_config.migrate()
    saved = persistent_config.load()

    assert set(saved) == {"refresh", "config_version"}
    # The rest are still commented defaults, free to change in a later release.
    assert "# days" in _read(config_dir)


def test_a_setting_lands_where_the_template_documents_it(config_dir):
    """Appending it would leave the file saying two things about one setting."""
    _write(config_dir, "refresh = 15.0\n")
    persistent_config.migrate()
    text = _read(config_dir)

    assert "refresh = 15.0" in text
    assert "# refresh = 5.0" not in text          # the default is replaced, not kept
    assert "auto-refresh interval" in text        # ...and its explanation stays


def test_an_unrecognised_key_is_kept_not_dropped(config_dir):
    """It may be a typo, or a setting from a build we are older than."""
    _write(config_dir, "refresh = 15.0\nmystery_setting = true\n")
    persistent_config.migrate()

    assert persistent_config.load()["mystery_setting"] is True


def test_the_new_template_arrives(config_dir):
    _write(config_dir, "refresh = 15.0\n")
    persistent_config.migrate()
    text = _read(config_dir)

    # Options documented since the file was written are now in it.
    assert "resource_monitor" in text
    assert f"config_version = {persistent_config.template_version()}" in text


def test_the_replaced_file_is_kept(config_dir):
    original = "# my own note\nrefresh = 15.0\n"
    _write(config_dir, original)
    notes = persistent_config.migrate()

    assert (config_dir / "config.toml.bak").read_text() == original
    assert any("backup" in n for n in notes)


def test_the_migration_says_what_it_did(config_dir):
    _write(config_dir, "refresh = 15.0\ndays = 3\n")
    notes = persistent_config.migrate()

    assert len(notes) == 1
    assert "2 settings kept" in notes[0]


# --- deprecated keys -------------------------------------------------------


def test_a_renamed_key_moves_its_value_across(config_dir, monkeypatch):
    monkeypatch.setattr(persistent_config, "DEPRECATED", {"old_days": "days"})
    _write(config_dir, "old_days = 21\n")

    notes = persistent_config.migrate()

    assert persistent_config.load()["days"] == 21
    assert "old_days" not in persistent_config.load()
    assert any("renamed to days" in n for n in notes)


def test_a_removed_key_is_dropped_with_a_note(config_dir, monkeypatch):
    monkeypatch.setattr(persistent_config, "DEPRECATED", {"gone": ""})
    _write(config_dir, "gone = true\nrefresh = 15.0\n")

    notes = persistent_config.migrate()

    assert "gone" not in persistent_config.load()
    assert any("no longer used" in n for n in notes)


def test_a_rename_does_not_overwrite_a_value_already_there(config_dir, monkeypatch):
    monkeypatch.setattr(persistent_config, "DEPRECATED", {"old_days": "days"})
    _write(config_dir, "old_days = 21\ndays = 3\n")

    persistent_config.migrate()

    assert persistent_config.load()["days"] == 3


def test_a_deprecated_key_is_not_reported_as_a_typo(monkeypatch):
    monkeypatch.setattr(persistent_config, "DEPRECATED", {"old_days": "days"})
    saved = {"old_days": 21, "genuine_typo": 1}

    assert deprecated_config_keys(saved) == ["old_days has been renamed to days"]
    assert unknown_config_keys(saved) == ["ignoring unknown setting: genuine_typo"]


def test_a_removed_key_says_it_is_ignored(monkeypatch):
    monkeypatch.setattr(persistent_config, "DEPRECATED", {"gone": ""})
    assert deprecated_config_keys({"gone": 1}) == ["gone is no longer used and is ignored"]


# --- the version key itself ------------------------------------------------


def test_the_packaged_template_carries_a_version():
    assert persistent_config.template_version() >= 1


def test_the_version_key_is_not_an_unknown_setting():
    """It lives in the file, so the typo check has to know about it."""
    assert unknown_config_keys({"config_version": 1}) == []
