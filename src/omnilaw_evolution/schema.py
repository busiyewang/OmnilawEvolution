"""Minimal JSON-schema subset validator for model outputs. Model output is
turned into a plan only after it passes these checks."""
from __future__ import annotations

TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
         "integer": int, "number": (int, float)}


class SchemaError(ValueError):
    pass


def validate(obj, schema, path="$"):
    t = schema.get("type")
    if t:
        ok = isinstance(obj, TYPES[t]) and not (t in ("integer", "number") and isinstance(obj, bool))
        if not ok:
            raise SchemaError("%s: expected %s, got %s" % (path, t, type(obj).__name__))
    if "enum" in schema and obj not in schema["enum"]:
        raise SchemaError("%s: %r not in %r" % (path, obj, schema["enum"]))
    if t == "string" and "maxLength" in schema and len(obj) > schema["maxLength"]:
        raise SchemaError("%s: longer than %d" % (path, schema["maxLength"]))
    if t == "number" or t == "integer":
        if "minimum" in schema and obj < schema["minimum"]:
            raise SchemaError("%s: below minimum" % path)
        if "maximum" in schema and obj > schema["maximum"]:
            raise SchemaError("%s: above maximum" % path)
    if t == "object":
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in obj:
                raise SchemaError("%s: missing %s" % (path, k))
        if schema.get("additionalProperties") is False:
            extra = set(obj) - set(props)
            if extra:
                raise SchemaError("%s: unexpected keys %s" % (path, sorted(extra)))
        for k, sub in props.items():
            if k in obj:
                validate(obj[k], sub, "%s.%s" % (path, k))
    if t == "array":
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            raise SchemaError("%s: more than %d items" % (path, schema["maxItems"]))
        for i, v in enumerate(obj):
            validate(v, schema.get("items", {}), "%s[%d]" % (path, i))
    return obj


STR = {"type": "string", "maxLength": 2000}

KNOWLEDGE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "maxItems": 10, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["method", "claim", "steps", "conditions", "limitations", "quote", "applicable"],
        "properties": {
            "method": {"type": "string", "maxLength": 64}, "claim": STR, "steps": STR,
            "conditions": STR, "limitations": STR, "applicable": {"type": "boolean"},
            "quote": {"type": "string", "maxLength": 600}}}}},
}

PROPOSAL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["proposals"],
    "properties": {"proposals": {"type": "array", "maxItems": 4, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["problem", "hypothesis", "methods", "knowledge_ids", "variants"],
        "properties": {
            "problem": STR, "hypothesis": STR,
            "methods": {"type": "array", "maxItems": 3, "items": {
                "type": "string", "enum": ["url_canonicalization", "shingle_jaccard", "title_token_overlap"]}},
            "knowledge_ids": {"type": "array", "maxItems": 10, "items": {"type": "integer"}},
            "variants": {"type": "array", "maxItems": 6, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"threshold": {"type": "number", "minimum": 0.05, "maximum": 0.95},
                               "k": {"type": "integer", "minimum": 2, "maximum": 8}}}}}}}},
}
