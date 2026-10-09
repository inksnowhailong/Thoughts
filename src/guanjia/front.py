"""前台大脑：直连硅基流动（OpenAI 兼容），无工具、短 system、短历史，只负责"说"和"派活"。

两段式输出协议（对流式友好）：
    先输出给用户听的口语句子（实时送 TTS）；
    需要动手时，另起一行输出分隔标记 `<<task>>`，后跟一行 JSON：{"task": "...", "reply_when_done": true}
    标记之后的内容不念，只解析成任务交给后台。
- `stream(text)`：同步生成器，只 yield 标记之前的口语文本；结束后 `take_task()` 取出本轮任务（没有则 None）；
- `rephrase(...)`：把后台结果改写成一句口语（同样流式），并把"任务→结果"写回历史；
- `abort()`：从别的线程关闭进行中的流（打断用），不影响后台。
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx

SF_URL = "https://api.siliconflow.cn/v1/chat/completions"
FRONT_MODEL = os.environ.get("GUANJIA_FRONT_MODEL", "deepseek-ai/DeepSeek-V4-Flash")  # 用户要省钱：前后台同一模型
TASK_MARK = "<<task>>"  # 口语与任务的分隔标记
HISTORY_TURNS = 6  # 前台历史只留最近 6 轮（12 条消息），控制 token 与首字延迟
FRONT_SYS_FILE = Path(__file__).resolve().parents[2] / "tmp" / "front_sys.txt"  # 可热改的前台 system，每轮现读
FRONT_SYS = (  # front_sys.txt 缺失或为空时的回退
    "你是家里的语音管家的前台，通过音箱和主人说话。你不能直接操作电脑，动手的事交给后台去做。\n"
    "规则：\n"
    "1. 闲聊、问时间日期、问你是谁、常识问答：直接口语回答，不派任务。\n"
    "2. 凡是要动电脑（开关软件、文件、设置）、查网上信息、设提醒闹钟、记事记账、查记忆：先说一句去做的话，"
    "然后另起一行输出 <<task>>，再下一行输出 JSON：{\"task\":\"给后台的完整明确指令\",\"reply_when_done\":true}\n"
    "3. 只能承诺\"我去做/马上办\"，绝不能说\"已经做好/打开了/设好了\"，结果要等后台回来。\n"
    "4. 每句不超过30字，最多两句，纯口语；不要表情、markdown、列表，不要输出思考过程。\n"
    "5. 用户的话来自语音识别，可能有同音错字，按最合理的意思理解。\n"
    "6. 每句新话都按它本身的意思回答，不要套到上一个话题上；只有主人问进度（弄好了吗、查到没）时才提任务状态。\n"
    "7. 下面「任务状态」里已在办的事，不得再说\"我这就去…/稍等/去办\"，也不得重复派任务；主人没问就完全别提。\n"
    "8. 识别出的话明显不通顺、不成指令、像杂音或旁人聊天时，只回\"没听清，再说一遍？\"，不要派任务。\n"
    "9. 回答进度只看「任务状态」：进行中就说还在办；只有「最近结案」里写着的结果才能说，绝不能自己编天气、数据等结果。\n"
    "示例：\n用户：帮我打开记事本\n助手：好的，我这就去打开记事本。\n<<task>>\n"
    "{\"task\":\"打开记事本\",\"reply_when_done\":true}\n"
    "反例（杂音句，即使刚派过任务也不接上一话题）：\n用户：就是用电干活\n助手：没听清，再说一遍？\n"
    "反例（任务进行中被问进度）：\n用户：天气查到了吗\n助手：还在查，查到马上告诉你。"
)
REPHRASE_SYS = (
    "你是家里的语音管家。把后台执行结果改写成一句对主人说的口语，不超过30字，"
    "如实说成功或失败；若后台在问主人或需要确认，就把要确认的问题原样转问主人，绝不能说成已经在办或办好了。"
    "不要表情、markdown，不要输出思考过程。"
)
_WEEKDAY_CN = "一二三四五六日"
# 待确认任务存在时，对短答做确定性判定（不经模型，快且不会误派新任务）；否定先判，防"不可以"命中"可以"
_NEG_RE = re.compile(r"^(不|别|算了|取消|先不|不用|不要|不行|不了|等等|再说)")
_POS_RE = re.compile(r"^(确认|确定|可以|去吧|好的?|行|对|是的?|嗯+|没问题|办吧|设吧|弄吧|OK|ok)")
CONFIRM_REPLY = "好，这就去办。"
CANCEL_REPLY = "好的，那就先不办了。"
_STATUS_CN = {"done": "完成", "failed": "失败", "awaiting_confirm": "等主人确认", "expired": "确认超时已作废",
              "cancelled": "已取消"}


def load_sf_key() -> str:
    """硅基 key：环境变量 GUANJIA_SF_KEY 优先，否则读 ~/.claude/settings.json 的 env.ANTHROPIC_AUTH_TOKEN。"""
    env = os.environ.get("GUANJIA_SF_KEY")
    if env:
        return env
    cfg = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8-sig"))
    return cfg["env"]["ANTHROPIC_AUTH_TOKEN"]


def _now_text() -> str:
    now = datetime.now()
    return f"当前时间：{now:%Y-%m-%d %H:%M}（星期{_WEEKDAY_CN[now.weekday()]}）。"


def _front_sys(active: list[tuple[int, str, float]], done: list[str], awaiting: tuple[int, str] | None = None) -> str:
    """前台 system：优先 front_sys.txt（热改），末尾由代码注入当前时间与结构化任务状态。

    Args:
        active: 进行中的任务 (编号, 内容, 已耗时秒)。
        done: 最近结案的任务说明（含结果）。
        awaiting: 等主人确认的任务 (编号, 内容)。
    """
    try:
        text = FRONT_SYS_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        text = ""
    lines = [f"#{i} {txt}（已办 {sec:.0f} 秒）" for i, txt, sec in active] or ["无"]
    state = "任务状态——进行中：\n" + "\n".join(lines)
    if awaiting:
        state += f"\n等主人确认：#{awaiting[0]} {awaiting[1]}（主人说确认/可以就办，说不用就取消）"
    if done:
        state += "\n最近结案：\n" + "\n".join(done)
    return (text or FRONT_SYS) + "\n" + _now_text() + "\n" + state


def _parse_task(raw: str) -> dict | None:
    """解析标记之后的 JSON；解析失败时把整段文字当成任务描述。"""
    raw = raw.strip()
    if not raw:
        return None
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e > s:
        try:
            obj = json.loads(raw[s:e + 1])
            if isinstance(obj, dict) and obj.get("task"):
                obj.setdefault("reply_when_done", True)
                return obj
        except json.JSONDecodeError:
            pass
    return {"task": raw, "reply_when_done": True}


class Front:
    """前台客户端：流式直连硅基，维护最近几轮对话历史。"""

    def __init__(self, timeout: float = 30.0) -> None:
        self.key = load_sf_key()
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False)
        self._resp: httpx.Response | None = None  # 进行中的流，供 abort() 从别的线程关闭
        self._hist: list[dict] = []  # 前台对话历史（user/assistant 交替，另含任务结果备注）
        self._hlock = threading.Lock()  # 后台回调线程也会写历史
        self._task: dict | None = None  # 本轮解析出的任务，take_task() 取走
        self._done: list[str] = []  # 最近结案的任务说明（只进 system，不进对话历史，防止诱导复读）

    def _raw_stream(self, messages: list[dict], max_tokens: int = 300) -> Iterator[str]:
        """逐段 yield 模型正文增量（忽略 reasoning_content）。"""
        body = {"model": FRONT_MODEL, "messages": messages, "stream": True,
                "enable_thinking": False, "max_tokens": max_tokens, "temperature": 0.6}
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        with self._client.stream("POST", SF_URL, headers=headers, json=body) as r:
            self._resp = r
            try:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or [{}]
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield delta
            finally:
                self._resp = None

    def _history(self) -> list[dict]:
        with self._hlock:
            return list(self._hist[-HISTORY_TURNS * 2:])

    def _remember(self, *msgs: dict) -> None:
        with self._hlock:
            self._hist.extend(msgs)
            self._hist = self._hist[-(HISTORY_TURNS * 2 + 4):]

    def stream(self, text: str, active: list[tuple[int, str, float]] | None = None,
               awaiting: tuple[int, str] | None = None) -> Iterator[str]:
        """一轮对话：只 yield 分隔标记之前的口语；标记之后解析成任务，结束后由 take_task() 取。

        active 为后台进行中的任务列表，awaiting 为等确认的任务，都注入 system 的「任务状态」。
        有待确认任务且用户短答肯定/否定时不经模型：固定回一句，take_task() 给出 {"confirm"/"cancel": 编号}。
        """
        self._task = None
        short = re.sub(r"[\s，。！？,.!?、~～]", "", text)
        if awaiting and len(short) <= 8 and (_NEG_RE.match(short) or _POS_RE.match(short)):
            neg = bool(_NEG_RE.match(short))
            reply = CANCEL_REPLY if neg else CONFIRM_REPLY
            self._task = {"cancel" if neg else "confirm": awaiting[0]}
            if neg:
                self.note_done(awaiting[0], awaiting[1], "cancelled", "主人说不用了")
            self._remember({"role": "user", "content": text}, {"role": "assistant", "content": reply})
            yield reply
            return
        with self._hlock:
            done = list(self._done)
        msgs = ([{"role": "system", "content": _front_sys(active or [], done, awaiting)}] + self._history()
                + [{"role": "user", "content": text}])
        buf, spoken, task_raw, in_task, completed = "", [], "", False, False
        try:
            for piece in self._raw_stream(msgs):
                if in_task:
                    task_raw += piece
                    continue
                buf += piece
                if TASK_MARK in buf:
                    pre, task_raw = buf.split(TASK_MARK, 1)
                    in_task, buf = True, ""
                    if pre.strip():
                        spoken.append(pre)
                        yield pre
                    continue
                # 末尾可能是半个标记（如 "<<ta"），先扣住不念
                hold = 0
                for k in range(min(len(TASK_MARK) - 1, len(buf)), 0, -1):
                    if TASK_MARK.startswith(buf[-k:]):
                        hold = k
                        break
                out, buf = buf[:len(buf) - hold], buf[len(buf) - hold:]
                if out:
                    spoken.append(out)
                    yield out
            if buf:
                spoken.append(buf)
                yield buf
            completed = True
        finally:
            said = "".join(spoken).strip()
            self._task = _parse_task(task_raw) if (completed and in_task) else None
            # 历史里只留说出口的话；派了什么任务由 system 的「任务状态」表达
            self._remember({"role": "user", "content": text}, {"role": "assistant", "content": said or "（被打断）"})

    def take_task(self) -> dict | None:
        t, self._task = self._task, None
        return t

    def rephrase(self, task_id: int, task: str, status: str, result: str) -> Iterator[str]:
        """把后台结果改写成一句口语（流式）；先把"任务→结果"记进最近结案，被打断也能对上"弄好了吗"。

        status 为后台任务状态（done / failed / awaiting_confirm / expired）。
        """
        self.note_done(task_id, task, status, result)
        msgs = [{"role": "system", "content": REPHRASE_SYS + "\n" + _now_text()},
                {"role": "user", "content": f"任务：{task}\n状态：{_STATUS_CN.get(status, status)}\n后台结果：{result[:1500]}"}]
        yield from self._raw_stream(msgs, max_tokens=120)

    def note_done(self, task_id: int, task: str, status: str, result: str) -> None:
        """记一条结案任务（最多留 3 条），供 system 的「最近结案」使用。"""
        with self._hlock:
            self._done = (self._done + [f"#{task_id} {task}：{_STATUS_CN.get(status, status)}，{result[:200]}"])[-3:]

    def abort(self) -> None:
        """从其他线程关闭当前前台流（打断用）。"""
        r = self._resp
        if r is not None:
            r.close()

    def close(self) -> None:
        self._client.close()
