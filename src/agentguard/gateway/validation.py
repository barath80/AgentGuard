"""Tool argument validation against the registered schema (THREAT_MODEL #13).

Deliberately not a full JSON Schema implementation — just the part the
threat model calls for: reject arguments the tool doesn't declare,
rather than silently passing them to an executor. For `write-high` and
`destructive` tools this is strict (any unknown key is rejected); for
lower-risk tools it's informational only, since a looser integration
shouldn't break over an extra harmless field.
"""

STRICT_RISK_CLASSES = ("write-high", "destructive")


def unknown_arg_rejection(risk_class: str, input_schema: dict, args: dict) -> str | None:
    properties = (input_schema or {}).get("properties", {})
    unknown = sorted(set(args) - set(properties))
    if not unknown:
        return None
    if risk_class in STRICT_RISK_CLASSES:
        return f"unexpected argument(s) for a {risk_class} tool: {', '.join(unknown)}"
    return None
