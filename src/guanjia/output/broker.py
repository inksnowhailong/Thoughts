"""Durable, idempotent output delivery."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Callable, Mapping, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from .contracts import (
    DeliveryAttempt, DeliveryRequirement, Expression, IdempotencyConflict,
    Importance, OutputAuthorizationContext, OutputRequest, OutputStatus, Receipt,
)
from .policy import ChannelStrategy, OutputAuthorizer


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f'{name} must be an aware UTC datetime')


def _claim_allowed(receipt: Receipt | None, phase: str) -> bool:
    if receipt is None:
        return False
    if phase in ('initial', 'authorization'):
        return receipt.status is OutputStatus.WAITING
    if phase.startswith('timeout'):
        return receipt.status in (OutputStatus.SENT, OutputStatus.FALLBACK)
    return False


def _clean_error(error: BaseException) -> str:
    """Only retain an exception class; messages may contain sensitive content."""
    return type(error).__name__


def _encode_request(request: OutputRequest) -> str:
    return json.dumps({
        'output_id': request.output_id, 'dedupe_key': request.dedupe_key,
        'actor_id': request.actor_id, 'conversation_id': request.conversation_id,
        'text': request.text, 'artifact_refs': request.artifact_refs,
        'preferred_channels': request.preferred_channels, 'fallback_channels': request.fallback_channels,
        'importance': request.importance.value, 'delivery_requirement': request.delivery_requirement.value,
        'ack_timeout_seconds': request.ack_timeout_seconds, 'task_id': request.task_id,
        'authorization': {
            'actor_id': request.authorization.actor_id, 'task_id': request.authorization.task_id,
            'intent': request.authorization.intent, 'capability': request.authorization.capability,
            'object_scope': request.authorization.object_scope, 'risk_scope': request.authorization.risk_scope,
            'provenance_summary': request.authorization.provenance_summary,
        },
    }, sort_keys=True, separators=(',', ':'))


def _decode_request(value: str) -> OutputRequest:
    item = json.loads(value)
    authorization = OutputAuthorizationContext(**item.pop('authorization'))
    item['artifact_refs'] = tuple(item['artifact_refs'])
    item['preferred_channels'] = tuple(item['preferred_channels'])
    item['fallback_channels'] = tuple(item['fallback_channels'])
    item['importance'] = Importance(item['importance'])
    item['delivery_requirement'] = DeliveryRequirement(item['delivery_requirement'])
    return OutputRequest(**item, authorization=authorization)


def _encode_expression(expression: Expression) -> str:
    return json.dumps(expression.__dict__, sort_keys=True, separators=(',', ':'))


def _decode_expression(value: str) -> Expression:
    return Expression(**json.loads(value))


def _fingerprint(request: OutputRequest) -> str:
    return _encode_request(request)


@dataclass(frozen=True)
class DeliveryClaim:
    output_id: str
    channel: str
    phase: str
    operation_id: str
    owner: str
    lease_until: datetime
    fencing_token: int
    receipt_version: int


class OutputStore(Protocol):
    def submit_with_payload(self, request: OutputRequest, expression: Expression) -> tuple[Receipt, bool]: ...
    def claim_delivery(self, output_id: str, channel: str, phase: str, operation_id: str,
                       owner: str, lease_until: datetime, now: datetime) -> DeliveryClaim | None: ...
    def complete_delivery(self, claim: DeliveryClaim, receipt: Receipt, attempt: DeliveryAttempt) -> bool: ...
    def fail_delivery(self, claim: DeliveryClaim, error: str, final: bool, now: datetime) -> Receipt | None: ...
    def mark_delivered(self, output_id: str, delivery_id: str, delivered_at: datetime) -> Receipt | None: ...
    def receipt(self, output_id: str) -> Receipt | None: ...
    def payload(self, output_id: str) -> tuple[OutputRequest, Expression] | None: ...
    def attempts_for(self, output_id: str) -> tuple[DeliveryAttempt, ...]: ...
    def due_must_be_seen(self, now: datetime, limit: int = 100) -> tuple[tuple[Receipt, OutputRequest, Expression], ...]: ...


class InMemoryOutputStore:
    """Thread-safe reference store with the same claim semantics as SQLite."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._receipts: dict[str, Receipt] = {}
        self._dedupe: dict[str, tuple[str, str]] = {}
        self._payloads: dict[str, tuple[OutputRequest, Expression]] = {}
        self._attempts: dict[str, list[DeliveryAttempt]] = {}
        self._ops: dict[tuple[str, str, str], DeliveryClaim] = {}
        self._completed: set[tuple[str, str, str]] = set()

    def submit_with_payload(self, request: OutputRequest, expression: Expression) -> tuple[Receipt, bool]:
        with self._lock:
            fingerprint = _fingerprint(request)
            dedupe = self._dedupe.get(request.dedupe_key)
            by_id = self._receipts.get(request.output_id)
            if dedupe:
                if dedupe[0] != fingerprint:
                    raise IdempotencyConflict('dedupe_key already belongs to different output semantics')
                return self._receipts[dedupe[1]], False
            if by_id:
                if by_id.dedupe_key != request.dedupe_key or self._dedupe[by_id.dedupe_key][0] != fingerprint:
                    raise IdempotencyConflict('output_id already belongs to different output semantics')
                return by_id, False
            receipt = Receipt(request.output_id, request.dedupe_key, OutputStatus.WAITING)
            self._receipts[request.output_id] = receipt
            self._dedupe[request.dedupe_key] = (fingerprint, request.output_id)
            self._payloads[request.output_id] = (request, expression)
            return receipt, True

    def claim_delivery(self, output_id: str, channel: str, phase: str, operation_id: str,
                       owner: str, lease_until: datetime, now: datetime) -> DeliveryClaim | None:
        _utc(now, 'now')
        _utc(lease_until, 'lease_until')
        if lease_until <= now:
            raise ValueError('lease_until must be after now')
        with self._lock:
            receipt = self._receipts.get(output_id)
            key = (output_id, channel, phase)
            prior = self._ops.get(key)
            if not _claim_allowed(receipt, phase) or key in self._completed or (prior and prior.lease_until > now):
                return None
            token = (prior.fencing_token if prior else 0) + 1
            claim = DeliveryClaim(output_id, channel, phase, operation_id, owner, lease_until, token, receipt.version + 1)
            self._ops[key] = claim
            self._receipts[output_id] = replace(
                receipt, version=claim.receipt_version, claim_channel=channel, claim_phase=phase,
                claim_operation_id=operation_id, claim_owner=owner, claim_until=lease_until,
                claim_fencing_token=token,
            )
            return claim

    def _owns(self, claim: DeliveryClaim) -> bool:
        current = self._ops.get((claim.output_id, claim.channel, claim.phase))
        receipt = self._receipts.get(claim.output_id)
        return bool(
            current == claim and receipt and receipt.version == claim.receipt_version
            and receipt.claim_operation_id == claim.operation_id and receipt.claim_owner == claim.owner
            and receipt.claim_fencing_token == claim.fencing_token
        )

    def complete_delivery(self, claim: DeliveryClaim, receipt: Receipt, attempt: DeliveryAttempt) -> bool:
        with self._lock:
            if not self._owns(claim):
                return False
            current = self._receipts[claim.output_id]
            if current.version != claim.receipt_version:
                return False
            self._receipts[claim.output_id] = receipt
            self._attempts.setdefault(claim.output_id, []).append(attempt)
            self._completed.add((claim.output_id, claim.channel, claim.phase))
            return True

    def fail_delivery(self, claim: DeliveryClaim, error: str, final: bool, now: datetime) -> Receipt | None:
        with self._lock:
            if not self._owns(claim):
                return None
            current = self._receipts[claim.output_id]
            if current.version != claim.receipt_version:
                return None
            next_receipt = (
                Receipt(current.output_id, current.dedupe_key, OutputStatus.FAILED, failure_reason=error, version=current.version + 1)
                if final else replace(
                    current, version=current.version + 1, claim_channel=None, claim_phase=None,
                    claim_operation_id=None, claim_owner=None, claim_until=None, claim_fencing_token=None,
                )
            )
            self._receipts[claim.output_id] = next_receipt
            self._attempts.setdefault(claim.output_id, []).append(
                DeliveryAttempt(claim.output_id, claim.channel, OutputStatus.FAILED, now, error=error)
            )
            self._completed.add((claim.output_id, claim.channel, claim.phase))
            return next_receipt

    def mark_delivered(self, output_id: str, delivery_id: str, delivered_at: datetime) -> Receipt | None:
        with self._lock:
            receipt = self._receipts.get(output_id)
            if receipt is None or receipt.delivery_id != delivery_id:
                raise ValueError('delivery receipt does not match output')
            if receipt.status is OutputStatus.DELIVERED:
                return receipt
            if receipt.status not in (OutputStatus.SENT, OutputStatus.FALLBACK):
                raise ValueError(f'cannot mark {receipt.status.value} output delivered')
            delivered = replace(receipt, status=OutputStatus.DELIVERED, delivered_at=delivered_at, version=receipt.version + 1)
            self._receipts[output_id] = delivered
            return delivered

    def receipt(self, output_id: str) -> Receipt | None:
        with self._lock:
            return self._receipts.get(output_id)

    def payload(self, output_id: str) -> tuple[OutputRequest, Expression] | None:
        with self._lock:
            return self._payloads.get(output_id)

    def attempts_for(self, output_id: str) -> tuple[DeliveryAttempt, ...]:
        with self._lock:
            return tuple(self._attempts.get(output_id, ()))

    def due_must_be_seen(self, now: datetime, limit: int = 100) -> tuple[tuple[Receipt, OutputRequest, Expression], ...]:
        with self._lock:
            return tuple(
                (receipt, *self._payloads[receipt.output_id])
                for receipt in self._receipts.values()
                if receipt.status in (OutputStatus.SENT, OutputStatus.FALLBACK)
                and receipt.sent_at is not None
                and self._payloads[receipt.output_id][0].delivery_requirement is DeliveryRequirement.MUST_BE_SEEN
                and now >= receipt.sent_at + timedelta(seconds=self._payloads[receipt.output_id][0].ack_timeout_seconds or 0)
            )[:limit]

    def close(self) -> None:
        return None


