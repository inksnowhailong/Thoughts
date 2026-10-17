"""Read-only Imou (乐橙云) input adapter: signed OpenAPI calls, no PTZ control or media download."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from guanjia.contracts import FrozenJsonValue, JsonValue

from .contracts import freeze_json, frozen_mapping

# Official OpenAPI method names (read-only subset).
_METHOD_DEVICE_LIST = 'listDeviceDetailsByPage'
_METHOD_DEVICE_DETAILS = 'listDeviceDetailsByIds'
_METHOD_DEVICE_ONLINE = 'deviceOnline'
_METHOD_CREATE_FLV = 'createDeviceFlvLive'
_METHOD_QUERY_FLV = 'queryDeviceFlvLive'
_METHOD_ALARM_MESSAGES = 'getAlarmMessage'
_METHOD_CLOUD_RECORD = 'queryCloudRecordByToken'

_FORBIDDEN_CONTROL_METHODS = frozenset({'controlPTZ', 'rotate', 'tilt'})

_SENSITIVE_PARAM_KEYS = frozenset(
    {
        'token',
        'accessToken',
        'appSecret',
        'cloudToken',
        'playToken',
        'devicePassword',
        'password',
        'salt',
    },
)


def imou_signing_password(app_secret: str) -> str:
    """Derive the HMAC key: lowercase hex SHA-256 of appSecret."""
    if not isinstance(app_secret, str) or not app_secret:
        raise ValueError('app_secret must be a non-empty string')
    return hashlib.sha256(app_secret.encode('utf-8')).hexdigest()


def imou_signing_source(*, unix_time: int, nonce: str, app_secret: str) -> str:
    """Build the official signing source string."""
    return f'time:{unix_time},nonce:{nonce},appSecret:{app_secret}'


def compute_imou_sign(*, unix_time: int, nonce: str, app_secret: str) -> str:
    """Official sign = Base64(HMAC-SHA256(source, password))."""
    source = imou_signing_source(unix_time=unix_time, nonce=nonce, app_secret=app_secret)
    password = imou_signing_password(app_secret)
    digest = hmac.new(password.encode('utf-8'), source.encode('utf-8'), hashlib.sha256).digest()
    return base64.b64encode(digest).decode('ascii')


class ImouCredentials:
    """App credentials injected at runtime; secret never appears in repr or logs."""

    __slots__ = ('app_id', '_app_secret')

    def __init__(self, app_id: str, app_secret: str) -> None:
        if not isinstance(app_id, str) or not app_id.strip():
            raise ValueError('app_id must be a non-empty string')
        if not isinstance(app_secret, str) or not app_secret:
            raise ValueError('app_secret must be a non-empty string')
        self.app_id = app_id.strip()
        self._app_secret = app_secret

    @property
    def app_secret(self) -> str:
        return self._app_secret

    def __repr__(self) -> str:
        return f'ImouCredentials(app_id={self.app_id!r}, app_secret=<redacted>)'

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ImouCredentials):
            return NotImplemented
        return self.app_id == other.app_id and self.app_secret == other.app_secret


class ImouCredentialsProvider(Protocol):
    """Supply ImouCredentials; implementations must not embed hard-coded secrets."""

    def imou_credentials(self) -> ImouCredentials:
        """Return current app credentials."""


class ImouTransport(Protocol):
    """POST one signed OpenAPI envelope to /openapi/[method]."""

    def post(self, method: str, envelope: Mapping[str, JsonValue], timeout: float) -> Mapping[str, JsonValue]:
        """Execute the HTTP call and return the parsed JSON body."""


class ImouApiError(Exception):
    """API failure exposing only the platform result code."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError('code must be a non-empty string')
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ImouChannelView:
    """One channel snapshot: status and capability only."""

    channel_id: str
    online: bool | None
    ability: str
    name: str
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'metadata', frozen_mapping(self.metadata))


@dataclass(frozen=True)
class ImouDeviceView:
    """Device status, capability, and channel snapshots."""

    device_id: str
    status: str
    ability: str
    name: str
    channels: tuple[ImouChannelView, ...]
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'channels', tuple(self.channels))
        object.__setattr__(self, 'metadata', frozen_mapping(self.metadata))


@dataclass(frozen=True)
class ImouDeviceListPage:
    """One page from listDeviceDetailsByPage."""

    devices: tuple[ImouDeviceView, ...]
    page: int
    page_size: int


@dataclass(frozen=True)
class ImouStreamUrlRef:
    """Reference to an FLV live stream URL (not downloaded by this adapter)."""

    device_id: str
    channel_id: str
    profile: str
    url: str


