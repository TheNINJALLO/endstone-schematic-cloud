from endstone_ninjos_schematics.config_merge import merge_missing


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