class SQLiteOutputStore:
    """Normalized SQLite store. Every state transition is a short transaction."""

    def __init__(self, path: str | Path) -> None:
        self._lock = RLock()
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self._path), timeout=5, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.execute('PRAGMA busy_timeout=5000')
        self._connection.executescript('''
            CREATE TABLE IF NOT EXISTS outputs (
              output_id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
              request_json TEXT NOT NULL, expression_json TEXT NOT NULL, status TEXT NOT NULL,
              channel TEXT, delivery_id TEXT, failure_reason TEXT, sent_at TEXT, delivered_at TEXT,
              version INTEGER NOT NULL, claim_channel TEXT, claim_phase TEXT, claim_operation_id TEXT,
              claim_owner TEXT, claim_until TEXT, claim_fencing_token INTEGER
            );
            CREATE TABLE IF NOT EXISTS dedupe (
              dedupe_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, output_id TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS attempts (
              id INTEGER PRIMARY KEY AUTOINCREMENT, output_id TEXT NOT NULL, channel TEXT NOT NULL,
              status TEXT NOT NULL, attempted_at TEXT NOT NULL, delivery_id TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS delivery_ops (
              output_id TEXT NOT NULL, channel TEXT NOT NULL, phase TEXT NOT NULL,
              operation_id TEXT NOT NULL, owner TEXT NOT NULL, lease_until TEXT NOT NULL,
              fencing_token INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (output_id, channel, phase)
            );
        ''')
        self._connection.commit()

    def _transaction(self, action):
        with self._lock:
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                value = action()
                self._connection.commit()
                return value
            except BaseException:
                self._connection.rollback()
                raise

    @staticmethod
    def _receipt(row: sqlite3.Row) -> Receipt:
        def date(column: str) -> datetime | None:
            return datetime.fromisoformat(row[column]) if row[column] else None
        return Receipt(
            row['output_id'], row['dedupe_key'], OutputStatus(row['status']), row['channel'], row['delivery_id'],
            row['failure_reason'], date('sent_at'), date('delivered_at'), row['version'],
            row['claim_channel'], row['claim_phase'], row['claim_operation_id'], row['claim_owner'],
            date('claim_until'), row['claim_fencing_token'],
        )

    @staticmethod
    def _values(receipt: Receipt) -> tuple[object, ...]:
        date = lambda value: value.isoformat() if value else None
        return (
            receipt.status.value, receipt.channel, receipt.delivery_id, receipt.failure_reason, date(receipt.sent_at),
            date(receipt.delivered_at), receipt.version, receipt.claim_channel, receipt.claim_phase,
            receipt.claim_operation_id, receipt.claim_owner, date(receipt.claim_until), receipt.claim_fencing_token,
            receipt.output_id,
        )

    def submit_with_payload(self, request: OutputRequest, expression: Expression) -> tuple[Receipt, bool]:
        fingerprint = _fingerprint(request)
        def action():
            dedupe = self._connection.execute('SELECT fingerprint, output_id FROM dedupe WHERE dedupe_key=?', (request.dedupe_key,)).fetchone()
            current = self._connection.execute('SELECT * FROM outputs WHERE output_id=?', (request.output_id,)).fetchone()
            if dedupe:
                if dedupe['fingerprint'] != fingerprint:
                    raise IdempotencyConflict('dedupe_key already belongs to different output semantics')
                return self._receipt(self._connection.execute('SELECT * FROM outputs WHERE output_id=?', (dedupe['output_id'],)).fetchone()), False
            if current:
                if current['dedupe_key'] != request.dedupe_key or current['fingerprint'] != fingerprint:
                    raise IdempotencyConflict('output_id already belongs to different output semantics')
                return self._receipt(current), False
            self._connection.execute(
                'INSERT INTO outputs(output_id,dedupe_key,fingerprint,request_json,expression_json,status,version) VALUES(?,?,?,?,?,?,0)',
                (request.output_id, request.dedupe_key, fingerprint, _encode_request(request), _encode_expression(expression), OutputStatus.WAITING.value),
            )
            self._connection.execute('INSERT INTO dedupe(dedupe_key,fingerprint,output_id) VALUES(?,?,?)', (request.dedupe_key, fingerprint, request.output_id))
            return Receipt(request.output_id, request.dedupe_key, OutputStatus.WAITING), True
        return self._transaction(action)

    def claim_delivery(self, output_id: str, channel: str, phase: str, operation_id: str,
                       owner: str, lease_until: datetime, now: datetime) -> DeliveryClaim | None:
        _utc(now, 'now')
        _utc(lease_until, 'lease_until')
        if lease_until <= now:
            raise ValueError('lease_until must be after now')
        def action():
            row = self._connection.execute('SELECT * FROM outputs WHERE output_id=?', (output_id,)).fetchone()
            op = self._connection.execute('SELECT * FROM delivery_ops WHERE output_id=? AND channel=? AND phase=?', (output_id, channel, phase)).fetchone()
            receipt = self._receipt(row) if row else None
            if not _claim_allowed(receipt, phase) or (op and op['completed']) or (op and datetime.fromisoformat(op['lease_until']) > now):
                return None
            token = (op['fencing_token'] if op else 0) + 1
            version = row['version'] + 1
            self._connection.execute('''
                INSERT INTO delivery_ops(output_id,channel,phase,operation_id,owner,lease_until,fencing_token,completed)
                VALUES(?,?,?,?,?,?,?,0)
                ON CONFLICT(output_id,channel,phase) DO UPDATE SET owner=excluded.owner,lease_until=excluded.lease_until,
                  fencing_token=excluded.fencing_token
            ''', (output_id, channel, phase, operation_id, owner, lease_until.isoformat(), token))
            self._connection.execute('''
                UPDATE outputs SET version=?,claim_channel=?,claim_phase=?,claim_operation_id=?,claim_owner=?,
                  claim_until=?,claim_fencing_token=? WHERE output_id=?
            ''', (version, channel, phase, operation_id, owner, lease_until.isoformat(), token, output_id))
            return DeliveryClaim(output_id, channel, phase, operation_id, owner, lease_until, token, version)
        return self._transaction(action)

    def _claim_current(self, claim: DeliveryClaim) -> bool:
        row = self._connection.execute('SELECT * FROM delivery_ops WHERE output_id=? AND channel=? AND phase=?', (claim.output_id, claim.channel, claim.phase)).fetchone()
        receipt = self._connection.execute('SELECT * FROM outputs WHERE output_id=?', (claim.output_id,)).fetchone()
        return bool(
            row and receipt and not row['completed'] and row['operation_id'] == claim.operation_id
            and row['owner'] == claim.owner and row['fencing_token'] == claim.fencing_token
            and receipt['version'] == claim.receipt_version and receipt['claim_operation_id'] == claim.operation_id
            and receipt['claim_owner'] == claim.owner and receipt['claim_fencing_token'] == claim.fencing_token
        )

    def complete_delivery(self, claim: DeliveryClaim, receipt: Receipt, attempt: DeliveryAttempt) -> bool:
        def action():
            if not self._claim_current(claim):
                return False
            cursor = self._connection.execute(
                '''UPDATE outputs SET status=?,channel=?,delivery_id=?,failure_reason=?,sent_at=?,delivered_at=?,version=?,
                   claim_channel=?,claim_phase=?,claim_operation_id=?,claim_owner=?,claim_until=?,claim_fencing_token=?
                   WHERE output_id=? AND version=?''',
                self._values(receipt) + (claim.receipt_version,),
            )
            if not cursor.rowcount:
                return False
            self._connection.execute('INSERT INTO attempts(output_id,channel,status,attempted_at,delivery_id,error) VALUES(?,?,?,?,?,?)',
                                     (attempt.output_id, attempt.channel, attempt.status.value, attempt.attempted_at.isoformat(), attempt.delivery_id, attempt.error))
            self._connection.execute('UPDATE delivery_ops SET completed=1 WHERE output_id=? AND channel=? AND phase=?',
                                     (claim.output_id, claim.channel, claim.phase))
            return True
        return self._transaction(action)

    def fail_delivery(self, claim: DeliveryClaim, error: str, final: bool, now: datetime) -> Receipt | None:
        def action():
            if not self._claim_current(claim):
                return None
            row = self._connection.execute('SELECT * FROM outputs WHERE output_id=? AND version=?', (claim.output_id, claim.receipt_version)).fetchone()
            if not row:
                return None
            current = self._receipt(row)
            receipt = (
                Receipt(row['output_id'], row['dedupe_key'], OutputStatus.FAILED, failure_reason=error, version=row['version'] + 1)
                if final else replace(
                    current, version=current.version + 1, claim_channel=None, claim_phase=None,
                    claim_operation_id=None, claim_owner=None, claim_until=None, claim_fencing_token=None,
                )
            )
            self._connection.execute(
                '''UPDATE outputs SET status=?,channel=?,delivery_id=?,failure_reason=?,sent_at=?,delivered_at=?,version=?,
                   claim_channel=?,claim_phase=?,claim_operation_id=?,claim_owner=?,claim_until=?,claim_fencing_token=?
                   WHERE output_id=?''', self._values(receipt),
            )
            self._connection.execute('INSERT INTO attempts(output_id,channel,status,attempted_at,delivery_id,error) VALUES(?,?,?,?,?,?)',
                                     (claim.output_id, claim.channel, OutputStatus.FAILED.value, now.isoformat(), None, error))
            self._connection.execute('UPDATE delivery_ops SET completed=1 WHERE output_id=? AND channel=? AND phase=? AND operation_id=? AND owner=? AND fencing_token=?',
                                     (claim.output_id, claim.channel, claim.phase, claim.operation_id, claim.owner, claim.fencing_token))
            return receipt
        return self._transaction(action)

    def receipt(self, output_id: str) -> Receipt | None:
        with self._lock:
            row = self._connection.execute('SELECT * FROM outputs WHERE output_id=?', (output_id,)).fetchone()
            return self._receipt(row) if row else None

    def payload(self, output_id: str) -> tuple[OutputRequest, Expression] | None:
        with self._lock:
            row = self._connection.execute('SELECT request_json,expression_json FROM outputs WHERE output_id=?', (output_id,)).fetchone()
            return (_decode_request(row['request_json']), _decode_expression(row['expression_json'])) if row else None

    def attempts_for(self, output_id: str) -> tuple[DeliveryAttempt, ...]:
        with self._lock:
            rows = self._connection.execute('SELECT * FROM attempts WHERE output_id=? ORDER BY id', (output_id,)).fetchall()
            return tuple(DeliveryAttempt(row['output_id'], row['channel'], OutputStatus(row['status']),
                         datetime.fromisoformat(row['attempted_at']), row['delivery_id'], row['error']) for row in rows)

    def due_must_be_seen(self, now: datetime, limit: int = 100) -> tuple[tuple[Receipt, OutputRequest, Expression], ...]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM outputs WHERE status IN ('sent','fallback') ORDER BY output_id").fetchall()
            result = []
            for row in rows:
                receipt, request = self._receipt(row), _decode_request(row['request_json'])
                if request.delivery_requirement is DeliveryRequirement.MUST_BE_SEEN and receipt.sent_at and now >= receipt.sent_at + timedelta(seconds=request.ack_timeout_seconds or 0):
                    result.append((receipt, request, _decode_expression(row['expression_json'])))
            return tuple(result[:limit])

    def mark_delivered(self, output_id: str, delivery_id: str, delivered_at: datetime) -> Receipt | None:
        def action():
            row = self._connection.execute('SELECT * FROM outputs WHERE output_id=?', (output_id,)).fetchone()
            if not row or row['delivery_id'] != delivery_id:
                raise ValueError('delivery receipt does not match output')
            receipt = self._receipt(row)
            if receipt.status is OutputStatus.DELIVERED:
                return receipt
            if receipt.status not in (OutputStatus.SENT, OutputStatus.FALLBACK):
                raise ValueError(f'cannot mark {receipt.status.value} output delivered')
            delivered = replace(receipt, status=OutputStatus.DELIVERED, delivered_at=delivered_at, version=receipt.version + 1)
            cursor = self._connection.execute(
                'UPDATE outputs SET status=?,delivered_at=?,version=? WHERE output_id=? AND version=? AND delivery_id=?',
                (delivered.status.value, delivered.delivered_at.isoformat(), delivered.version, output_id, receipt.version, delivery_id),
            )
            return delivered if cursor.rowcount else None
        return self._transaction(action)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class OutputBroker:
    def __init__(self, store: OutputStore, channels: Mapping[str, ChannelStrategy], authorizer: OutputAuthorizer,
                 clock: Callable[[], datetime] | None = None, owner: str | None = None, lease_seconds: float = 30) -> None:
        if type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError('lease_seconds must be a positive finite number')
        self._store, self._channels, self._authorizer = store, dict(channels), authorizer
        self._clock, self._owner, self._lease_seconds = clock or _now, owner or str(uuid4()), lease_seconds

    @staticmethod
    def _operation_id(output_id: str, channel: str, phase: str) -> str:
        return str(uuid5(NAMESPACE_URL, f'guanjia-output:{output_id}:{channel}:{phase}'))

    def _send(self, request: OutputRequest, expression: Expression, channels: tuple[str, ...], start: int,
              phase: str, now: datetime) -> Receipt:
        for index, channel_name in enumerate(channels[start:], start):
            current = self._store.receipt(request.output_id)
            if current is None:
                raise ValueError('output was not submitted')
            operation_id = self._operation_id(request.output_id, channel_name, phase)
            claim = self._store.claim_delivery(
                request.output_id, channel_name, phase, operation_id, self._owner,
                now + timedelta(seconds=self._lease_seconds), now,
            )
            if claim is None:
                return self._store.receipt(request.output_id) or current
            try:
                strategy = self._channels[channel_name]
                delivery_id = strategy.deliver(request, expression, operation_id)
            except Exception as error:
                result = self._store.fail_delivery(claim, _clean_error(error), index == len(channels) - 1, now)
                if result and result.status is OutputStatus.FAILED:
                    return result
                continue
            status = OutputStatus.SENT if index < len(request.preferred_channels) else OutputStatus.FALLBACK
            sent = Receipt(request.output_id, request.dedupe_key, status, channel_name, delivery_id, sent_at=now,
                           version=claim.receipt_version + 1)
            attempt = DeliveryAttempt(request.output_id, channel_name, OutputStatus.SENT, now, delivery_id)
            if self._store.complete_delivery(claim, sent, attempt):
                return sent
            return self._store.receipt(request.output_id) or current
        return self._store.receipt(request.output_id) or Receipt(request.output_id, request.dedupe_key, OutputStatus.WAITING)

    def submit(self, request: OutputRequest, expression: Expression) -> Receipt:
        receipt, _ = self._store.submit_with_payload(request, expression)
        if receipt.status is not OutputStatus.WAITING:
            return receipt
        if not self._authorizer.allows(request):
            # Authorization failure has no external attempt and is terminal.
            claim = self._store.claim_delivery(request.output_id, '__authorization__', 'authorization',
                                               self._operation_id(request.output_id, '__authorization__', 'authorization'),
                                               self._owner, self._clock() + timedelta(seconds=self._lease_seconds), self._clock())
            return self._store.fail_delivery(claim, 'AuthorizationDenied', True, self._clock()) if claim else (self._store.receipt(request.output_id) or receipt)
        return self._send(request, expression, request.preferred_channels + request.fallback_channels, 0, 'initial', self._clock())

    def mark_delivered(self, output_id: str, delivery_id: str) -> Receipt:
        delivered = self._store.mark_delivered(output_id, delivery_id, self._clock())
        if delivered is not None:
            return delivered
        return self._store.receipt(output_id) or (_ for _ in ()).throw(ValueError('delivery receipt does not match output'))

    def attempts_for(self, output_id: str) -> tuple[DeliveryAttempt, ...]:
        return self._store.attempts_for(output_id)

    def handle_timeouts(self, now: datetime) -> tuple[Receipt, ...]:
        _utc(now, 'now')
        updated = []
        for receipt, request, expression in self._store.due_must_be_seen(now):
            channels = request.preferred_channels + request.fallback_channels
            start = channels.index(receipt.channel) + 1 if receipt.channel in channels else len(channels)
            timeout_channel = receipt.channel or 'unknown'
            timeout_phase = 'timeout-ack'
            claim = self._store.claim_delivery(
                request.output_id, timeout_channel, timeout_phase,
                self._operation_id(request.output_id, timeout_channel, timeout_phase),
                self._owner, now + timedelta(seconds=self._lease_seconds), now,
            )
            if claim is None:
                continue
            acknowledged = self._store.fail_delivery(claim, 'AckTimeout', start >= len(channels), now)
            if acknowledged is None:
                continue
            if acknowledged.status is OutputStatus.FAILED:
                updated.append(acknowledged)
                continue
            result = self._send(request, expression, channels, start, 'timeout', now)
            if result != receipt:
                updated.append(result)
        return tuple(updated)
