"""Wave1-B：bridge 服务全路径测试。

覆盖：鉴权、404、policy/check 三态（allow/deny/confirmation_required）、
/decide 审批兼容口、/internal/policy/confirm 一次性凭证（用过即失效、过期 120s）、
工具 exec（override mock，不发起外网）、审计落盘（JSONL + permission/audit.py）、
worker 审批登记与回调、approval_ipc.py 的 GUANJIA_APPROVAL_URL 覆盖。
天气/web_fetch/speak 的 ok 路径全部用 override mock；真实外呼不进测试。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from guanjia.approval_ipc import ApprovalIpc
from guanjia.bridge import action_fingerprint
from guanjia.bridge_service import BridgeApp, make_bridge_server


def check(condition: bool, name: str) -> None:
    print(('PASS ' if condition else 'FAIL ') + name)
    if not condition:
        raise SystemExit(1)


# ---------- 可控时钟 ----------

START = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
clock = [START]


def now_fn() -> datetime:
    return clock[0]


def advance(seconds: int) -> None:
    clock[0] = clock[0] + timedelta(seconds=seconds)


# ---------- 工具 override（不发外网） ----------


def fake_get_weather(args: dict) -> dict:
    return {'ok': True, 'output': {'text': f"家这边：现在18度，晴。不用带伞。city={args.get('city')}",
                                   'place': '家这边', 'umbrella': False}}


def fake_web_fetch(args: dict) -> dict:
    return {'ok': True, 'output': {'url': args.get('url'), 'status': 200, 'text': '正文' * 100}}


def fake_speak(args: dict) -> dict:
    return {'ok': True, 'output': {'text': args.get('text'), 'reason': 'queued'}}


# ---------- 起 bridge ----------

DATA_DIR = Path(tempfile.mkdtemp(prefix='guanjia-bridge-test-'))
APP = BridgeApp(
    data_dir=DATA_DIR, now_fn=now_fn,
    tool_overrides={'get_weather': fake_get_weather, 'web_fetch': fake_web_fetch, 'speak': fake_speak},
)
SERVER = make_bridge_server(APP, port=0, token='test-token', approval_cap='test-cap')
threading.Thread(target=SERVER.serve_forever, name='bridge-test', daemon=True).start()
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"
CLIENT = httpx.Client(base_url=BASE, headers={'Authorization': 'Bearer test-token'},
                      trust_env=False, timeout=5.0)


def get(path: str, **kwargs) -> httpx.Response:
    return CLIENT.get(path, **kwargs)


def post(path: str, body: dict, **kwargs) -> httpx.Response:
    return CLIENT.post(path, json=body, **kwargs)


def policy_check(capability_id: str, args: dict, session_id: str = 'voice-main',
                 task_id: str | None = None, credential: str | None = None) -> dict:
    response = post('/internal/policy/check', {
        'session_id': session_id, 'capability_id': capability_id, 'args': args,
        'task_id': task_id, 'credential': credential,
    })
    check(response.status_code == 200, f'policy/check {capability_id} http 200')
    return response.json()


# ---------- 1. 基础：鉴权与未知路由 ----------

r = get('/no-such-route')
check(r.status_code == 404 and r.json().get('reason') == 'not_found', '未知路由 404 JSON')
r = httpx.get(f'{BASE}/status', trust_env=False, timeout=5.0)  # 无 token
check(r.status_code == 403, '缺 Bearer 拒绝')
r = httpx.get(f'{BASE}/status', headers={'Authorization': 'Bearer wrong'}, trust_env=False, timeout=5.0)
check(r.status_code == 403, '错 token 拒绝')
r = get('/status')
check(r.status_code == 200 and r.json().get('ok') is True, '/status 带 token 可读')

# ---------- 2. policy/check 三态 ----------

decision = policy_check('get_weather', {})
check(decision['outcome'] == 'allow', 'L0 get_weather 直接放行')
decision = policy_check('no_such_capability', {'x': 1})
check(decision['outcome'] == 'deny' and 'role policy' in decision['reason'], '未登记能力被角色矩阵拒绝')
decision = policy_check('web_fetch', {'url': 'https://example.com/'})
check(decision['outcome'] == 'allow', 'L0 web_fetch 放行')

# L2 → confirmation_required
decision = policy_check('terminal', {'command': 'rm -rf /tmp/x'})
check(decision['outcome'] == 'confirmation_required', 'L2 terminal 触发 confirmation_required')
todo_1 = decision.get('todo_id')
check(bool(todo_1), '返回 todo_id')
check('terminal' in (decision.get('summary') or ''), '摘要包含能力名')
decision_repeat = policy_check('terminal', {'command': 'rm -rf /tmp/x'})
check(decision_repeat['outcome'] == 'confirmation_required'
      and decision_repeat.get('todo_id') == todo_1, 'pending 期间重查返回同一 todo')

# ---------- 3. /latest 审批兼容口 ----------

r = get('/latest', headers={'X-Guanjia-Approval-Cap': 'test-cap', 'Authorization': ''})
check(r.status_code == 200, '/latest 用能力串可读')
pending = r.json().get('pending')
check(pending is not None and pending.get('id') == todo_1, '/latest 能看到 pending')
check(pending.get('status') == 'open' and pending.get('expiresAt'), 'pending 形状同旧接口')
r = get('/latest', headers={'X-Guanjia-Approval-Cap': 'wrong-cap', 'Authorization': ''})
check(r.status_code == 403, '/latest 错能力串拒绝')
r = get('/latest?session=voice-main', headers={'X-Guanjia-Approval-Cap': 'test-cap', 'Authorization': ''})
check((r.json().get('pending') or {}).get('id') == todo_1, '/latest 按会话过滤可见')
r = get('/latest?session=other-session', headers={'X-Guanjia-Approval-Cap': 'test-cap', 'Authorization': ''})
check(r.json().get('pending') is None, '/latest 别的会话看不到')

# ---------- 4. /decide 口头批准 → 原样重查放行（语音重试语义） ----------

r = post('/decide', {'id': todo_1, 'decision': 'approve'},
         headers={'X-Guanjia-Approval-Cap': 'test-cap', 'Authorization': ''})
check(r.status_code == 200 and r.json().get('ok') is True, '/decide 批准成功')
r = post('/decide', {'id': todo_1, 'decision': 'approve'},
         headers={'X-Guanjia-Approval-Cap': 'test-cap', 'Authorization': ''})
check(r.status_code == 409, '/decide 一次性：再批同一条 409')
decision = policy_check('terminal', {'command': 'rm -rf /tmp/x'})
check(decision['outcome'] == 'allow', '批准后原样重查放行（匹配 confirmation todo）')
decision = policy_check('terminal', {'command': 'rm -rf /tmp/OTHER'})
check(decision['outcome'] == 'confirmation_required' and decision.get('todo_id') != todo_1,
      '批准后换参数重试不能蹭放行')
r = post('/decide', {'id': decision['todo_id'], 'decision': 'reject'},
         headers={'X-Guanjia-Approval-Cap': 'test-cap', 'Authorization': ''})
check(r.status_code == 200 and r.json().get('ok') is True, '/decide 拒绝成功')
decision = policy_check('terminal', {'command': 'rm -rf /tmp/OTHER'})
check(decision['outcome'] == 'deny' and 'rejected' in decision['reason'], '拒绝后重查明确驳回')

# ---------- 5. /internal/policy/confirm 一次性凭证 ----------

decision = policy_check('secrets', {'action': 'read'}, session_id='confirm-test')
check(decision['outcome'] == 'confirmation_required', 'L2 secrets 触发确认')
todo_2 = decision['todo_id']
r = post('/internal/policy/confirm', {'todo_id': todo_2, 'decision': 'approve'})
check(r.status_code == 200 and isinstance(r.json().get('credential'), str), 'confirm 批准发一次性凭证')
credential = r.json()['credential']
r = post('/internal/policy/confirm', {'todo_id': todo_2, 'decision': 'approve'})
check(r.status_code == 409, 'confirm 一次性：同一 todo 再批 409')
r = post('/internal/policy/confirm', {'todo_id': 'no-such-todo', 'decision': 'approve'})
check(r.status_code == 404, 'confirm 未知 todo 404')

# ---------- 6. 工具 exec ----------

r = post('/internal/tools/get_weather/exec', {'args': {'city': '上海'}, 'session_id': 'voice-main'})
check(r.status_code == 200 and r.json().get('ok') is True
      and 'city=上海' in r.json()['output']['text'], 'exec L0 工具执行成功（override）')
r = post('/internal/tools/web_fetch/exec', {'args': [], 'session_id': 'voice-main'})
check(r.status_code == 400, 'exec 参数形状不对 400')
r = post('/internal/tools/web_fetch/exec', {'session_id': 'voice-main'})
check(r.status_code == 400, 'exec 缺 args 400')
r = post('/internal/tools/no_such_capability/exec', {'args': {}, 'session_id': 'voice-main'})
check(r.status_code == 403 and r.json()['error']['code'] == 'policy_denied', 'exec 未登记能力 403')
r = post('/internal/tools/terminal/exec', {'args': {'command': 'x'}, 'session_id': 'voice-main'})
check(r.status_code == 403 and r.json()['error']['code'] == 'confirmation_required'
      and r.json()['error'].get('todo_id'), 'exec L2 无凭证 403 并带 todo_id')

# 凭证 exec：一次性（凭证绑定 secrets 能力；secrets 无 Python 实现，但凭证校验先行）
r = post('/internal/tools/secrets/exec', {'args': {'action': 'read'},
                                          'session_id': 'confirm-test', 'credential': credential},
         headers={'x-guanjia-credential': credential})
check(r.status_code == 200, '带一次性凭证 exec 通过校验（工具本身如实报未实现）')
check(r.json().get('ok') is False and r.json()['error']['code'] == 'tool_not_implemented',
      'secrets 在 bridge 无实现，不伪造成功')
r = post('/internal/tools/secrets/exec', {'args': {'action': 'read'},
                                          'session_id': 'confirm-test', 'credential': credential},
         headers={'x-guanjia-credential': credential})
check(r.status_code == 403 and r.json()['error']['code'] == 'invalid_credential',
      '凭证用过即失效：再 exec 403')
r = post('/internal/tools/web_fetch/exec', {'args': {'url': 'https://example.com/c'},
                                            'session_id': 'confirm-test', 'credential': credential},
         headers={'x-guanjia-credential': credential})
check(r.status_code == 403 and r.json()['error']['code'] == 'invalid_credential',
      '凭证不能跨能力使用（绑定 secrets ≠ web_fetch）')
# check 带 credential 只看不烧
decision = policy_check('secrets', {'action': 'read'}, session_id='confirm-test')
check(decision['outcome'] == 'allow', '批准后无凭证重查也放行（todo 已批准）')

# speak 的本地校验（直接调真实实现：长度边界在任何 HTTP 之前）
from guanjia.bridge import tools_impl  # noqa: E402

result = tools_impl.speak({'text': ''})
check(result.get('ok') is False and result['error']['code'] == 'empty', 'speak 空文本拒绝')
result = tools_impl.speak({'text': '念' * 301})
check(result.get('ok') is False and result['error']['code'] == 'too_long', 'speak 超 300 字拒绝')
saved_say_url = tools_impl.SAY_URL
tools_impl.SAY_URL = 'http://127.0.0.1:1/say'  # 死端口：真机上也不出声，连接必被拒
try:
    result = tools_impl.speak({'text': '你好'})
    check(result.get('ok') is False and result['error']['code'] == 'daemon_down',
          'speak 语音守护进程不在时如实报 daemon_down（不真发）')
finally:
    tools_impl.SAY_URL = saved_say_url

# ---------- 7. 审计：JSONL + permission/audit.py 结构化落库 ----------

r = post('/internal/audit', {'event': {'kind': 'tool_execution', 'session_id': 'voice-main',
                                       'capability_id': 'get_weather', 'outcome': 'executed', 'ok': True}})
check(r.status_code == 200 and r.json().get('recorded') == 'jsonl', '/internal/audit 落 JSONL')
audit_lines = (DATA_DIR / 'bridge-audit.jsonl').read_text(encoding='utf-8').splitlines()
check(any('"capability_id": "get_weather"' in line for line in audit_lines), 'JSONL 里有执行事件')
check(any('"error_code": "confirmation_required"' in line for line in audit_lines),
      'JSONL 里有 exec 被拦的审计')

fingerprint = action_fingerprint({'command': 'rm -rf /tmp/x'})
record = APP.permission_audit.confirmation_for(
    session_id='voice-main', actor_id='guanjia-owner', todo_id=todo_1,
    task_id=None, action_fingerprint=fingerprint, now=clock[0],
)
check(record is not None, 'confirmation 经 permission/audit.py 结构化落库（Latch 内可查）')
connection = sqlite3.connect(DATA_DIR / 'permission-audit.sqlite3')
rows = connection.execute('SELECT COUNT(*) FROM permission_audit').fetchone()
connection.close()
check(rows[0] >= 1, 'permission-audit.sqlite3 有审计行')

# ---------- 8. confirm 过期 120s ----------

decision = policy_check('terminal', {'command': 'timeout-case'}, session_id='expire-test')
todo_3 = decision['todo_id']
r = post('/internal/policy/confirm', {'todo_id': todo_3, 'decision': 'approve'})
check(r.status_code == 200, '常量 120s 内 confirm 成功')
advance(121)  # 越过 CONFIRMATION_TIMEOUT_SEC
decision = policy_check('terminal', {'command': 'another-l2'}, session_id='expire-test')
check(decision['outcome'] == 'confirmation_required', '过期后新参数仍可触发确认')
todo_4 = decision['todo_id']
advance(121)
r = post('/internal/policy/confirm', {'todo_id': todo_4, 'decision': 'approve'})
check(r.status_code == 409 and r.json().get('reason') == 'expired', 'confirm 过期 409 expired')
r = get('/latest', headers={'X-Guanjia-Approval-Cap': 'test-cap', 'Authorization': ''})
check(r.json().get('pending') is None, '过期后 /latest 不再吐出 pending')
advance(-121)  # 回到正常时钟附近，继续后面的用例

# ---------- 9. worker 审批：登记 → /latest → /decide → 回调带一次性凭证 ----------

callbacks: list[tuple[str, dict]] = []


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        callbacks.append((self.path, body))
        data = b'{"ok": true}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


callback_server = ThreadingHTTPServer(('127.0.0.1', 0), _CallbackHandler)
threading.Thread(target=callback_server.serve_forever, name='callback-test', daemon=True).start()
callback_base = f"http://127.0.0.1:{callback_server.server_address[1]}"

r = post('/internal/bridge/worker_approval', {
    'task_id': 'task-7', 'capability': 'web_fetch', 'args_summary': '{"url": "https://example.com/w"}',
    'todo_id': 'worker-todo-1', 'callback': callback_base, 'run_id': 'run-9',
})
check(r.status_code == 200 and r.json().get('id') == 'worker-todo-1', 'worker 审批登记成功')
r = get('/latest', headers={'X-Guanjia-Approval-Cap': 'test-cap', 'Authorization': ''})
check((r.json().get('pending') or {}).get('id') == 'worker-todo-1', 'worker 审批进 /latest')
r = post('/decide', {'id': 'worker-todo-1', 'decision': 'approve'},
         headers={'X-Guanjia-Approval-Cap': 'test-cap', 'Authorization': ''})
check(r.status_code == 200 and r.json().get('ok') is True, 'worker 审批 /decide 批准')
check(len(callbacks) == 1 and callbacks[0][0] == '/v1/worker/runs/run-9/resume',
      '决定回调打到 {callback}/v1/worker/runs/{run_id}/resume')
callback_credential = callbacks[0][1].get('credential')
check(bool(callback_credential) and callbacks[0][1].get('decision') == 'approve', '回调带 decision+credential')
r = post('/internal/tools/web_fetch/exec', {'args': {'url': 'https://example.com/w'},
                                            'session_id': 'worker', 'credential': callback_credential},
         headers={'x-guanjia-credential': callback_credential})
check(r.status_code == 200 and r.json().get('ok') is True, 'worker 凭证绑定的能力可 exec')
r = post('/internal/tools/web_fetch/exec', {'args': {'url': 'https://example.com/w'},
                                            'session_id': 'worker', 'credential': callback_credential},
         headers={'x-guanjia-credential': callback_credential})
check(r.status_code == 403, 'worker 凭证同样一次性')

# ---------- 10. approval_ipc.py 的 GUANJIA_APPROVAL_URL 覆盖 ----------

os.environ['GUANJIA_APPROVAL_URL'] = BASE
os.environ['GUANJIA_APPROVAL_CAP'] = 'test-cap'
try:
    decision = policy_check('terminal', {'command': 'ipc-case'}, session_id='ipc-test')
    ipc_todo = decision['todo_id']
    ipc = ApprovalIpc()
    pending = ipc.latest()
    check(pending is not None and pending.get('id') == ipc_todo,
          'ApprovalIpc.latest 走 GUANJIA_APPROVAL_URL 能拿到 pending')
    check(ipc.decide(ipc_todo, 'approve') is True, 'ApprovalIpc.decide 能批准')
    check(ipc.decide(ipc_todo, 'approve') is False, 'ApprovalIpc.decide 一次性语义')
finally:
    os.environ.pop('GUANJIA_APPROVAL_URL', None)
    os.environ.pop('GUANJIA_APPROVAL_CAP', None)

# ---------- 收尾 ----------

CLIENT.close()
SERVER.shutdown()
SERVER.server_close()
callback_server.shutdown()
callback_server.server_close()
APP.store.close()
print('ALL PASS test_bridge_service')
