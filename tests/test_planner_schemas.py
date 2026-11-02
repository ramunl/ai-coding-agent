"""Validate bundled output contracts before a provider can reject a request."""

import json
from pathlib import Path

import pytest

SCHEMAS = Path(__file__).resolve().parents[1] / "ai_agent" / "schemas"


def strict_objects(node, path="$"):
    if isinstance(node, dict):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
            assert set(node.get("required", [])) == set(node["properties"]), path
        for key, value in node.items():
            strict_objects(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            strict_objects(value, f"{path}[{index}]")


@pytest.mark.parametrize("schema", sorted(SCHEMAS.glob("*.json")), ids=lambda p: p.name)
def test_provider_schemas_require_every_property_recursively(schema):
    # Codex structured output rejects optional properties at any object depth,
    # even though a general JSON Schema validator accepts the schema itself.
    strict_objects(json.loads(schema.read_text()))
