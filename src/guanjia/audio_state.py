"""列出系统所有可激活的音频端点（录音/播放）的静音状态与主音量，定位"麦克风录到全零"是不是系统级静音或音量 0。"""

import json

from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

rows = []
for dev in AudioUtilities.GetAllDevices():
    name = getattr(dev, "FriendlyName", None) or "?"
    try:
        ep = dev._dev.Activate(IAudioEndpointVolume._iid_, 23, None)
        vol = ep.QueryInterface(IAudioEndpointVolume)
        rows.append({"name": name, "state": getattr(dev, "state", None) and str(dev.state),
                     "muted": bool(vol.GetMute()), "volume": round(vol.GetMasterVolumeLevelScalar(), 2)})
    except Exception as e:  # noqa: BLE001
        rows.append({"name": name, "state": getattr(dev, "state", None) and str(dev.state), "error": str(e)[:50]})

mic = AudioUtilities.GetMicrophone()
spk = AudioUtilities.GetSpeakers()
print("default mic:", getattr(mic, "FriendlyName", None))
print("default speakers:", getattr(spk, "FriendlyName", None))
print(json.dumps([r for r in rows if "EDIFIER" in r["name"] or "USB2.0" in r["name"]], ensure_ascii=False, indent=1))
