"""Adapter for tokamap-mappings 2.0.0 -> 2.1.0 (attached to 0004-2.0.0-to-2.1.0.yaml).

Contract: pure function of the document; must be deterministic (the tool runs it twice).
Tested in tests/test_e2e.py::test_slice_adapter_*.
"""
import re

LITERAL = re.compile(r"^\[\s*(-?\d*)\s*(?::\s*(-?\d*)\s*)?(?::\s*(-?\d*)\s*)?\]$")


def migrate(doc, ctx):
    for name, m in doc["mappings"].items():
        if not isinstance(m, dict) or m.get("map_type") != "DATA_SOURCE" or "slice" not in m:
            continue
        s = m["slice"]
        if "{{" in s:
            continue  # templated: cannot be resolved statically, keep as string
        mt = LITERAL.match(s)
        if not mt:
            ctx.fail(("mappings", name, "slice"), f"unrecognised slice syntax {s!r}")
        start, stop, step = mt.groups()
        if stop is None and step is None:  # single index "[3]" -> [3:4]
            if start == "":
                ctx.fail(("mappings", name, "slice"), f"empty slice {s!r}")
            obj = {"start": int(start), "stop": int(start) + 1}
        else:
            obj = {k: int(v) for k, v in (("start", start), ("stop", stop), ("step", step)) if v}
        m["slice"] = obj
        ctx.record(("mappings", name, "slice"), f"{s!r} -> {obj}")
    return doc