@dataclass(frozen=True)
class ImouAlarmMessageView:
    """Alarm message with optional media URL reference."""

    alarm_id: str
    device_id: str
    channel_id: str
    alarm_type: int
    cloud_record_token: str
    local_time: str
    media_url_ref: str | None
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'metadata', frozen_mapping(self.metadata))


@dataclass(frozen=True)
class ImouAlarmMessagePage:
    alarms: tuple[ImouAlarmMessageView, ...]
    next_alarm_id: str
    count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, 'alarms', tuple(self.alarms))


@dataclass(frozen=True)
class ImouCloudRecordView:
    """Cloud recording segment reference (thumb / path URLs are references only)."""

    record_id: str
    device_id: str
    local_begin_time: str
    local_end_time: str
    thumb_url_ref: str
    record_path_ref: str
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'metadata', frozen_mapping(self.metadata))


Clock = Callable[[], int]
NonceFactory = Callable[[], str]
RequestIdFactory = Callable[[], str]


def build_imou_envelope(
    credentials: ImouCredentials,
    params: Mapping[str, JsonValue],
    *,
    unix_time: int,
    nonce: str,
    request_id: str,
) -> dict[str, JsonValue]:
    """Assemble the standard HTTPS body with a fresh signature."""
    sign = compute_imou_sign(unix_time=unix_time, nonce=nonce, app_secret=credentials.app_secret)
    return {
        'system': {
            'ver': '1.0',
            'appId': credentials.app_id,
            'sign': sign,
            'time': unix_time,
            'nonce': nonce,
        },
        'params': dict(params),
        'id': request_id,
    }


