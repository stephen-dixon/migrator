"""JSON Pointer (RFC 6901) helpers plus instance-path formatting.

Schema locations are written as URI-fragment pointers: "#", "#/$defs/x".
Instance locations are tuples of keys / indices, formatted as $["a"][0].
"""
from __future__ import annotations

from typing import Any


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def split(ptr: str) -> list[str]:
    if ptr.startswith("#"):
        ptr = ptr[1:]
    if ptr == "":
        return []
    if not ptr.startswith("/"):
        raise ValueError(f"not a JSON pointer: {ptr!r}")
    return [unescape(t) for t in ptr[1:].split("/")]


def join(*tokens: str) -> str:
    return "#" + "".join("/" + escape(str(t)) for t in tokens)


def child(ptr: str, *tokens: str) -> str:
    base = ptr if ptr.startswith("#") else "#" + ptr
    return base + "".join("/" + escape(str(t)) for t in tokens)


def get(doc: Any, ptr: str) -> Any:
    node = doc
    for tok in split(ptr):
        if isinstance(node, list):
            node = node[int(tok)]
        elif isinstance(node, dict):
            node = node[tok]
        else:
            raise KeyError(ptr)
    return node


def exists(doc: Any, ptr: str) -> bool:
    try:
        get(doc, ptr)
        return True
    except (KeyError, IndexError, ValueError):
        return False


# --- instance paths ---------------------------------------------------------

Path = tuple  # tuple[str | int, ...]


def fmt_path(path: Path) -> str:
    out = "$"
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else f"[{p!r}]".replace("'", '"')
    return out


def get_path(doc: Any, path: Path) -> Any:
    node = doc
    for p in path:
        node = node[p]
    return node


def set_path(doc: Any, path: Path, value: Any) -> Any:
    """Set value at path; returns the (possibly new) root."""
    if not path:
        return value
    parent = get_path(doc, path[:-1])
    parent[path[-1]] = value
    return doc
