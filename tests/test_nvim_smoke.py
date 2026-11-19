"""Headless Neovim against the real language server (skipped without nvim/pygls)."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import EXAMPLE, ROOT

pytest.importorskip("pygls")


@pytest.mark.skipif(not shutil.which("nvim"), reason="nvim not installed")
def test_neovim_records_a_change_via_code_action(tmp_path):
    proj = tmp_path / "tokamap"
    shutil.copytree(EXAMPLE, proj)
    schema = proj / "schemas/tokamap-mappings.schema.json"
    out = tmp_path / "out.json"
    server = Path(sys.executable).parent / "schemamig"
    r = subprocess.run(["nvim", "--headless", "-u", "NONE", "-l", str(ROOT / "editors/nvim/test/smoke.lua"),
                        str(ROOT / "editors/nvim"), str(server), str(schema), str(out)],
                       capture_output=True, text=True, timeout=60, cwd=proj)
    res = json.loads(out.read_text()) if out.exists() else {}
    assert r.returncode == 0, (res, r.stderr[-2000:])
    assert any("unrecorded schema change" in m for m in res["diagnostics"])
    assert any("record as rename-field: rename field 'probe' -> 'probe_key'" in t for t in res["actions"])
    assert "save the working schema first" in res["dirty_refusal"]
    assert res["record"].get("ok"), res["record"] and res["record"]["record"]["ops"][0]["to"] == "probe_key"
    assert not any("unrecorded" in m for m in res["diagnostics_after"])
    journal = (proj / "changes/tokamap-mappings.jsonl").read_text().splitlines()
    assert len(journal) == 1 and json.loads(journal[0])["reason"] == "probe holds a mapping key"
