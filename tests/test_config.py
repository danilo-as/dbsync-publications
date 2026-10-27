import pytest

from dbsync_publications.config import ConfigError, describe, load_settings

SRC = "postgresql://u:secret@src-host:5432/warehouse"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("SOURCE_DATABASE_URL", "TARGET_1_DATABASE_URL", "TARGET_2_DATABASE_URL", "MIGRATIONS_DIR"):
        monkeypatch.delenv(var, raising=False)


def write_env(tmp_path, content):
    path = tmp_path / ".env"
    path.write_text(content)
    return str(path)


def test_loads_source_and_only_defined_targets(tmp_path):
    env = write_env(tmp_path, f"SOURCE_DATABASE_URL={SRC}\nTARGET_1_DATABASE_URL=postgresql://u:p@t1/db\n")
    settings = load_settings(env)
    assert settings.source_url == SRC
    assert settings.target_urls == {"TARGET_1_DATABASE_URL": "postgresql://u:p@t1/db"}


def test_two_targets(tmp_path):
    env = write_env(
        tmp_path,
        f"SOURCE_DATABASE_URL={SRC}\nTARGET_1_DATABASE_URL=postgresql://u:p@t1/db\n"
        "TARGET_2_DATABASE_URL=postgresql://u:p@t2/db\n",
    )
    assert list(load_settings(env).target_urls) == ["TARGET_1_DATABASE_URL", "TARGET_2_DATABASE_URL"]


@pytest.mark.parametrize(
    "content, message",
    [
        ("TARGET_1_DATABASE_URL=postgresql://t1/db\n", "SOURCE_DATABASE_URL is not defined"),
        (f"SOURCE_DATABASE_URL={SRC}\n", "Define at least one target"),
        (f"SOURCE_DATABASE_URL={SRC}\nTARGET_1_DATABASE_URL={SRC}\n", "identical to the source"),
    ],
)
def test_invalid_configuration(tmp_path, content, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(write_env(tmp_path, content))


def test_describe_hides_credentials():
    assert describe(SRC) == "src-host:5432/warehouse"


def test_migrations_dir_is_optional(tmp_path):
    env = write_env(tmp_path, f"SOURCE_DATABASE_URL={SRC}\nTARGET_1_DATABASE_URL=postgresql://u:p@t1/db\n")
    assert load_settings(env).migrations_dir is None


def test_migrations_dir_is_read(tmp_path):
    env = write_env(
        tmp_path,
        f"SOURCE_DATABASE_URL={SRC}\nTARGET_1_DATABASE_URL=postgresql://u:p@t1/db\nMIGRATIONS_DIR={tmp_path}\n",
    )
    assert load_settings(env).migrations_dir == tmp_path
