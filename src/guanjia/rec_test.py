"""麦克风探针：逐个尝试音箱条的各个输入设备（MME/DirectSound/WASAPI/WDM-KS），各录 1.5 秒，打印峰值与 RMS。

用于定位"录到全零"是哪一层的问题：设备接口、声道数、还是系统级静音/隐私拦截。
"""

import json

import numpy as np
import sounddevice as sd

results = []
for idx, d in enumerate(sd.query_devices()):
    if d["max_input_channels"] <= 0 or "EDIFIER" not in d["name"]:
        continue
    api = sd.query_hostapis(d["hostapi"])["name"]
    for ch in (1, min(2, d["max_input_channels"])):
        try:
            sr = int(d["default_samplerate"])
            rec = sd.rec(int(sr * 1.5), samplerate=sr, channels=ch, dtype="float32", device=idx)
            sd.wait()
            peak = float(np.max(np.abs(rec)))
            rms = float(np.sqrt(np.mean(rec ** 2)))
            results.append({"idx": idx, "api": api, "ch": ch, "sr": sr, "peak": round(peak, 5), "rms": round(rms, 6)})
        except Exception as e:  # noqa: BLE001
            results.append({"idx": idx, "api": api, "ch": ch, "error": str(e)[:80]})
print(json.dumps(results, ensure_ascii=False, indent=1))
