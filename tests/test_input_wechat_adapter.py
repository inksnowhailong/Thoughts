import threading
import time
from datetime import datetime, timezone

from guanjia.input.contracts import MediaRef
from guanjia.input.adapters.wechat import WeChatInboundMessage, WeChatShadowAdapter


def check(value, message):
    if not value:
        raise AssertionError(message)


now = datetime(2026, 10, 16, tzinfo=timezone.utc)
received = []
started = threading.Event()
release = threading.Event()


def slow_sink(record):
    received.append(record)
    started.set()
    release.wait(1)


adapter = WeChatShadowAdapter(slow_sink, max_queue_size=1)
first = WeChatInboundMessage('wechat-1', 'corr-wechat-1', 'sender-1', '你好', now, {'nested': {'safe': True}})
second = WeChatInboundMessage('wechat-2', 'corr-wechat-2', 'sender-2', '第二条', now)
third = WeChatInboundMessage('wechat-3', 'corr-wechat-3', 'sender-3', '第三条', now)
check(adapter.observe(first), 'inbound OpenClaw message is queued')
check(started.wait(1), 'background sink receives the first message')
check(adapter.observe(second), 'bounded queue accepts one pending shadow')
started_at = time.monotonic()
check(not adapter.observe(third), 'full queue drops only the shadow')
check(time.monotonic() - started_at < 0.1, 'OpenClaw receive path never blocks')
check(not adapter.observe(first), 'duplicate inbound ID is idempotently ignored')
try:
    first.metadata['nested']['safe'] = False
except TypeError:
    pass
else:
    raise AssertionError('inbound DTO metadata is deeply immutable')
release.set()
check(adapter.drain(timeout=1), 'drain completes queued inbound shadows')
check([record.input_id for record in received] == ['wechat-1', 'wechat-2'], 'correlated unique messages reach sink')


def failing_sink(_record):
    raise RuntimeError('unavailable')


failing = WeChatShadowAdapter(failing_sink, max_queue_size=1)
check(failing.observe(WeChatInboundMessage('wechat-4', 'corr-wechat-4', 'sender-4', '隔离', now)), 'sink errors cannot fail OpenClaw receive')
check(failing.drain(timeout=1), 'failed sink is isolated asynchronously')
check(failing.metrics().sink_failures == 1, 'sink failure is counted')
failing.close()
adapter.close()

attachment = MediaRef('b' * 64, 'b' * 64, 'image', 2)
rich = WeChatInboundMessage(
    'wechat-rich',
    'corr-wechat-rich',
    'sender-rich',
    '有附件',
    now,
    reply_target='openclaw-message-9',
    attachments=(attachment,),
)
record = rich.as_shadow_record()
check(rich.reply_target == 'openclaw-message-9', 'reply target is retained as inbound provenance')
check(record.metadata['attachments'][0]['media_id'] == attachment.media_id, 'attachments retain references only')
try:
    record.metadata['attachments'][0]['media_id'] = 'changed'
except TypeError:
    pass
else:
    raise AssertionError('attachment references remain deeply immutable')
print('ALL PASS test_input_wechat_adapter')
