from pathlib import Path


def test_no_direct_dimension_id_access_remains():
    source_root = Path(__file__).parents[1] / "src" / "endstone_ninjos_schematics"
    combined = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.py"))
    assert ".dimension.id" not in combined
    assert "block.dimension.id" not in combined


def test_build_marker_is_present():
    plugin_source = (
        Path(__file__).parents[1]
        / "src"
        / "endstone_ninjos_schematics"
        / "plugin.py"
    ).read_text(encoding="utf-8")
    assert 'PLUGIN_VERSION = "1.7.0"' in plugin_source
    assert 'BUILD_ID = "blockdata-nscm-v2-20260904"' in plugin_source
