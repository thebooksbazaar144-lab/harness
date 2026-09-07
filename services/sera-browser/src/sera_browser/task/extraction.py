"""Structured extraction — extract(prompt, schema) → validated JSON.

Inspired by browser-use's `StructuredOutputAction[T]`, Skyvern's
`extract(prompt, schema)`, and Stagehand's `extract(prompt, zod)`.

The `extract` action lets the LLM request structured data extraction
from the current page. The planner:
1. Sends the page content (or a SoM-annotated screenshot) to the LLM
   with the extraction prompt + JSON schema.
2. The LLM returns JSON matching the schema.
3. We validate the JSON against the schema and return it.

This is NOT a browser action — it's an LLM query over the current page
state. The executor records the request; the planner fulfills it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..logging import get_logger

log = get_logger("sera.task.extraction")


@dataclass
class ExtractionResult:
    """The result of a structured extraction call."""
    ok: bool
    data: Any = None
    error: str = ""
    schema: dict[str, Any] | None = None


def validate_against_schema(data: Any, schema: dict[str, Any] | None) -> tuple[bool, str]:
    """Lightweight JSON-schema-style validation.

    We don't depend on `jsonschema` — we implement a minimal validator
    that checks `type`, `properties`, and `required`. This covers the
    common cases for structured extraction without adding a dependency.

    For full JSON-schema validation, install `jsonschema` and call it
    separately.
    """
    if schema is None:
        return True, "no schema"
    if not isinstance(schema, dict):
        return False, "schema must be a dict"
    return _validate_value(data, schema, path="")


def _validate_value(value: Any, schema: dict[str, Any], path: str) -> tuple[bool, str]:
    """Recursively validate a value against a JSON-schema fragment."""
    if not isinstance(schema, dict):
        return True, "non-dict schema skipped"
    expected_type = schema.get("type")
    if expected_type:
        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
            "null": type(None),
        }
        py_type = type_map.get(expected_type)
        if py_type is None:
            return False, f"{path}: unknown type {expected_type!r}"
        # Special case: bool is a subclass of int, but we want strict typing.
        if expected_type == "integer" and isinstance(value, bool):
            return False, f"{path}: expected integer, got boolean"
        if expected_type == "number" and isinstance(value, bool):
            return False, f"{path}: expected number, got boolean"
        if not isinstance(value, py_type):
            return False, f"{path}: expected {expected_type}, got {type(value).__name__}"
    # Object: check required + properties.
    if expected_type == "object" or isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(value, dict):
            for req in required:
                if req not in value:
                    return False, f"{path}: missing required field {req!r}"
            properties = schema.get("properties", {})
            for key, sub_schema in properties.items():
                if key in value:
                    ok, msg = _validate_value(value[key], sub_schema, path=f"{path}.{key}")
                    if not ok:
                        return False, msg
    # Array: check items.
    if expected_type == "array" or isinstance(value, list):
        items_schema = schema.get("items")
        if items_schema and isinstance(value, list):
            for i, item in enumerate(value):
                ok, msg = _validate_value(item, items_schema, path=f"{path}[{i}]")
                if not ok:
                    return False, msg
    # Enum check.
    enum = schema.get("enum")
    if enum and value not in enum:
        return False, f"{path}: value {value!r} not in enum {enum}"
    return True, "ok"


def build_extraction_prompt(
    page_content: str,
    user_prompt: str,
    schema: dict[str, Any] | None,
) -> str:
    """Build the prompt sent to the LLM for extraction.

    The LLM is instructed to return ONLY valid JSON matching the schema.
    """
    schema_str = json.dumps(schema, indent=2) if schema else "(no schema — return any valid JSON)"
    # Truncate page content to avoid blowing the context.
    max_content = 8000
    if len(page_content) > max_content:
        page_content = page_content[:max_content] + "\n... (truncated)"
    return (
        "Extract structured data from the page content below.\n\n"
        f"EXTRACTION PROMPT:\n{user_prompt}\n\n"
        f"JSON SCHEMA:\n{schema_str}\n\n"
        f"PAGE CONTENT:\n{page_content}\n\n"
        "Respond with a single JSON object matching the schema. "
        "Do not include markdown fences or any other text."
    )


def parse_extraction_response(raw: str, schema: dict[str, Any] | None) -> ExtractionResult:
    """Parse + validate the LLM's extraction response."""
    if not raw or not raw.strip():
        return ExtractionResult(ok=False, error="empty response", schema=schema)
    text = raw.strip()
    # Strip markdown fences.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Parse JSON.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: find the first { and last }.
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError as e:
                return ExtractionResult(ok=False, error=f"invalid JSON: {e}", schema=schema)
        else:
            return ExtractionResult(ok=False, error="no JSON found in response", schema=schema)
    # Validate against schema.
    ok, msg = validate_against_schema(data, schema)
    if not ok:
        return ExtractionResult(ok=False, error=f"schema validation failed: {msg}", schema=schema, data=data)
    return ExtractionResult(ok=True, data=data, schema=schema)


__all__ = [
    "ExtractionResult",
    "validate_against_schema",
    "build_extraction_prompt",
    "parse_extraction_response",
]
