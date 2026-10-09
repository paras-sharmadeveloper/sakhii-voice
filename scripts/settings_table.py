"""Print the README settings table from app/settings.py.

    python scripts/settings_table.py

tests/test_settings_doc.py fails when README.md is out of date with it.
"""

import json
import sys
import typing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic_core import PydanticUndefined  # noqa: E402

from app.settings import Settings  # noqa: E402

SENSITIVE_BOOTSTRAP = {"redis_password", "redis_url", "sakhii_voice_cred_key"}
WHERE = {"bootstrap": ".env only", "runtime": "settings", "secret": "secrets"}


def _type(annotation) -> tuple[str, str]:
    if typing.get_origin(annotation) is typing.Literal:
        values = typing.get_args(annotation)
        kind = type(values[0]).__name__
        return ("string" if kind == "str" else kind), " / ".join(f"`{v}`" for v in values)
    if typing.get_origin(annotation) is dict:
        return "object", ""
    return {"str": "string", "int": "int", "float": "float", "bool": "bool"}[annotation.__name__], ""


def _range(field) -> str:
    lo = hi = None
    for m in field.metadata:
        lo = getattr(m, "ge", lo)
        hi = getattr(m, "le", hi)
    return f"{lo} – {hi}" if lo is not None or hi is not None else ""


def _default(field) -> str:
    value = field.default_factory() if field.default is PydanticUndefined else field.default
    if value == "" or value == {}:
        return "(empty)"
    return f"`{json.dumps(value) if isinstance(value, dict | bool) else value}`"


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def table() -> str:
    rows = ["| Name | Where | Secret | Type | Default | Range / values | Description |",
            "|---|---|---|---|---|---|---|"]
    order = {"bootstrap": 0, "runtime": 1, "secret": 2}
    for name, field in sorted(Settings.model_fields.items(), key=lambda kv: order[Settings.kind(kv[0])]):
        kind = Settings.kind(name)
        type_name, choices = _type(field.annotation)
        rows.append(
            f"| `{name.upper()}` | {WHERE[kind]} | {'yes' if kind == 'secret' or name in SENSITIVE_BOOTSTRAP else 'no'} "
            f"| {type_name} | {_default(field)} | {choices or _range(field)} | {_md(field.description or '')} |"
        )
    return "\n".join(rows)


if __name__ == "__main__":
    print(table())
