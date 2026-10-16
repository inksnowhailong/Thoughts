"""P4-6d 控制端初代资料源清单。

只对明确白名单中的文件做字节流哈希，不解码或提取正文，不生成记忆候选。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from guanjia.sleep.staging import ROOT

# 初代资料只在控制端；不能用 Path.home()，否则远程家机可能误被当成来源。
CONTROL_SOURCE = Path(r"C:\Users\moxue\.thoughts\instances\大师")
SOURCE_STAGING_ROOT = ROOT / "data" / "sleep" / "staging" / "gen1-source"
_ALLOWED = {
    "user-portrait.md": "portrait",
    "memory-consolidated.md": "memory-summary",
    "memory-raw.md": "memory-raw",
    "persona.md": "persona",
    "profile.json": "profile",
    "personality.json": "personality",
}
_SENSITIVE_NAME = ("key", "token", "secret", "credential", "password", "permission", "session", "log", "state")


class SourceAdapterRefused(ValueError):
    """源路径或 staging 位置不符合 P4-6d 本地审计边界。"""


@dataclass(frozen=True)
class SourceItem:
    path: str
    sha256: str
    bytes: int
    category: str


@dataclass(frozen=True)
class SourceManifest:
    source_root: str
    sources: tuple[SourceItem, ...]
    excluded: tuple[dict[str, str], ...]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _hash_file(path: Path) -> str:
    """仅流式读取原始字节计算摘要；不 decode、不保留或输出正文。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_sources(source_root: Path, *, allowed_roots: Iterable[Path] = (CONTROL_SOURCE,)) -> SourceManifest:
    source_root = source_root.resolve()
    if not any(source_root == root.resolve() for root in allowed_roots):
        raise SourceAdapterRefused("source 必须是明确允许的控制端初代目录")
    if not source_root.is_dir():
        raise SourceAdapterRefused("source 目录不存在")
    sources: list[SourceItem] = []
    excluded: list[dict[str, str]] = []
    for path in sorted(source_root.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file():
            excluded.append({"path": path.name, "reason": "not_top_level_regular_file"})
            continue
        name = path.name
        lowered = name.casefold()
        if any(marker in lowered for marker in _SENSITIVE_NAME):
            excluded.append({"path": name, "reason": "sensitive_filename"})
            continue
        category = _ALLOWED.get(name)
        if category is None:
            excluded.append({"path": name, "reason": "not_allowlisted"})
            continue
        sources.append(SourceItem(name, _hash_file(path), path.stat().st_size, category))
    return SourceManifest(str(source_root), tuple(sources), tuple(excluded))


def stage_manifest(
    manifest: SourceManifest,
    staging_root: Path = SOURCE_STAGING_ROOT,
    *,
    run_id: str | None = None,
    allowed_roots: Iterable[Path] = (ROOT / "data", ROOT / "tmp"),
) -> Path:
    """只写项目 data/tmp 下的源审计清单；绝不写候选或 workspace。"""
    if not any(_inside(staging_root, root) for root in allowed_roots):
        raise SourceAdapterRefused("staging-root 必须位于项目 data/ 或 tmp/")
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + "-source"
    if not run_id.replace("-", "").replace("_", "").isalnum():
        raise SourceAdapterRefused("run-id 不合法")
    directory = staging_root.resolve() / run_id
    if directory.exists():
        raise FileExistsError(f"staging 已存在：{directory}")
    directory.mkdir(parents=True)
    payload = {
        "kind": "p4-gen1-source-manifest",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_root": manifest.source_root,
        "sources": [asdict(item) for item in manifest.sources],
        "excluded": list(manifest.excluded),
        "next_step": "人工或本地模型在控制端读取正文并蒸馏；本清单不含正文，也不能直接导入。",
    }
    (directory / "source-manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return directory
