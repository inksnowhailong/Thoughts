"""P3 微信上游 2.4.8 provenance shim：默认只校验，--apply 才写入。"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil
from pathlib import Path

VERSION = "2.4.8"
SOURCE_SHA256 = "eeff7621ac80353cc03f1af91b9ce9e35d886c3ee2fbeb3fd96e43d2b3394b24"
IMPORT = 'import { writeGuanjiaProvenance } from "./guanjia-provenance-bridge.js";\n'
NEEDLE = "const runId = randomUUID();"

BRIDGE = '''import crypto from "node:crypto"; import fs from "node:fs"; import os from "node:os"; import path from "node:path";
const dir=path.join(os.homedir(),".openclaw","workspace","guanjia","wechat-provenance");
const keyPath=path.join(os.homedir(),".openclaw","workspace","guanjia","wechat-provenance.key");
const canonical=(r)=>JSON.stringify(r);
export function writeGuanjiaProvenance({accountId,senderUserId,sessionKey,runId}) {
  if(!/^[A-Za-z0-9_-]{1,128}$/.test(accountId)||!/^[A-Za-z0-9._@-]{1,256}$/.test(senderUserId)||sessionKey!=="agent:main:main"||!runId) return false;
  fs.mkdirSync(dir,{recursive:true}); let key; try { key=fs.readFileSync(keyPath); } catch { key=crypto.randomBytes(32); fs.writeFileSync(keyPath,key,{mode:0o600}); }
  const createdAt=Date.now(), record={version:1,runId,sessionKey,accountId,senderUserId,createdAt,expiresAt:createdAt+30000,nonce:crypto.randomBytes(24).toString("base64url")};
  const signed={...record,signature:crypto.createHmac("sha256",key).update(canonical(record)).digest("hex")};
  const target=path.join(dir,crypto.createHash("sha256").update(runId).digest("hex")+".json"), tmp=target+".tmp";
  fs.writeFileSync(tmp,JSON.stringify(signed),{mode:0o600}); fs.renameSync(tmp,target); return true;
}'''

def sha(data: bytes) -> str: return hashlib.sha256(data).hexdigest()
def patch_source(source: str) -> str:
    if IMPORT in source or source.count(NEEDLE) != 1: raise ValueError("unexpected upstream source shape")
    return IMPORT + source.replace(NEEDLE, NEEDLE + "\\n    writeGuanjiaProvenance({ accountId: deps.accountId, senderUserId: full.from_user_id ?? '', sessionKey: route.sessionKey, runId });", 1)

def locate() -> Path:
    roots = list((Path.home()/".openclaw"/"npm"/"projects").glob("*/node_modules/@tencent-weixin/openclaw-weixin"))
    if len(roots) != 1: raise RuntimeError("expected exactly one installed WeChat plugin")
    return roots[0]

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--apply",action="store_true"); p.add_argument("--revert",action="store_true"); a=p.parse_args(argv)
    root=locate(); package=json.loads((root/"package.json").read_text()); source=root/"dist/src/messaging/process-message.js"; backup=source.with_suffix(".js.guanjia-provenance.bak")
    if package.get("version") != VERSION: raise SystemExit("refuse: unsupported WeChat plugin version")
    current=source.read_bytes()
    if a.revert:
        if not backup.exists(): raise SystemExit("refuse: no audited backup")
        shutil.copy2(backup,source); return 0
    if sha(current) != SOURCE_SHA256: raise SystemExit("refuse: source hash mismatch; update/patch already changed")
    if not a.apply:
        print(json.dumps({"ok":True,"version":VERSION,"source_sha256":sha(current),"would_patch":str(source)},ensure_ascii=False)); return 0
    backup.write_bytes(current); (source.parent/"guanjia-provenance-bridge.js").write_text(BRIDGE,encoding="utf-8")
    source.write_text(patch_source(current.decode("utf-8")),encoding="utf-8")
    manifest=Path.home()/".openclaw"/"workspace"/"guanjia"/"weixin-provenance-shim.json"; manifest.write_text(json.dumps({"version":VERSION,"original_sha256":SOURCE_SHA256,"patched_sha256":sha(source.read_bytes())},indent=2),encoding="utf-8")
if __name__ == "__main__": main()
