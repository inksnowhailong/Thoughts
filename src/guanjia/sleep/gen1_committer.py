"""P4-6c gen1 专用受控提交器。默认 dry-run，绝不自动运行。"""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from guanjia.sleep.gen1_import import CandidateInputError, candidate_fingerprint, validate_candidate_content, validate_candidate_target
from guanjia.sleep.layers import over_limit_report
from guanjia.sleep.staging import ROOT
from guanjia.wsfiles import WorkspaceFiles
from guanjia.wslock import ws_lock

_ROOTS = (ROOT / "data", ROOT / "tmp")
_CANDIDATE_KEYS = {"id","content","target_layer","target","source","privacy","visibility","content_sha256","topic","importance","status","sensitive_flags"}
_DECISION_KEYS = {"accept": {"action","at","candidate_sha256"}, "edit": {"action","at","candidate_sha256","content"},
                  "move": {"action","at","candidate_sha256","target_layer","target"}, "reject": {"action","at","candidate_sha256"}}

class Gen1CommitRefused(RuntimeError): pass
@dataclass(frozen=True)
class Gen1CommitResult:
    staging: Path; files: tuple[str,...]; dry_run: bool

def _inside(path: Path, roots: Iterable[Path]) -> bool:
    return any(_relative(path.resolve(), root.resolve()) for root in roots)
def _relative(path: Path, root: Path) -> bool:
    try: path.relative_to(root); return True
    except ValueError: return False
def _sha(raw: bytes) -> str: return hashlib.sha256(raw).hexdigest()
def _read(path: Path) -> tuple[object, str]:
    try:
        raw=path.read_bytes(); return json.loads(raw.decode("utf-8")), _sha(raw)
    except (OSError,ValueError,UnicodeDecodeError) as e: raise Gen1CommitRefused(f"暂存文件无法读取：{e}") from e

def _load(stage: Path, roots=_ROOTS) -> tuple[dict,list[dict],dict[str,dict],str]:
    stage=stage.resolve()
    if not _inside(stage, roots) or not stage.is_dir(): raise Gen1CommitRefused("staging 不在可信 data/tmp 根下")
    manifest, mh=_read(stage/"manifest.json"); candidates,ch=_read(stage/"candidates.json"); decisions,dh=_read(stage/"decisions.json")
    if not isinstance(manifest,dict) or manifest.get("kind")!="p4-gen1-import-pending": raise Gen1CommitRefused("不是 gen1 staging")
    if not isinstance(candidates,list) or any(not isinstance(x,dict) or set(x)!=_CANDIDATE_KEYS for x in candidates): raise Gen1CommitRefused("候选字段不合法")
    if not isinstance(decisions,dict) or set(decisions)!={"kind","updated_at","decisions"} or decisions["kind"]!="p4-gen1-import-decisions" or not isinstance(decisions["decisions"],dict): raise Gen1CommitRefused("决定文件不合法")
    byid={x.get("id"):x for x in candidates}
    if len(byid)!=len(candidates) or any(not isinstance(k,str) for k in byid): raise Gen1CommitRefused("候选 id 不合法")
    return manifest,candidates,decisions["decisions"], _sha((mh+ch+dh).encode())

def _target(layer:str,target:str) -> tuple[str,str|None]:
    validate_candidate_target(layer,target)
    if target=="core.user.owner": return "USER.md","主人"
    if target=="core.user.general": return "USER.md","通用"
    if target=="core.facts": return "MEMORY.md","关键事实"
    if target=="people/owner": return "memory/people/主人.md",None
    if target=="people/wife": return "memory/people/老婆.md",None
    return f"memory/topics/{target.split('/',1)[1]}.md",None

def _append(text:str, section:str|None, line:str, rel:str) -> str:
    if section is None:
        if not text: return f"# {Path(rel).stem}\n\n<!-- layer: L3 | source: gen1 | visibility: family-default-on -->\n\n## 初代画像\n{line}\n"
        return text.rstrip()+"\n"+line+"\n"
    marker=f"## {section}"
    start=text.find(marker)
    if start<0: raise Gen1CommitRefused(f"{rel} 缺少分节 {marker}")
    end=text.find("\n## ", start+len(marker))
    if end<0: end=len(text)
    return text[:end].rstrip()+"\n"+line+"\n"+text[end:].lstrip("\n")

def _plan(stage:Path, fs:WorkspaceFiles, roots=_ROOTS) -> tuple[dict[str,str],str]:
    _, candidates, decisions, fingerprint=_load(stage,roots)
    plan:dict[str,str]={}
    for cand in candidates:
        if cand.get("status")!="pending" or not isinstance(cand.get("source"),str) or not cand["source"].startswith("gen1:"): raise Gen1CommitRefused("候选来源或状态不合法")
        if hashlib.sha256(str(cand.get("content","")).encode()).hexdigest()!=cand.get("content_sha256"): raise Gen1CommitRefused("候选内容哈希变化")
        decision=decisions.get(cand["id"])
        if not decision or not isinstance(decision,dict): continue
        action=decision.get("action")
        if action not in _DECISION_KEYS or set(decision)!=_DECISION_KEYS[action] or decision.get("candidate_sha256")!=candidate_fingerprint(cand): raise Gen1CommitRefused("决定来源已变化或字段不合法")
        if action=="reject": continue
        try:
            content = validate_candidate_content(decision["content"] if action=="edit" else cand["content"])
            layer,target=(decision["target_layer"],decision["target"]) if action=="move" else (cand["target_layer"],cand["target"])
            rel, section=_target(layer,target)
        except CandidateInputError as e:
            raise Gen1CommitRefused(str(e)) from e
        base=plan.get(rel)
        if base is None: base=fs.read(rel)["text"]
        line=f"- {content} <!-- src: {cand['source']} | candidate: {cand['id']} | sha256: {cand['content_sha256']} -->"
        plan[rel]=_append(base,section,line,rel)
    all_files={rel:fs.read(rel)["text"] for rel in ("USER.md","MEMORY.md")}
    all_files.update(plan)
    over=over_limit_report(all_files)
    if any(len(text)>6000 for rel,text in plan.items() if rel.startswith("memory/")) or over: raise Gen1CommitRefused("预算超限："+"；".join(over or ["L3 文件超过 6000 字"]))
    return {k:v for k,v in plan.items() if fs.read(k)["text"]!=v},fingerprint

def commit_gen1(stage:Path, fs:WorkspaceFiles, *, commit=False, roots=_ROOTS, before_lock=None) -> Gen1CommitResult:
    plan,fingerprint=_plan(stage,fs,roots); result=Gen1CommitResult(stage.resolve(),tuple(sorted(plan)),not commit)
    if not commit:return result
    if before_lock: before_lock()
    with ws_lock(timeout=10,holder="p4-6c",path=fs.lock_path):
        locked_plan,locked_fingerprint=_plan(stage,fs,roots)
        if locked_fingerprint!=fingerprint: raise Gen1CommitRefused("staging 在预检后发生变化")
        if locked_plan!=plan: raise Gen1CommitRefused("workspace 在预检后发生变化")
        for rel,text in sorted(plan.items()):
            current=fs.read(rel)
            fs.write_unlocked(rel,text,current["sha"],actor="p4-6c-gen1")
    return Gen1CommitResult(stage.resolve(),tuple(sorted(plan)),False)
