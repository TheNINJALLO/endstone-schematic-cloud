import json
from pathlib import Path

ROOT = Path(__file__).parents[1] / "addon"


def test_new_tool_items_and_textures_exist():
    for name in ("schem_undo", "schem_redo", "schem_confirm"):
        assert (ROOT / "NinjOS_Schematics_BP" / "items" / f"{name}.json").is_file()
        assert (ROOT / "NinjOS_Schematics_RP" / "textures" / "items" / f"{name}.png").is_file()


def test_outline_particles_use_numeric_components_without_molang_strings():
    for name in ("selection_outline", "placement_outline"):
        path = ROOT / "NinjOS_Schematics_RP" / "particles" / f"{name}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        assert data["particle_effect"]["description"]["identifier"] == f"ninjos:{name}"
        assert "variable." not in text
        assert "query." not in text
