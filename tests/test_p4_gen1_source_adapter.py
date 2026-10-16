"""P4-6d 源清单：fixture 不读取真实初代目录。"""
from __future__ import annotations
import json,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from guanjia.sleep.gen1_source import SourceAdapterRefused, discover_sources, stage_manifest
failed=0
def check(ok,name):
 global failed;print(("PASS " if ok else "FAIL ")+name);failed+=not ok
tmp=Path(tempfile.mkdtemp(prefix="gj-p46d-")); source=tmp/"大师";source.mkdir()
(source/"user-portrait.md").write_text("fixture portrait",encoding="utf-8")
(source/"persona.md").write_text("fixture persona",encoding="utf-8")
(source/"activity-log.jsonl").write_text("not read",encoding="utf-8")
(source/"permissions.json").write_text("not read",encoding="utf-8")
(source/".omc").mkdir()
manifest=discover_sources(source,allowed_roots=(source,))
check([item.path for item in manifest.sources]==["persona.md","user-portrait.md"] and all(len(item.sha256)==64 for item in manifest.sources),"allowlisted files receive byte hashes")
check({item["reason"] for item in manifest.excluded}=={"sensitive_filename","not_top_level_regular_file"},"logs permissions and sessions are excluded by filename")
try:discover_sources(tmp/"other",allowed_roots=(source,))
except SourceAdapterRefused:check(True,"non-allowlisted source is refused")
else:check(False,"non-allowlisted source is refused")
stage=stage_manifest(manifest,tmp/"data"/"sleep"/"staging"/"gen1-source",run_id="fixture",allowed_roots=(tmp/"data",tmp/"tmp"))
saved=json.loads((stage/"source-manifest.json").read_text(encoding="utf-8"))
check(saved["kind"]=="p4-gen1-source-manifest" and "fixture portrait" not in (stage/"source-manifest.json").read_text(encoding="utf-8"),"staged manifest contains metadata and no source body")
try:stage_manifest(manifest,tmp/"outside",run_id="bad",allowed_roots=(tmp/"data",))
except SourceAdapterRefused:check(True,"non-trusted manifest staging is refused")
else:check(False,"non-trusted manifest staging is refused")
print("ALL PASS test_p4_gen1_source_adapter" if not failed else f"{failed} FAILED");sys.exit(1 if failed else 0)
