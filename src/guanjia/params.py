"""参数登记表：面板「节点抽屉」里的参数表。

- 设置项（Settings 的全部字段）：可在面板改，立即生效；
- 只读常量（代码常量 / 环境变量 / 命令行参数）：由 VoiceDaemon 构造时传进来，改了要重启语音。
params 不 import voice，避免反向依赖。
"""

from __future__ import annotations

from guanjia.settings import Settings, to_dict

# (key, 中文名, 节点, 单位, 说明)
SETTING_PARAMS = (
    ("wake_words", "唤醒词", "wake", "", "多个用逗号隔开，按读音容错匹配"),
    ("wake_required", "必须喊唤醒词", "wake", "", "关掉就是听到就答"),
    ("follow_up_sec", "免喊秒数", "wake", "秒", "它回完最后一句后这么久内接着说不用再喊"),
    ("ack_text", "唤醒应答", "wake", "", "只喊唤醒词时的应答"),
    ("min_chars", "最少字数", "asr", "字", "更短的片段当碎音忽略（免喊窗口里「好/行/对/要/是/不/停」例外）"),
    ("spk_threshold", "声纹门槛", "speaker", "", "软门禁后只管免喊窗口，其余只作身份标签"),
    ("barge_rms_min", "打断线下限", "barge", "RMS", "它说话时你开口的音量超过这个就停"),
    ("earcon", "提示音", "play", "", "说完话立刻放提示音"),
)

# (key, 中文名, 节点, 单位, 来源)；值由 VoiceDaemon 传入 {key: (当前值, 默认值)}
CONST_PARAMS = (
    ("MIC_GAIN", "麦克风增益", "mic", "倍", "voice.py MIC_GAIN；命令行 --mic-gain"),
    ("AEC_ENABLED", "回声消除 AEC", "mic", "", "环境变量 GUANJIA_AEC；命令行 --aec"),
    ("VAD_THRESHOLD", "VAD 人声门限", "vad", "", "vad.py VAD(threshold=…)"),
    ("VAD_MIN_SILENCE", "VAD 最短静音", "vad", "秒", "vad.py VAD(min_silence=…)，一句话中间停顿短于它不切段"),
    ("VAD_MIN_SPEECH", "VAD 最短语音", "vad", "秒", "vad.py VAD(min_speech=…)"),
    ("MIN_UTT_SEC", "最短有效话段", "vad", "秒", "voice.py MIN_UTT_SEC，更短的段当碎音丢弃"),
    ("FIRST_SENT_MAX_CHARS", "首句字数硬上限", "tts", "字", "voice.py FIRST_SENT_MAX_CHARS"),
    ("FIRST_SENT_SOFT_MIN", "首句软下限", "tts", "字", "voice.py FIRST_SENT_SOFT_MIN，首句在断句处至少这么多字才送合成"),
    ("BARGE_MS", "打断累计时长", "barge", "毫秒", "voice.py BARGE_MS"),
    ("BARGE_GUARD_MS", "打断保护期", "barge", "毫秒", "voice.py BARGE_GUARD_MS，每次开始说话后这段时间只学回声"),
)


def describe(current: Settings, consts: dict[str, tuple] | None = None) -> list[dict]:
    """每项：key,label,node,unit,default,current,overridden,effect,editable,source。"""
    cur, default = to_dict(current), to_dict(Settings())
    out = [{"key": k, "label": label, "node": node, "unit": unit, "default": default[k], "current": cur[k],
            "overridden": cur[k] != default[k], "effect": "立即", "editable": True, "source": f"设置项 {k}（{note}）"}
           for k, label, node, unit, note in SETTING_PARAMS]
    for k, label, node, unit, source in CONST_PARAMS:
        if consts is None or k not in consts:
            continue
        value, dflt = consts[k]
        out.append({"key": k, "label": label, "node": node, "unit": unit, "default": dflt, "current": value,
                    "overridden": value != dflt, "effect": "需重启语音", "editable": False, "source": source})
    return out
