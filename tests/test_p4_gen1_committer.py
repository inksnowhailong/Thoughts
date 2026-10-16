"""P4-6c 专用提交器：所有 case 都用临时 workspace/staging。"""
from __future__ import annotations
import hashlib,json,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/"src"))
from guanjia.sleep.gen1_committer import Gen1CommitRefused,commit_gen1
from guanjia.sleep.gen1_import import candidate_fingerprint
from guanjia.wsfiles import WorkspaceFiles
failed=0
def check(ok,name):
 global failed; print(("PASS " if ok else "FAIL ")+name); failed+=not ok
def refuse(fn,name):
 try:fn()
 except Gen1CommitRefused:check(True,name)
 else:check(False,name)
def make(base, *, content="主人偏好简短回答。", action="accept", target="core.user.owner"):
 ws=base/"workspace"; (ws/"memory"/"people").mkdir(parents=True); (ws/"memory"/"topics").mkdir()
 (ws/"USER.md").write_text("# 用户\n## 主人\n## 通用\n",encoding="utf-8")
 (ws/"MEMORY.md").write_text("# 记忆\n## 关键事实\n## 悬而未决\n## 最近要点\n## 记忆目录\n",encoding="utf-8")
 (ws/"SOUL.md").write_text("# SOUL\n",encoding="utf-8")
 stage=base/"data"/"sleep"/"staging"/"gen1-import"/"fixture"; stage.mkdir(parents=True)
 cand={"id":"gen1-a","content":content,"target_layer":"L1","target":"core.user.owner","source":"gen1:fixture/profile.md","privacy":"owner-approved-open","visibility":"family-default-on","content_sha256":hashlib.sha256(content.encode()).hexdigest(),"topic":"沟通","importance":6,"status":"pending","sensitive_flags":[]}
 dec={"action":action,"at":"2026-10-14T17:00:00+0800","candidate_sha256":candidate_fingerprint(cand)}
 if action=="edit":dec["content"]="主人偏好一句说完。"
 if action=="move":dec.update(target_layer="L3",target=target)
 (stage/"manifest.json").write_text(json.dumps({"kind":"p4-gen1-import-pending"}),encoding="utf-8")
 (stage/"candidates.json").write_text(json.dumps([cand],ensure_ascii=False),encoding="utf-8")
 (stage/"decisions.json").write_text(json.dumps({"kind":"p4-gen1-import-decisions","updated_at":"x","decisions":{"gen1-a":dec}},ensure_ascii=False),encoding="utf-8")
 fs=WorkspaceFiles(root=ws,history_dir=base/"history",lock_path=ws/"guanjia"/"lock")
 return ws,stage,fs
tmp=Path(tempfile.mkdtemp(prefix="gj-p46c-")); roots=(tmp/"data",tmp/"tmp")
ws,stage,fs=make(tmp)
before=(ws/"USER.md").read_bytes(); r=commit_gen1(stage,fs,roots=roots)
check(r.dry_run and (ws/"USER.md").read_bytes()==before,"dry-run zero writes")
r=commit_gen1(stage,fs,commit=True,roots=roots)
check(not r.dry_run and "src: gen1:fixture/profile.md" in (ws/"USER.md").read_text(encoding="utf-8") and fs.history("USER.md"),"success writes traced entry with history")
tmp2=Path(tempfile.mkdtemp(prefix="gj-p46c-edit-"));ws,stage,fs=make(tmp2,action="edit");commit_gen1(stage,fs,commit=True,roots=(tmp2/"data",tmp2/"tmp"));check("一句说完" in (ws/"USER.md").read_text(encoding="utf-8"),"edit is committed")
tmp3=Path(tempfile.mkdtemp(prefix="gj-p46c-move-"));ws,stage,fs=make(tmp3,action="move",target="people/owner");commit_gen1(stage,fs,commit=True,roots=(tmp3/"data",tmp3/"tmp"));check((ws/"memory"/"people"/"主人.md").exists(),"move writes allowed person file")
tmp4=Path(tempfile.mkdtemp(prefix="gj-p46c-bad-"));ws,stage,fs=make(tmp4,action="edit");d=json.loads((stage/"decisions.json").read_text(encoding="utf-8"));d["decisions"]["gen1-a"]["content"]="token=secret";(stage/"decisions.json").write_text(json.dumps(d),encoding="utf-8");refuse(lambda:commit_gen1(stage,fs,roots=(tmp4/"data",tmp4/"tmp")),"sensitive edit refused")
tmp5=Path(tempfile.mkdtemp(prefix="gj-p46c-source-"));ws,stage,fs=make(tmp5);c=json.loads((stage/"candidates.json").read_text(encoding="utf-8"));c[0]["source"]="gen1:changed.md";(stage/"candidates.json").write_text(json.dumps(c),encoding="utf-8");refuse(lambda:commit_gen1(stage,fs,roots=(tmp5/"data",tmp5/"tmp")),"source change refused")
tmpb=Path(tempfile.mkdtemp(prefix="gj-p46c-budget-"));ws,stage,fs=make(tmpb,content="x"*800);(ws/"USER.md").write_text("# 用户\n## 主人\n"+"x"*2400+"\n## 通用\n",encoding="utf-8");refuse(lambda:commit_gen1(stage,fs,roots=(tmpb/"data",tmpb/"tmp")),"over-budget plan refused")
tmp6=Path(tempfile.mkdtemp(prefix="gj-p46c-race-"));ws,stage,fs=make(tmp6);before=(ws/"USER.md").read_bytes();refuse(lambda:commit_gen1(stage,fs,commit=True,roots=(tmp6/"data",tmp6/"tmp"),before_lock=lambda:(stage/"decisions.json").write_text((stage/"decisions.json").read_text(encoding="utf-8")+" ",encoding="utf-8")),"lock-time staging race refused");check((ws/"USER.md").read_bytes()==before,"race zero writes")
print("ALL PASS test_p4_gen1_committer" if not failed else f"{failed} FAILED");sys.exit(1 if failed else 0)
