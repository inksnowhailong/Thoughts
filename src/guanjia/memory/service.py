"""Memory Service（Phase 4）：SQLite 记忆库 + 检索 + 导入候选。

- 存储复用 memory/repository.py 的 MemoryRepository（SQLiteBackend，WAL）；
- 检索 = MemoryQuery（关键词包含匹配 + importance/confidence 排序），
  返回包内 content 截到 ≤500 字（Worker 上下文预算，重构计划 §1.2）；
- OpenClaw workspace 存量记忆经 staging/review/commit 链导入：
  scripts/import_openclaw_memory.py 产出候选 → 审阅 decisions.json → 本服务落库。
"""

from __future__ import annotations

import re
from pathlib import Path
from threading import RLock

from .contracts import (
    MemoryCandidate,
    MemoryPacket,
    MemoryQuery,
    MemoryRecord,
)
from .repository import MemoryRepository

# Worker 上下文预算（重构计划）：检索包 ≤8 条 × 500 字
MAX_PACKET_RECORDS = 8
MAX_RECORD_CHARS = 500


def clamp_content(text: str, limit: int = MAX_RECORD_CHARS) -> str:
    """按码点截断，保证进入 Worker 上下文的单条记忆不超过预算。"""
    return ''.join(list(str(text or '').strip())[:limit])


class MemoryService:
    """业务真相的记忆门面：bridge /internal/memory/search 与导入脚本的唯一后端。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._repo = MemoryRepository.sqlite_path(str(self._path))
        self._lock = RLock()

    def close(self) -> None:
        with self._lock:
            self._repo.close()

    def write(self, candidate: MemoryCandidate) -> MemoryRecord:
        """写入/更新一条记忆（同 key 覆盖，冲突进 conflicts 表）。"""
        with self._lock:
            return self._repo.write(candidate)

    def get(self, key: str) -> MemoryRecord | None:
        return self._repo.get(key)

    _KEYWORD_SPLIT = r'[\s，。！？、,.\?!;；:：/\\|()\[\]{}<>「」『』"\'`~@#$%^&*\-_=+]+'

    @classmethod
    def _keywords(cls, text: str) -> tuple[str, ...]:
        """查询分词：空白/标点切分；保留 ≥2 码点的词（中文短语不硬切）。"""
        words = [w.casefold() for w in re.split(cls._KEYWORD_SPLIT, str(text or '')) if w]
        return tuple(dict.fromkeys(w for w in words if len(w) >= 2)) or (
            (text.strip().casefold(),) if text.strip() else ())

    def search(
        self,
        text: str,
        *,
        people: tuple[str, ...] = (),
        types: tuple[str, ...] = (),
        limit: int = MAX_PACKET_RECORDS,
    ) -> MemoryPacket:
        """检索最小记忆包：多关键词打分（命中数×词长 + importance/confidence），≤limit 条。

        每条 content 钳到 ≤500 字；无关键词命中时回退 repository 的子串查询（保持旧行为）。
        """
        limit = max(1, min(int(limit), MAX_PACKET_RECORDS))
        keywords = self._keywords(text)
        people_set = set(people)
        types_set = set(types)
        scored: list[tuple[float, MemoryRecord]] = []
        with self._lock:
            for record in self._repo.records():
                if types_set and record.memory_type not in types_set:
                    continue
                if people_set and not people_set.intersection(record.people):
                    continue
                haystack = f'{record.key}\n{record.content}'.casefold()
                score = 0.0
                for word in keywords:
                    if word in haystack:
                        score += 1.0 + min(len(word), 20) / 20.0
                if not keywords:
                    # 空查询：只按 importance/confidence 取头部
                    score = 1.0
                if score > 0.0:
                    score += record.importance / 10.0 + record.confidence / 10.0
                    scored.append((score, record))
        if not scored:
            return self.clamp_packet(self._repo.query(MemoryQuery(
                text=text, people=tuple(people), types=tuple(types), limit=limit)))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return self.clamp_packet(MemoryPacket(
            tuple(record for _, record in scored[:limit]),
            MemoryQuery(text=text, people=tuple(people), types=tuple(types), limit=limit),
        ))

    @staticmethod
    def clamp_packet(packet: MemoryPacket) -> MemoryPacket:
        clamped = MemoryPacket(
            tuple(
                MemoryRecord(
                    id=record.id, key=record.key,
                    content=clamp_content(record.content),
                    memory_type=record.memory_type, source=record.source,
                    confidence=record.confidence, importance=record.importance,
                    retention=record.retention, people=record.people,
                )
                for record in packet.records
            ),
            packet.query,
        )
        return clamped

    @staticmethod
    def packet_to_wire(packet: MemoryPacket) -> dict[str, object]:
        """MemoryPacket → JSON-safe dict（bridge 响应形状）。"""
        return {
            'query': {'text': packet.query.text, 'people': list(packet.query.people),
                      'types': list(packet.query.types), 'limit': packet.query.limit},
            'records': [
                {
                    'id': record.id, 'key': record.key, 'content': record.content,
                    'memory_type': record.memory_type, 'confidence': record.confidence,
                    'importance': record.importance, 'people': list(record.people),
                    'updated_at': record.updated_at.isoformat(),
                }
                for record in packet.records
            ],
        }
