from endstone_ninjos_schematics.compat import dimension_identifier, resolve_dimension


class LegacyDimension:
    OVERWORLD = 0
    NETHER = 1
    THE_END = 2

    def __init__(self, name, dimension_type):
        self.name = name
        self.type = dimension_type


class ModernDimension:
    def __init__(self, identifier):
        self.id = identifier


class TypeOnlyDimension:
    OVERWORLD = 0
    NETHER = 1
    THE_END = 2

    def __init__(self, dimension_type):
        self.type = dimension_type


class FakeLevel:
    def __init__(self, dimensions, accepted):
        self.dimensions = dimensions
        self.accepted = accepted

    def get_dimension(self, identifier):
        return self.accepted.get(identifier)


def test_dimension_identifier_supports_legacy_name():
    dimension = LegacyDimension("Overworld", LegacyDimension.OVERWORLD)
    assert dimension_identifier(dimension) == "Overworld"


def test_dimension_identifier_supports_modern_id():
    dimension = ModernDimension("minecraft:nether")
    assert dimension_identifier(dimension) == "minecraft:nether"


def test_dimension_identifier_falls_back_to_type():
    dimension = TypeOnlyDimension(TypeOnlyDimension.THE_END)
    assert dimension_identifier(dimension) == "minecraft:the_end"


def test_resolve_dimension_translates_new_id_to_legacy_name():
    overworld = LegacyDimension("Overworld", LegacyDimension.OVERWORLD)
    level = FakeLevel([overworld], {"Overworld": overworld})
    assert resolve_dimension(level, "minecraft:overworld") is overworld


def test_resolve_dimension_translates_legacy_name_to_new_id():
    nether = ModernDimension("minecraft:nether")
    level = FakeLevel([nether], {"minecraft:nether": nether})
    assert resolve_dimension(level, "Nether") is nether
