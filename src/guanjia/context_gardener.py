"""Context gardener: compact the main session when the house is quiet and the view is stable.

Signals (both required):
  - conversation idle  : main-watermark last activity older than idle_sec
  - vision stable      : no person and low change score sustained for stable_sec

Cycle:
  1. extract high-value memories from the last N hours of the main transcript
     into a dedicated gems memory file (interim high-value memory layer)
  2. reindex memory so gems become searchable
  3. compact the agent:main:main session via the official gateway RPC

Fail-closed: any unreadable signal skips the cycle entirely.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone as tz_module
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
"# Windows 无系统时区库，直接用固定 UTC+8 偏移"

import httpx

from guanjia.brain import Brain
from guanjia.sleep.watermark import read_main_watermark

_TZ = tz_module(timedelta(hours=8), 'Asia/Shanghai')
_NODE = r'C:\Users\inksn\AppData\Roaming\fnm\node-versions\v24.21.0\installation\node.exe'
_OPENCLAW_CLI = (r'C:\Users\inksn\AppData\Roaming\fnm\node-versions\v24.21.0'
                 r'\installation\node_modules\openclaw\dist\index.js')
_AGENT_DB = Path.home() / '.openclaw' / 'agents' / 'main' / 'agent' / 'openclaw-agent.sqlite'
_VISION_LATEST = Path(r'F:\coding\guanjia-v2-development\data\vision\latest.json')
_WORKSPACE = Path.home() / '.openclaw' / 'workspace'
_GARDEN_ROOT = Path(r'F:\coding\guanjia-v2-development\data\garden')
_MAIN_SESSION = 'agent:main:main'
_GARDEN_SESSION = 'agent:main:garden'
_POLL_SEC = 30.0


class ContextGardenerConfig:
    def __init__(self) -> None:
        self.idle_sec = float(os.environ.get('GUANJIA_GARDEN_IDLE_SEC', 300))
        self.stable_sec = float(os.environ.get('GUANJIA_GARDEN_STABLE_SEC', 120))
        self.cooldown_sec = float(os.environ.get('GUANJIA_GARDEN_COOLDOWN_SEC', 3600))
        self.transcript_hours = float(os.environ.get('GUANJIA_GARDEN_HOURS', 24))
        self.min_messages = int(os.environ.get('GUANJIA_GARDEN_MIN_MESSAGES', 10))
        self.max_transcript_chars = int(os.environ.get('GUANJIA_GARDEN_MAX_CHARS', 8000))
        self.change_threshold = float(os.environ.get('GUANJIA_GARDEN_CHANGE_THRESHOLD', 0.055))


class ContextGardener:
    """Background thread; one cycle per quiet window, cooldown-protected."""

    def __init__(self, config: ContextGardenerConfig | None = None) -> None:
        self.config = config or ContextGardenerConfig()
        _GARDEN_ROOT.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = threading.Lock()
        self._stable_since: float | None = None
        self._last_cycle_at = 0.0
        self._brain = Brain(session_key=_GARDEN_SESSION)

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='context-gardener', daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.wait(_POLL_SEC):
            try:
                if self._should_garden():
                    self.run_cycle()
            except Exception as exc:  # noqa: BLE001  never break the loop
                self._note({'ok': False, 'error': f'{type(exc).__name__}: {exc}'})

    # ---------- signals ----------

    def _should_garden(self) -> bool:
        now = time.time()
        if now - self._last_cycle_at < self.config.cooldown_sec:
            return False
        if not self._idle_ok():
            self._stable_since = None
            return False
        if not self._vision_stable_ok(now):
            return False
        return True

    def _idle_ok(self) -> bool:
        watermark = read_main_watermark()
        if not getattr(watermark, 'valid', False) or not watermark.last_activity_at:
            return False
        return (time.time() - float(watermark.last_activity_at)) >= self.config.idle_sec

    def _vision_stable_ok(self, now: float) -> bool:
        try:
            latest = json.loads(_VISION_LATEST.read_text(encoding='utf-8'))
        except Exception:
            self._stable_since = None
            return False
        if not latest.get('ok'):
            self._stable_since = None
            return False
        try:
            age = now - datetime.fromisoformat(str(latest['ts'])).timestamp()
        except (KeyError, ValueError):
            self._stable_since = None
            return False
        stable_row = (
            age < 120
            and not latest.get('person_present')
            and float(latest.get('change_score') or 0) < self.config.change_threshold
        )
        if not stable_row:
            self._stable_since = None
            return False
        if self._stable_since is None:
            self._stable_since = now
            return False
        return (now - self._stable_since) >= self.config.stable_sec

    # ---------- the cycle ----------

    def run_cycle(self) -> dict[str, object]:
        with self._running:
            transcript = self._read_transcript()
            # 内容变化量很少：没有可提取的宝石，也没有新的已完成轮次需要修剪，
            # compact 只会白白打断一次前缀缓存——整个周期跳过。
            # 水位线不推进：这点增量留到下次凑够一起处理，不丢消息。
            if len(transcript) < self.config.min_messages:
                report = {
                    'ok': True,
                    'ts': datetime.now(_TZ).isoformat(timespec='seconds'),
                    'skipped': 'low_activity',
                    'messages': len(transcript),
                }
                self._note(report)
                return report
            self._last_cycle_at = time.time()
            report: dict[str, object] = {
                'ok': True,
                'ts': datetime.now(_TZ).isoformat(timespec='seconds'),
                'messages': len(transcript),
            }
            memories: list[dict[str, str]] = []
            if len(transcript) >= self.config.min_messages:
                memories = self._extract(transcript)
                report['memories_extracted'] = len(memories)
                if memories:
                    path = self._write_gems(memories)
                    report['gems_file'] = str(path)
                    self._reindex_memory()
            pruned = self._prune_tool_results()
            if pruned:
                report['tool_results_pruned'] = pruned
            self._save_watermark()
            compact = self._compact_main_session()
            report['compact'] = compact
            self._note(report)
            return report

    def _read_transcript(self) -> list[tuple[str, str]]:
        last_ms = self._load_watermark()
        if last_ms:
            cutoff_ms = last_ms + 1  # 只读上次提取之后的新消息，不重复
        else:
            cutoff_ms = int((datetime.now(tz_module.utc) - timedelta(hours=self.config.transcript_hours)).timestamp() * 1000)
        rows: list[tuple[str, str]] = []
        try:
            db = sqlite3.connect(str(_AGENT_DB))
            db.row_factory = sqlite3.Row
            node = db.execute(
                'SELECT current_session_id FROM session_nodes WHERE session_key=?',
                (_MAIN_SESSION,),
            ).fetchone()
            if node is None:
                return rows
            sid = node['current_session_id']
            for record in db.execute(
                'SELECT event_json, created_at FROM transcript_events '
                'WHERE session_id=? AND created_at >= ? ORDER BY seq',
                (sid, cutoff_ms),
            ):
                if record['event_json'] is None:
                    continue
                try:
                    event = json.loads(record['event_json'])
                except json.JSONDecodeError:
                    continue
                message = event.get('message') or {}
                role = message.get('role')
                if role not in ('user', 'assistant'):
                    continue
                content = message.get('content')
                text = self._flatten(content)
                text = re.sub(r'\s+', ' ', text).strip()
                if text:
                    rows.append((str(role), text[:500]))
            db.close()
        except Exception:
            return rows
        return rows

    @staticmethod
    def _flatten(content: object) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get('text'), str):
                    parts.append(part['text'])
                elif isinstance(part, str):
                    parts.append(part)
            return ' '.join(parts)
        return ''

    def _extract(self, transcript: list[tuple[str, str]]) -> list[dict[str, str]]:
        lines = [f'{role}: {text}' for role, text in transcript]
        tail = '\n'.join(lines)[-self.config.max_transcript_chars:]
        prompt = (
            '以下是一个家庭助手最近的真实对话记录。提取值得长期记住的高价值信息，'
            '例如：主人的偏好与习惯、确定的事实、双方约定、重要事件。'
            '天气等临时查询和纯工具中间输出直接忽略；'
            '但闲聊不要按类型排除——闲聊中透露的想法、心情、口味、生活细节同样值得提取，'
            '是否保留由你按"对主人是否有长期价值"来判断，只有完全无实质内容的寒暄才跳过。'
            '若没有值得保留的内容，返回 {"memories": []}。'
            '只输出一个 JSON 对象，格式 {"memories": [{"topic": "主题", "fact": "一句事实"}]}，最多 10 条，不要 markdown。\n\n'
            f'对话记录：\n{tail}'
        )
        try:
            reply = self._brain.ask(prompt)
        except Exception:
            return []
        match = re.search(r'\{.*\}', reply, re.S)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        memories = parsed.get('memories') if isinstance(parsed, dict) else None
        result: list[dict[str, str]] = []
        if isinstance(memories, list):
            for item in memories[:10]:
                if isinstance(item, dict) and isinstance(item.get('fact'), str) and item['fact'].strip():
                    result.append({
                        'topic': str(item.get('topic') or '记录')[:20].strip() or '记录',
                        'fact': item['fact'].strip()[:160],
                    })
        return result

    def _write_gems(self, memories: list[dict[str, str]]) -> Path:
        gems_dir = _WORKSPACE / 'memory' / 'gems'
        gems_dir.mkdir(parents=True, exist_ok=True)
        path = gems_dir / f'gems-{datetime.now(_TZ).strftime("%Y-%m-%d")}.md'
        stamp = datetime.now(_TZ).strftime('%Y-%m-%d %H:%M')
        entry_lines = [f'- [{m["topic"]}] {m["fact"]}（{stamp} 园丁提取）' for m in memories]
        block = f'\n# {stamp} 上下文园丁提取\n\n' + '\n'.join(entry_lines) + '\n'
        with path.open('a', encoding='utf-8') as handle:
            handle.write(block)
        return path

    def _reindex_memory(self) -> None:
        try:
            subprocess.run([_NODE, _OPENCLAW_CLI, 'memory', 'index'],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=False)
        except Exception:
            pass

    def _compact_main_session(self) -> dict[str, object]:
        try:
            completed = subprocess.run(
                [_NODE, _OPENCLAW_CLI, 'sessions', 'compact', _MAIN_SESSION, '--json', '--timeout', '180000'],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=200, check=False,
            )
            stdout = (completed.stdout or '').strip()
            match = re.search(r'\{.*\}', stdout, re.S)
            payload: dict[str, object]
            if match:
                try:
                    payload = json.loads(match.group(0))
                except json.JSONDecodeError:
                    payload = {'raw': stdout[:400]}
            else:
                payload = {'raw': (stdout or (completed.stderr or '')[:400])}
            payload['exit_code'] = completed.returncode
            return payload
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}

    def _load_watermark(self):
        try:
            state=json.loads((_GARDEN_ROOT/'state.json').read_text(encoding='utf-8'))
            return state.get('last_extract_at_ms')
        except Exception:
            return None

    def _save_watermark(self):
        try:
            path=_GARDEN_ROOT/'state.json'
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps({'last_extract_at_ms':int(time.time()*1000)}),encoding='utf-8')
        except Exception:
            pass

    def _prune_tool_results(self) -> int:
        """Mark old tool results as context-ineligible.

        After the model has used a tool result to generate its response,
        the raw tool output no longer needs to stay in the context.
        The model's own assistant message already contains the conclusion.
        """
        try:
            db = sqlite3.connect(str(_AGENT_DB))
            # Mark toolResult events as ineligible, EXCEPT those from the current
            # unfinished turn (after the last user message)
            cursor = db.execute("""
                UPDATE session_transcript_active_events
                SET context_eligible = 0
                WHERE context_eligible = 1
                AND session_id = (
                    SELECT current_session_id FROM session_nodes
                    WHERE session_key = ?
                )
                AND event_seq IN (
                    SELECT te.seq FROM transcript_events te
                    WHERE te.event_json IS NOT NULL
                    AND te.event_json LIKE '%toolResult%'
                    AND te.seq < (
                        SELECT COALESCE(MAX(te2.seq), 0)
                        FROM transcript_events te2
                        WHERE te2.session_id = te.session_id
                        AND te2.event_json LIKE '%"role":"user"%'
                    )
                )
            """, (_MAIN_SESSION,))
            count = cursor.rowcount if cursor and cursor.rowcount and cursor.rowcount > 0 else 0
            db.commit()
            db.close()
            return count
        except Exception:
            return 0

    @staticmethod
    def _note(report: dict[str, object]) -> None:
        try:
            path = _GARDEN_ROOT / 'last-run.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass


__all__ = ['ContextGardener', 'ContextGardenerConfig']





