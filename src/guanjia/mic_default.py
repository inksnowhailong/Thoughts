"""查系统默认录音设备（控制台/通信两种角色）及所有活动录音端点的音量，定位"别人听我声音小"是否因为默认设备指错或电平低。"""

import json
from ctypes import POINTER, cast

from comtypes import CLSCTX_ALL, CoCreateInstance
from pycaw.pycaw import IMMDeviceEnumerator, IAudioEndpointVolume, AudioUtilities
from pycaw.constants import CLSID_MMDeviceEnumerator, EDataFlow, ERole

enum = CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL)


def name_of(dev):
    return AudioUtilities.CreateDevice(dev).FriendlyName


out = {"default": {}, "capture_endpoints": []}
for role, label in ((ERole.eConsole.value, "console(游戏/普通应用)"), (ERole.eCommunications.value, "communications(语音通话)")):
    try:
        d = enum.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, role)
        out["default"][label] = name_of(d)
    except Exception as e:  # noqa: BLE001
        out["default"][label] = f"ERR {str(e)[:40]}"

coll = enum.EnumAudioEndpoints(EDataFlow.eCapture.value, 1)  # 1 = DEVICE_STATE_ACTIVE
for i in range(coll.GetCount()):
    d = coll.Item(i)
    try:
        ep = d.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(ep, POINTER(IAudioEndpointVolume))
        lo, hi, step = vol.GetVolumeRange()
        out["capture_endpoints"].append({
            "name": name_of(d), "muted": bool(vol.GetMute()),
            "level_pct": round(vol.GetMasterVolumeLevelScalar() * 100),
            "level_db": round(vol.GetMasterVolumeLevel(), 1),
            "range_db": [round(lo, 1), round(hi, 1)],
        })
    except Exception as e:  # noqa: BLE001
        out["capture_endpoints"].append({"name": name_of(d), "error": str(e)[:60]})
print(json.dumps(out, ensure_ascii=False, indent=1))
