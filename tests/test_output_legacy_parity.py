from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from guanjia.output import IdempotencyConflict, OutputAuthorizationContext, OutputRequest
from guanjia.output.adapters import InMemoryAuditSink, redact
from guanjia.output.adapters.legacy_voice import LegacyVoiceShadowAdapter
from guanjia.output.adapters.panel import PanelShadowAdapter
from guanjia.output.adapters.wechat import WechatShadowAdapter
from guanjia.permission import ActorEvidence, AuthorizationRequest, PermissionDecision, PermissionOutcome, Reversibility, Role


def check(value: bool, label: str) -> None:
    print(('PASS ' if value else 'FAIL ') + label)
    if not value:
        raise SystemExit(1)


now = datetime(2026, 10, 16, 11, 0, tzinfo=timezone.utc)
sink = InMemoryAuditSink()
request = OutputRequest(
    'output-1', 'task-1:done', 'owner', 'agent:main:main', '任务完成', (), ('voice',),
    authorization=OutputAuthorizationContext('owner', 'task-1', 'notify', 'task.notice', 'task-1', 'low', 'worker'),
)
voice = LegacyVoiceShadowAdapter(sink)
result = voice.mirror_reply(
    request,
    {'kind': 'reply', 'output_id': 'output-1', 'actor_id': 'owner', 'conversation_id': 'agent:main:main',
     'text': '不同回复 Authorization: Bearer abc.def password=hunter2 C:/private/movie.mp4?token=abc',
     'channels': ('voice',), 'nested': ({'text': 'sk-abcdefgh https://host/x?api_key=secret'},)},
    now,
)
check(result.recorded and any(item['field'] == 'text' for item in result.differences), 'voice mirrors reply as a structured comparison')
stored = sink.events[result.event_id]
check(not any(secret in repr(stored) for secret in ('C:/private', 'movie.mp4', 'hunter2', 'abc.def', 'sk-abcdefgh', 'api_key=secret')),
      'nested content, tokens, URL queries, and media paths are redacted')
check(redact({'list': [{'text': 'file:///tmp/a.wav C:/private/a.mp3 secret=hidden'}]}) == {'list': [{'text': '<redacted:path> <redacted:path> secret=<redacted>'}]},
      'content redaction applies recursively outside sensitive field names')
check(
    str(redact('x' * 2_049)).endswith('<truncated:string>')
    and redact(list(range(65)))[-1] == '<truncated:items>',
    'audit strings and collections have bounded evidence size',
)
for invalid_observation in ({1: 'not-a-string-key'}, {'items': {1, 2}}):
    try:
        voice.mirror_reply(request, invalid_observation, now)
    except ValueError:
        check(True, 'non-JSON legacy observations are rejected before audit')
    else:
        check(False, 'non-JSON legacy observations are rejected before audit')

again = LegacyVoiceShadowAdapter(sink).mirror_reply(
    request,
    {'kind': 'reply', 'output_id': 'output-1', 'actor_id': 'owner', 'conversation_id': 'agent:main:main',
     'text': '不同回复 Authorization: Bearer abc.def password=hunter2 C:/private/movie.mp4?token=abc',
     'channels': ('voice',), 'nested': ({'text': 'sk-abcdefgh https://host/x?api_key=secret'},)},
    now.replace(microsecond=1),
)
check(not again.recorded and again.isolated_error is None and stored['observed_at'] == '2026-10-16T11:00:00Z',
      'tuple inputs normalize and replay preserves the first observed_at timestamp')

parallel_sink = InMemoryAuditSink()
parallel = LegacyVoiceShadowAdapter(parallel_sink)
with ThreadPoolExecutor(max_workers=8) as workers:
    outcomes = list(workers.map(
        lambda _: parallel.mirror_reply(
            request,
            {'kind': 'reply', 'output_id': 'output-1', 'actor_id': 'owner', 'conversation_id': 'agent:main:main',
             'text': '任务完成', 'channels': ['voice']},
            now,
        ).recorded,
        range(16),
    ))
check(sum(outcomes) == 1 and len(parallel_sink.events) == 1, 'concurrent mirrors persist exactly one event')
try:
    parallel_sink.record(next(iter(parallel_sink.events)), {'schema': 'different'})
except IdempotencyConflict:
    check(True, 'same event id with conflicting redacted semantics is rejected')
else:
    check(False, 'same event id with conflicting redacted semantics is rejected')


class BrokenSink:
    def record(self, event_id, event):
        raise OSError('audit storage offline')


isolated = LegacyVoiceShadowAdapter(BrokenSink()).mirror_reply(
    request, {'kind': 'reply'}, now,
)
check(not isolated.recorded and isolated.isolated_error == 'OSError', 'audit write failure is isolated from legacy reply')

panel = PanelShadowAdapter(sink)
panel_result = panel.mirror_task_notice(
    request,
    {'kind': 'task_notice', 'output_id': 'output-1', 'task_id': 'task-1', 'text': '任务完成', 'channels': ['voice']},
    now,
)
check(panel_result.recorded, 'panel task notice remains a passive audit event')

owner = ActorEvidence('owner', Role.OWNER, ('wechat:owner',))
permission_request = AuthorizationRequest(
    'permission-1', owner, 'agent:main:main', 'task-1', 'publish', 'message.public_send',
    'target-private', 'public', True, Reversibility.REVERSIBLE, 'user request', now,
)
wechat = WechatShadowAdapter(sink)
wechat_result = wechat.mirror_permission_request(
    permission_request,
    PermissionDecision(PermissionOutcome.CONFIRMATION_REQUIRED, 'confirmation required', 'todo-1'),
    {'kind': 'permission_request', 'request_id': 'permission-1', 'actor_id': 'owner', 'session_id': 'agent:main:main',
     'task_id': 'task-1', 'outcome': 'confirmation_required', 'todo_id': 'todo-1', 'params': {'path': 'C:\\private\\x'}},
    now,
)
check(wechat_result.recorded, 'wechat permission request is mirrored without sending or approving')
check('C:\\private\\x' not in repr(sink.events[wechat_result.event_id]), 'permission tool parameters are redacted')

print('ALL PASS test_output_legacy_parity')
