from endstone_ninjos_schematics.config_merge import merge_missing

import pytest


@pytest.mark.parametrize("old_limit, adaptive, expected", [
    (256, True, 1200), (128, True, 128), (1200, True, 1200), (256, False, 256),
])
def test_plugin_migrates_legacy_paste_limit_but_preserves_custom_settings(old_limit, adaptive, expected):
    from types import SimpleNamespace
    from test_chunk_loading import _load_plugin_module

    module = _load_plugin_module()
    plugin = object.__new__(module.NinjOSSchematicsPlugin)
    plugin.config = {
        "performance": {"paste_changed_blocks_per_tick": old_limit, "paste_adaptive_pacing": adaptive},
        "database": {"password": "preserved"},
    }
    saved = []
    plugin.save_config = lambda: saved.append(True)

    def unexpected_warning(message):
        raise AssertionError(message)

    plugin.logger = SimpleNamespace(info=lambda _message: None, warning=unexpected_warning)
    plugin._merge_config_defaults()
    assert plugin.config["performance"]["paste_changed_blocks_per_tick"] == expected
    assert plugin.config["performance"]["paste_adaptive_pacing"] is adaptive
    assert plugin.config["database"]["password"] == "preserved"
    assert saved


def test_merge_adds_new_sections_and_preserves_credentials():
    current = {"database": {"host": "db.example", "password": "secret"}}
    defaults = {
        "database": {"host": "127.0.0.1", "password": "change-me", "hard_delete_enabled": True},
        "disk": {"enabled": True, "directory": "schematics"},
    }
    assert merge_missing(current, defaults)
    assert current["database"]["host"] == "db.example"
    assert current["database"]["password"] == "secret"
    assert current["database"]["hard_delete_enabled"] is True
    assert current["disk"]["directory"] == "schematics"


def test_merge_is_idempotent():
    current = {"disk": {"enabled": False}}
    defaults = {"disk": {"enabled": True}}
    assert not merge_missing(current, defaults)
    assert current["disk"]["enabled"] is False


def test_merge_adds_worldedit_export_settings_without_replacing_database():
    current = {"database": {"host": "mysql.remote", "password": "keep-this"}}
    defaults = {
        "database": {"host": "127.0.0.1", "password": "change-me"},
        "worldedit": {
            "enabled": True,
            "directory": "worldedit_schematics",
            "java_data_version": 4671,
        },
    }
    assert merge_missing(current, defaults)
    assert current["database"] == {"host": "mysql.remote", "password": "keep-this"}
    assert current["worldedit"]["enabled"] is True
    assert current["worldedit"]["directory"] == "worldedit_schematics"
    assert current["worldedit"]["java_data_version"] == 4671
