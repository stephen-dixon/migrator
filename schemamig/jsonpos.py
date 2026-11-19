"""Map JSON pointers to text positions (and back) for editor integration.

A small position-recording JSON scanner: for every value it records the pointer, the
range of its key (for object members) and the range of the value. Lines/columns are
0-based, as in LSP (columns count characters; fine for the ASCII-heavy schemas here).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import pointer as P


@dataclass
class Span:
    ptr: str
    start: tuple[int, int]          # value start (line, col)
    end: tuple[int, int]            # value end
    key: tuple[tuple[int, int], tuple[int, int]] | None = None  # key range for members


class JsonPosError(ValueError):
    pass


def index(text: str) -> dict[str, Span]:
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    def pos(i: int) -> tuple[int, int]:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= i:
                lo = mid
            else:
                hi = mid - 1
        return lo, i - line_starts[lo]

    out: dict[str, Span] = {}
    n = len(text)

    def ws(i: int) -> int:
        while i < n and text[i] in " \t\r\n":
            i += 1
        return i

    def string(i: int) -> tuple[str, int]:
        assert text[i] == '"'
        j, buf = i + 1, []
        while j < n and text[j] != '"':
            if text[j] == "\\":
                j += 1
                esc = text[j]
                if esc == "u":
                    buf.append(chr(int(text[j + 1:j + 5], 16)))
                    j += 4
                else:
                    buf.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(esc, esc))
            else:
                buf.append(text[j])
            j += 1
        if j >= n:
            raise JsonPosError("unterminated string")
        return "".join(buf), j + 1

    def value(i: int, ptr: str, key=None) -> int:
        i = ws(i)
        if i >= n:
            raise JsonPosError("unexpected end of input")
        start, ch = i, text[i]
        if ch == "{":
            i = ws(i + 1)
            while i < n and text[i] != "}":
                ks = i
                k, i = string(i)
                krange = (pos(ks), pos(i))
                i = ws(i)
                if text[i] != ":":
                    raise JsonPosError(f"expected ':' at {pos(i)}")
                i = value(i + 1, P.child(ptr, k), krange)
                i = ws(i)
                if text[i] == ",":
                    i = ws(i + 1)
            i += 1
        elif ch == "[":
            i, k = ws(i + 1), 0
            while i < n and text[i] != "]":
                i = value(i, P.child(ptr, str(k)))
                k += 1
                i = ws(i)
                if text[i] == ",":
                    i = ws(i + 1)
            i += 1
        elif ch == '"':
            _, i = string(i)
        else:
            while i < n and text[i] not in ",]} \t\r\n":
                i += 1
        out[ptr] = Span(ptr, pos(start), pos(i), key)
        return i

    value(0, "#")
    return out


def locate(idx: dict[str, Span], ptr: str) -> Span:
    """Span for ptr, or for its nearest existing ancestor."""
    while ptr not in idx and ptr != "#":
        toks = P.split(ptr)
        ptr = P.join(*toks[:-1]) if len(toks) > 1 else "#"
    return idx[ptr]


def at_position(idx: dict[str, Span], line: int, col: int) -> str:
    """Deepest pointer whose key or value range contains (line, col)."""
    best, depth = "#", -1
    for s in idx.values():
        ranges = [(s.start, s.end)] + ([s.key] if s.key else [])
        if any(a <= (line, col) <= b for a, b in ranges):
            d = len(P.split(s.ptr))
            if d > depth:
                best, depth = s.ptr, d
    return best
