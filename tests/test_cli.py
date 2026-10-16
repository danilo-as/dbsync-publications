from dbsync_publications.cli import ALL_TABLES, resolve_tables

NAMES = ["prices", "system_pricing", "volumes"]


def test_all_tables_choice_selects_every_table():
    assert resolve_tables([ALL_TABLES], NAMES) == NAMES
    assert resolve_tables([ALL_TABLES, "prices"], NAMES) == NAMES


def test_individual_selection_is_kept_as_is():
    assert resolve_tables(["volumes", "prices"], NAMES) == ["volumes", "prices"]
