"""README's settings table is generated from app/settings.py (Laravel builds its form from it)."""

from pathlib import Path

from app.settings import Settings, names
from scripts.settings_table import table


def test_readme_table_is_up_to_date():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert table() in readme, "README settings table is stale: python scripts/settings_table.py"


def test_every_setting_is_documented_and_classified():
    for name, field in Settings.model_fields.items():
        assert field.description, f"{name} has no description"
    assert names("bootstrap") == {"host", "port", "redis_host", "redis_port", "redis_password", "redis_db",
                                  "redis_url", "redis_key_prefix", "sakhii_voice_cred_key"}
    for name in names("secret"):
        assert Settings.model_fields[name].repr is False, f"{name} would show in repr()"
