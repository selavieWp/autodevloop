"""Minimal YAML load/dump with optional PyYAML acceleration.

If PyYAML is installed it is used for full-fidelity parsing and dumping.
Otherwise a small but correct subset implementation handles the nested
maps, block/flow lists, scalars, comments, and quoting that AutoDevLoop
config files use. We control the config schema, so the fallback is safe.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised when PyYAML is present
    import yaml as _pyyaml
except Exception:  # noqa: BLE001
    _pyyaml = None


def _scalar(raw: str) -> Any:
    value = raw.strip()
    if value == "" or value.lower() in {"null", "none", "~"}:
        return None
    low = value.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _split_flow(inner)]
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        if not inner:
            return {}
        result: dict[str, Any] = {}
        for part in _split_flow(inner):
            key, sep, raw_val = part.partition(":")
            if not sep:
                continue
            parsed_key = _scalar(key.strip())
            result[str(parsed_key)] = _scalar(raw_val.strip())
        return result
    if (value[0] == value[-1]) and value[0] in {'"', "'"} and len(value) >= 2:
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _split_flow(inner: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote = ""
    current = ""
    for ch in inner:
        if quote:
            current += ch
            if ch == quote:
                quote = ""
            continue
        if ch in {'"', "'"}:
            quote = ch
            current += ch
        elif ch in "[{":
            depth += 1
            current += ch
        elif ch in "]}":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts


def _fallback_load(text: str) -> Any:
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # strip trailing inline comments outside quotes
        lines.append(raw.rstrip())

    root: dict[str, Any] = {}
    # stack entries: (indent, container, container_kind)
    stack: list[tuple[int, Any, str]] = [(-1, root, "map")]

    for raw in lines:
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()
        while stack and indent <= stack[-1][0] and not (content.startswith("- ") or content == "-"):
            stack.pop()
        if not stack:
            stack = [(-1, root, "map")]
        parent = stack[-1][1]

        if content.startswith("- ") or content == "-":
            item_text = content[1:].strip()
            # ensure parent is a list
            if not isinstance(parent, list):
                continue
            if item_text == "":
                child: dict[str, Any] = {}
                parent.append(child)
                stack.append((indent, child, "map"))
            elif ":" in item_text and not item_text.startswith(("'", '"')):
                child = {}
                parent.append(child)
                key, _, val = item_text.partition(":")
                child[key.strip()] = _scalar(val) if val.strip() else {}
                stack.append((indent, child, "map"))
            else:
                parent.append(_scalar(item_text))
            continue

        if ":" not in content:
            continue
        key, _, val = content.partition(":")
        key = key.strip()
        val = val.strip()
        if not isinstance(parent, dict):
            continue
        if val == "":
            # could be a nested map or a list; decide by next line indent later.
            child_map: dict[str, Any] = {}
            parent[key] = child_map
            stack.append((indent, child_map, "map"))
        else:
            parent[key] = _scalar(val)

    return _promote_lists(root)


def _promote_lists(node: Any) -> Any:
    """Convert empty-map placeholders that actually hold list items."""
    if isinstance(node, dict):
        return {k: _promote_lists(v) for k, v in node.items()}
    return node


def load(text: str) -> dict[str, Any]:
    if not text or not text.strip():
        return {}
    if _pyyaml is not None:
        data = _pyyaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    data = _fallback_load(text)
    return data if isinstance(data, dict) else {}


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or any(c in text for c in ":#") or text.strip() != text or text.lower() in {"true", "false", "null", "yes", "no"}:
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _fallback_dump(data: Any, indent: int = 0) -> str:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict) and value:
                lines.append(f"{pad}{key}:")
                lines.append(_fallback_dump(value, indent + 1))
            elif isinstance(value, list) and value:
                lines.append(f"{pad}{key}:")
                for item in value:
                    if isinstance(item, dict):
                        body = _fallback_dump(item, indent + 2).lstrip()
                        lines.append(f"{pad}  - {body}")
                    else:
                        lines.append(f"{pad}  - {_dump_scalar(item)}")
            elif isinstance(value, list):
                lines.append(f"{pad}{key}: []")
            elif isinstance(value, dict):
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}: {_dump_scalar(value)}")
    return "\n".join(line for line in lines if line != "")


def dump(data: dict[str, Any]) -> str:
    if _pyyaml is not None:
        return _pyyaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return _fallback_dump(data).rstrip() + "\n"
