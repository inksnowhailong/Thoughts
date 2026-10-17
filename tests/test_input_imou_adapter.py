import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from guanjia.input.adapters.imou import (  # noqa: E402
    ImouApiError,
    ImouCredentials,
    ImouReadOnlyClient,
    compute_imou_sign,
    imou_signing_password,
    imou_signing_source,
    redact_imou_error_detail,
)


def check(value, message):
    if not value:
        raise AssertionError(message)


OFFICIAL_TIME = 1706511734
OFFICIAL_NONCE = 'f5a1ae2d-c09c-4d39-a744-83a5c2c653c2'
OFFICIAL_SECRET = 'test123456789test123456789'
OFFICIAL_SIGN = 'xjhCQBoJ9hRDsCjyDcHjtDNzRZ3ZJezcawsfWeiaoxU='
OFFICIAL_PASSWORD = '1f2da89b2fab404859f02bcc80e8521bc7a63af3e5b1da5318f7cf7c56836b09'

source = imou_signing_source(unix_time=OFFICIAL_TIME, nonce=OFFICIAL_NONCE, app_secret=OFFICIAL_SECRET)
check(
    source
    == 'time:1706511734,nonce:f5a1ae2d-c09c-4d39-a744-83a5c2c653c2,appSecret:test123456789test123456789',
    'official signing source string',
)
check(imou_signing_password(OFFICIAL_SECRET) == OFFICIAL_PASSWORD, 'official password derivation')
check(
    compute_imou_sign(unix_time=OFFICIAL_TIME, nonce=OFFICIAL_NONCE, app_secret=OFFICIAL_SECRET) == OFFICIAL_SIGN,
    'official sign vector',
)

creds = ImouCredentials('lcd-app-id', OFFICIAL_SECRET)
check(OFFICIAL_SECRET not in repr(creds), 'app_secret absent from repr')
check(OFFICIAL_SECRET not in str(creds), 'app_secret absent from str')

captured = []


class _Provider:
    def imou_credentials(self):
        return creds


class _Transport:
    def post(self, method, envelope, timeout):
        captured.append({'method': method, 'envelope': envelope, 'timeout': timeout})
        return {
            'result': {'code': '0', 'msg': 'ok', 'data': {'deviceList': []}},
            'id': envelope['id'],
        }


client = ImouReadOnlyClient(
    _Transport(),
    _Provider(),
    clock=lambda: OFFICIAL_TIME,
    nonce_factory=lambda: OFFICIAL_NONCE,
    request_id_factory=lambda: 'req-1',
)
page = client.device_list('At_access_token_example')
check(page.devices == (), 'device_list maps success payload')
check(len(captured) == 1, 'one transport call')
call = captured[0]
check(call['method'] == 'listDeviceDetailsByPage', 'device_list uses read-only list API')
serialized = json.dumps(call['envelope'], ensure_ascii=False)
check(OFFICIAL_SECRET not in serialized, 'request body never embeds app_secret')
check('controlPTZ' not in serialized and call['method'] not in {'controlPTZ', 'rotate', 'tilt'}, 'request has no PTZ')
for name in ('controlPTZ', 'rotate', 'tilt'):
    check(not hasattr(client, name), f'client has no {name} method')

try:
    failing = ImouReadOnlyClient(
        type(
            'FailTransport',
            (),
            {
                'post': lambda self, method, envelope, timeout: {
                    'result': {
                        'code': 'SN1001',
                        'msg': f'bad token=At_leak&digest=abc&appSecret:{OFFICIAL_SECRET}',
                        'data': {'flv': 'https://example.test/vod/x.flv?digest=secretbits'},
                    },
                    'id': 'x',
                }
            },
        )(),
        _Provider(),
    )
    failing.device_list('At_access_token_example')
except ImouApiError as error:
    check(error.code == 'SN1001', 'errors expose code only')
    check(str(error) == 'SN1001', 'exception message is code only')
    check(OFFICIAL_SECRET not in str(error), 'errors omit app secret')
else:
    raise AssertionError('non-zero code must raise ImouApiError')

redacted = redact_imou_error_detail(
    f'token=At_leak&digest=abc appSecret:{OFFICIAL_SECRET} https://x.test/a?digest=zzz',
    app_secret=OFFICIAL_SECRET,
)
check(OFFICIAL_SECRET not in redacted, 'redaction removes app secret')
check('digest=<redacted>' in redacted or 'digest=' not in redacted.split('https://')[0], 'redaction strips token query')

print('ALL PASS test_input_imou_adapter')
