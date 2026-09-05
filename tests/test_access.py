from endstone_ninjos_schematics.access import player_has_schematic_access


class Player:
    def __init__(self, *, is_op=False, tags=()):
        self.is_op = is_op
        self.scoreboard_tags = list(tags)


def test_operator_is_allowed_without_tag():
    assert player_has_schematic_access(Player(is_op=True), "architect")


def test_architect_tag_is_allowed_case_insensitively():
    assert player_has_schematic_access(Player(tags=["Architect"]), "architect")


def test_regular_player_is_denied():
    assert not player_has_schematic_access(Player(tags=["builder"]), "architect")