class ImouReadOnlyClient:
    """Signed read-only Imou OpenAPI client. Cloud PTZ is intentionally out of scope."""

    def __init__(
        self,
        transport: ImouTransport,
        credentials: ImouCredentialsProvider,
        *,
        clock: Clock | None = None,
        nonce_factory: NonceFactory | None = None,
        request_id_factory: RequestIdFactory | None = None,
        default_timeout: float = 30.0,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._clock = clock or (lambda: 0)
        self._nonce_factory = nonce_factory or (lambda: '0' * 32)
        self._request_id_factory = request_id_factory or (lambda: 'request-id')
        if not isinstance(default_timeout, (int, float)) or default_timeout <= 0:
            raise ValueError('default_timeout must be a positive number')
        self._default_timeout = float(default_timeout)

    def device_list(
        self,
        access_token: str,
        *,
        page: int = 1,
        page_size: int = 50,
        source: str = 'bindAndShare',
        timeout: float | None = None,
    ) -> ImouDeviceListPage:
        data = self._invoke(
            _METHOD_DEVICE_LIST,
            {
                'token': access_token,
                'page': page,
                'pageSize': page_size,
                'source': source,
            },
            timeout,
        )
        devices_raw = data.get('deviceList') or data.get('devices') or []
        if not isinstance(devices_raw, list):
            devices_raw = []
        devices = tuple(_device_view(item) for item in devices_raw if isinstance(item, Mapping))
        return ImouDeviceListPage(devices, page, page_size)

    def device_status(
        self,
        access_token: str,
        *,
        device_id: str | None = None,
        device_ids: tuple[str, ...] = (),
        timeout: float | None = None,
    ) -> tuple[ImouDeviceView, ...]:
        """Online status for one device, or detailed status for many ids."""
        ids = _normalize_device_ids(device_id, device_ids)
        if len(ids) == 1:
            data = self._invoke(
                _METHOD_DEVICE_ONLINE,
                {'token': access_token, 'deviceId': ids[0]},
                timeout,
            )
            return (_device_view_from_online(data),)
        device_list = [{'deviceId': item} for item in ids]
        data = self._invoke(
            _METHOD_DEVICE_DETAILS,
            {'token': access_token, 'deviceList': device_list},
            timeout,
        )
        devices_raw = data.get('deviceList') or []
        if not isinstance(devices_raw, list):
            return ()
        return tuple(_device_view(item) for item in devices_raw if isinstance(item, Mapping))

    def create_flv_realtime(
        self,
        access_token: str,
        *,
        device_id: str,
        channel_id: str,
        timeout: float | None = None,
    ) -> tuple[ImouStreamUrlRef, ...]:
        data = self._invoke(
            _METHOD_CREATE_FLV,
            {
                'token': access_token,
                'deviceId': device_id,
                'channelId': channel_id,
                'type': 'realTime',
            },
            timeout,
        )
        return _flv_refs(device_id, channel_id, data)

    def query_flv_realtime(
        self,
        access_token: str,
        *,
        device_id: str,
        channel_id: str,
        timeout: float | None = None,
    ) -> tuple[ImouStreamUrlRef, ...]:
        data = self._invoke(
            _METHOD_QUERY_FLV,
            {'token': access_token, 'deviceId': device_id, 'channelId': channel_id},
            timeout,
        )
        return _flv_refs(device_id, channel_id, data)

    def get_alarm_messages(
        self,
        access_token: str,
        *,
        device_id: str,
        channel_id: str,
        begin_time: str,
        end_time: str,
        count: int = 30,
        next_alarm_id: str = '-1',
        timeout: float | None = None,
    ) -> ImouAlarmMessagePage:
        data = self._invoke(
            _METHOD_ALARM_MESSAGES,
            {
                'token': access_token,
                'deviceId': device_id,
                'channelId': channel_id,
                'beginTime': begin_time,
                'endTime': end_time,
                'count': count,
                'nextAlarmId': next_alarm_id,
            },
            timeout,
        )
        alarms_raw = data.get('alarms') or []
        if not isinstance(alarms_raw, list):
            alarms_raw = []
        alarms = tuple(_alarm_view(item) for item in alarms_raw if isinstance(item, Mapping))
        next_id = str(data.get('nextAlarmId', '-1'))
        total = int(data.get('count', len(alarms))) if isinstance(data.get('count'), (int, str)) else len(alarms)
        return ImouAlarmMessagePage(alarms, next_id, total)

    def query_cloud_record_by_token(
        self,
        access_token: str,
        *,
        device_id: str,
        cloud_token: str,
        timeout: float | None = None,
    ) -> ImouCloudRecordView:
        data = self._invoke(
            _METHOD_CLOUD_RECORD,
            {'token': access_token, 'deviceId': device_id, 'cloudToken': cloud_token},
            timeout,
        )
        return ImouCloudRecordView(
            str(data.get('recordId', '')),
            device_id,
            str(data.get('localBeginTime', '')),
            str(data.get('localEndTime', '')),
            str(data.get('thumbUrl', '')),
            str(data.get('recordPath', '')),
            _safe_metadata(data),
        )

    def _invoke(self, method: str, params: Mapping[str, JsonValue], timeout: float | None) -> Mapping[str, JsonValue]:
        if method in _FORBIDDEN_CONTROL_METHODS:
            raise ValueError(f'forbidden control method: {method}')
        credentials = self._credentials.imou_credentials()
        envelope = build_imou_envelope(
            credentials,
            params,
            unix_time=self._clock(),
            nonce=self._nonce_factory(),
            request_id=self._request_id_factory(),
        )
        _assert_envelope_safe(envelope, credentials.app_secret)
        body = self._transport.post(method, envelope, timeout if timeout is not None else self._default_timeout)
        return _unwrap_result(body)

    @staticmethod
    def forbidden_control_methods() -> frozenset[str]:
        return _FORBIDDEN_CONTROL_METHODS


def _normalize_device_ids(device_id: str | None, device_ids: tuple[str, ...]) -> tuple[str, ...]:
    ids: list[str] = []
    if device_id is not None and device_id.strip():
        ids.append(device_id.strip())
    for item in device_ids:
        if isinstance(item, str) and item.strip():
            ids.append(item.strip())
    if not ids:
        raise ValueError('device_id or device_ids is required')
    return tuple(ids)


def _unwrap_result(body: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    if not isinstance(body, Mapping):
        raise ImouApiError('INVALID_RESPONSE')
    result = body.get('result')
    if not isinstance(result, Mapping):
        raise ImouApiError('INVALID_RESPONSE')
    code = result.get('code')
    if code is None:
        raise ImouApiError('INVALID_RESPONSE')
    code_text = str(code)
    if code_text != '0':
        raise ImouApiError(code_text)
    data = result.get('data')
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ImouApiError('INVALID_RESPONSE')
    return data


def _assert_envelope_safe(envelope: Mapping[str, JsonValue], app_secret: str) -> None:
    serialized = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
    if app_secret in serialized:
        raise ValueError('envelope must not serialize app_secret')
    method_blob = serialized.lower()
    for forbidden in _FORBIDDEN_CONTROL_METHODS:
        if forbidden.lower() in method_blob:
            raise ValueError(f'envelope must not reference {forbidden}')


def _device_view(raw: Mapping[str, JsonValue]) -> ImouDeviceView:
    channels_raw = raw.get('channels') or raw.get('channelList') or []
    channels: list[ImouChannelView] = []
    if isinstance(channels_raw, list):
        for item in channels_raw:
            if isinstance(item, Mapping):
                channels.append(_channel_view(item))
    status = str(raw.get('deviceStatus') or raw.get('status') or raw.get('onLine') or 'unknown')
    return ImouDeviceView(
        str(raw.get('deviceId', '')),
        status,
        str(raw.get('ability') or raw.get('deviceAbility') or ''),
        str(raw.get('name') or raw.get('deviceName') or ''),
        tuple(channels),
        _safe_metadata(raw),
    )


def _device_view_from_online(raw: Mapping[str, JsonValue]) -> ImouDeviceView:
    channels_raw = raw.get('channels') or []
    channels: list[ImouChannelView] = []
    if isinstance(channels_raw, list):
        for item in channels_raw:
            if isinstance(item, Mapping):
                online = _online_flag(item.get('onLine'))
                channels.append(
                    ImouChannelView(
                        str(item.get('channelId', '')),
                        online,
                        '',
                        '',
                        {},
                    ),
                )
    return ImouDeviceView(
        str(raw.get('deviceId', '')),
        str(raw.get('onLine', 'unknown')),
        '',
        '',
        tuple(channels),
        _safe_metadata(raw),
    )


def _channel_view(raw: Mapping[str, JsonValue]) -> ImouChannelView:
    online = _online_flag(raw.get('channelOnline') or raw.get('onLine') or raw.get('online'))
    return ImouChannelView(
        str(raw.get('channelId', '')),
        online,
        str(raw.get('channelAbility') or raw.get('ability') or ''),
        str(raw.get('channelName') or raw.get('name') or ''),
        _safe_metadata(raw),
    )


def _online_flag(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {'1', 'true', 'online'}:
        return True
    if text in {'0', 'false', 'offline'}:
        return False
    return None


def _flv_refs(device_id: str, channel_id: str, data: Mapping[str, JsonValue]) -> tuple[ImouStreamUrlRef, ...]:
    refs: list[ImouStreamUrlRef] = []
    flv = data.get('flv')
    if isinstance(flv, str) and flv:
        refs.append(ImouStreamUrlRef(device_id, channel_id, 'sd', flv))
    flv_hd = data.get('flvHD')
    if isinstance(flv_hd, str) and flv_hd:
        refs.append(ImouStreamUrlRef(device_id, channel_id, 'hd', flv_hd))
    return tuple(refs)


def _alarm_view(raw: Mapping[str, JsonValue]) -> ImouAlarmMessageView:
    media = raw.get('picUrl') or raw.get('thumbUrl')
    media_ref = str(media) if isinstance(media, str) and media else None
    alarm_type = raw.get('type')
    type_value = int(alarm_type) if isinstance(alarm_type, (int, str)) and str(alarm_type).isdigit() else -1
    return ImouAlarmMessageView(
        str(raw.get('alarmId', '')),
        str(raw.get('deviceId', '')),
        str(raw.get('channelId', '')),
        type_value,
        str(raw.get('token', '')),
        str(raw.get('localDate', '')),
        media_ref,
        _safe_metadata(raw),
    )


def _safe_metadata(raw: Mapping[str, JsonValue]) -> Mapping[str, FrozenJsonValue]:
    cleaned: dict[str, JsonValue] = {}
    for key, value in raw.items():
        if key in _SENSITIVE_PARAM_KEYS:
            continue
        if isinstance(value, str) and _looks_sensitive_url(value):
            cleaned[key] = _strip_url_query(value)
            continue
        try:
            cleaned[key] = value
        except TypeError:
            continue
    return frozen_mapping(cleaned)


def _looks_sensitive_url(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith('http://') or lowered.startswith('https://')


def _strip_url_query(url: str) -> str:
    parts = urlsplit(url)
    return parts._replace(query='', fragment='').geturl()


def redact_imou_error_detail(text: str, *, app_secret: str = '') -> str:
    """Strip secrets, tokens, and URL query strings from error surfaces."""
    if not text:
        return ''
    redacted = text
    if app_secret:
        redacted = redacted.replace(app_secret, '<redacted>')
    redacted = re.sub(r'(token|accessToken|cloudToken|appSecret|password|digest)=[^&\s"]+', r'\1=<redacted>', redacted, flags=re.I)
    redacted = re.sub(r'appSecret:[^,\s}"]+', 'appSecret:<redacted>', redacted, flags=re.I)
    return redacted
