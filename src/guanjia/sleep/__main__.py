"""P4 sleep 包的手动安全工具；当前只提供 staging，不接入休眠。"""

from __future__ import annotations

import argparse
from pathlib import Path

from guanjia.sleep.committer import CommitRefused, commit_staging
from guanjia.sleep.gen1_committer import Gen1CommitRefused, commit_gen1
from guanjia.sleep.gen1_import import CandidateInputError, GEN1_STAGING_ROOT, load_candidate_input, prepare_candidates, stage_candidates
from guanjia.sleep.gen1_source import CONTROL_SOURCE, SOURCE_STAGING_ROOT, SourceAdapterRefused, discover_sources, stage_manifest
from guanjia.sleep.staging import ROOT, STAGING_ROOT, WORKSPACE, SnapshotChanged, snapshot_workspace, stage_index_snapshot
from guanjia.sleep.rehearsal import SleepRehearsal
from guanjia.wsfiles import WorkspaceFiles


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P4 分层记忆安全 staging（不写 workspace）")
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage-index", help="为 MEMORY/index 生成只读 staging")
    stage.add_argument("--workspace", type=Path, default=WORKSPACE)
    stage.add_argument("--staging-root", type=Path, default=STAGING_ROOT)
    stage.add_argument("--run-id", default=None)
    rehearse = sub.add_parser("rehearse", help="P4 休眠只读演练：只写 staging，绝不提交或换新")
    rehearse.add_argument("--last-message-at", type=float, required=True, help="主线最后用户消息 epoch 秒（演练输入）")
    rehearse.add_argument("--staging-root", type=Path, default=STAGING_ROOT)
    rehearse.add_argument("--run-id", default=None)
    rehearse.add_argument("--dry-run", action="store_true")
    commit = sub.add_parser("commit-staging", help="提交已人工审核的 MEMORY/index staging")
    commit.add_argument("staging", type=Path, help="必须显式指定 data/ 或 tmp/ 下的 staging 目录")
    commit.add_argument("--workspace", type=Path, default=WORKSPACE)
    mode = commit.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="仅验证，不写 workspace")
    mode.add_argument("--commit", action="store_true", help="通过全部验证后才写入")
    gen1 = sub.add_parser("gen1-stage", help="P4-6 初代画像候选暂存（不写 workspace）")
    gen1.add_argument("--input", type=Path, required=True, help="控制端已蒸馏的 candidates JSON")
    gen1.add_argument("--staging-root", type=Path, default=GEN1_STAGING_ROOT)
    gen1.add_argument("--run-id", default=None)
    gen1mode = gen1.add_mutually_exclusive_group(required=True)
    gen1mode.add_argument("--dry-run", action="store_true", help="只输出候选统计，不写 staging")
    gen1mode.add_argument("--stage", action="store_true", help="只写 data/tmp staging，所有候选保持 pending")
    gen1commit = sub.add_parser("gen1-commit", help="提交已确认的 gen1 暂存（默认拒绝写 workspace）")
    gen1commit.add_argument("staging", type=Path)
    gen1commit.add_argument("--workspace", type=Path, default=WORKSPACE)
    gen1commit_mode = gen1commit.add_mutually_exclusive_group(required=True)
    gen1commit_mode.add_argument("--dry-run", action="store_true")
    gen1commit_mode.add_argument("--commit", action="store_true")
    source = sub.add_parser("gen1-source-manifest", help="控制端初代资料源清单（不读取正文语义）")
    source.add_argument("--source", type=Path, required=True, help=f"仅允许 {CONTROL_SOURCE}")
    source.add_argument("--staging-root", type=Path, default=SOURCE_STAGING_ROOT)
    source.add_argument("--run-id", default=None)
    source_mode = source.add_mutually_exclusive_group(required=True)
    source_mode.add_argument("--dry-run", action="store_true")
    source_mode.add_argument("--stage", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "rehearse":
        try:
            result = SleepRehearsal(staging_root=args.staging_root).run(
                last_message_at=args.last_message_at, run_id=args.run_id, dry_run=args.dry_run)
        except (OSError, ValueError, FileExistsError, SnapshotChanged) as e:
            parser.error(str(e))
        print(result.directory or result.reason)
        return 0
    if args.command == "stage-index":
        # CLI 只允许把产物放在项目 data/ 或 tmp/，防止把“staging”误用成任意覆盖路径。
        if not (_inside(args.staging_root, ROOT / "data") or _inside(args.staging_root, ROOT / "tmp")):
            parser.error("--staging-root 必须位于项目 data/ 或 tmp/")
        try:
            result = stage_index_snapshot(snapshot_workspace(args.workspace), args.staging_root, run_id=args.run_id)
        except (OSError, ValueError, FileExistsError, SnapshotChanged) as e:
            parser.error(str(e))
        print(result.directory)
        return 0
    if args.command == "gen1-stage":
        try:
            batch = prepare_candidates(load_candidate_input(args.input))
            if args.dry_run:
                print(f"pending={len(batch.candidates)} duplicates={len(batch.duplicates)} rejected={len(batch.rejected)}")
                return 0
            result = stage_candidates(batch, args.staging_root, run_id=args.run_id)
        except (OSError, ValueError, FileExistsError, CandidateInputError) as e:
            parser.error(str(e))
        print(result.directory)
        return 0
    if args.command == "gen1-commit":
        try:
            result = commit_gen1(args.staging, WorkspaceFiles(root=args.workspace), commit=args.commit)
        except (OSError, ValueError, Gen1CommitRefused) as e:
            parser.error(str(e))
        print(result.staging)
        return 0
    if args.command == "gen1-source-manifest":
        try:
            manifest = discover_sources(args.source)
            if args.dry_run:
                print(f"sources={len(manifest.sources)} excluded={len(manifest.excluded)}")
                return 0
            print(stage_manifest(manifest, args.staging_root, run_id=args.run_id))
            return 0
        except (OSError, ValueError, FileExistsError, SourceAdapterRefused) as e:
            parser.error(str(e))
    try:
        result = commit_staging(args.staging, WorkspaceFiles(root=args.workspace), commit=args.commit)
    except (OSError, ValueError, CommitRefused) as e:
        parser.error(str(e))
    print(result.staging)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
