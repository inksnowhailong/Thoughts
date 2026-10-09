"""后台任务队列：前台派来的任务在独立线程里交给 OpenClaw 网关（工具/记忆/守卫都在网关）执行。

- 每个任务有自增编号与状态：pending → running → done / failed，
  或 running → awaiting_confirm（网关守卫要主人确认）→ 确认后同会话续跑 / cancelled / expired；
- 每个任务一个线程、一条独立网关会话（user 字段即会话键），确认时用同一个 user 只发"确认"续上原任务；
- 每次执行有总超时 TASK_TIMEOUT_SEC，待确认超过 CONFIRM_TTL_SEC 自动作废（守卫的待确认只在内存存 3 分钟）；
- 网关连不上（如正在重启）按 CONNECT_RETRY_SEC 间隔重试，最多约 20 秒；
- 结果通过 on_done(task) 回调交还；用自己的 Brain 实例，前台打断不会波及后台。
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from guanjia.brain import Brain

log = logging.getLogger("guanjia.backoffice")

BACK_MODEL = os.environ.get("GUANJIA_BACK_MODEL", "siliconflow/deepseek-ai/DeepSeek-V4-Flash")
BACK_USER = "voice-backoffice"  # 会话键前缀：每个任务一条独立会话
BACK_SYS = ("你是语音管家的后台执行者，收到的是主人通过语音派来的任务。直接用工具把事做完，不要反问；"
            "启动记事本等图形界面程序必须用不等待退出的方式（如 PowerShell Start-Process），不要阻塞等它关闭。"
            "做完用一两句中文简述结果（做了什么、成没成、关键数据），不要客套。")
TASK_TIMEOUT_SEC = float(os.environ.get("GUANJIA_TASK_TIMEOUT", "90"))  # 单次执行总超时
CONFIRM_TTL_SEC = 180.0  # 待确认有效期：与网关守卫内存里的 3 分钟对齐
CONNECT_RETRIES = 10  # 连接被拒的重试次数（约 20 秒，覆盖网关重启）
CONNECT_RETRY_SEC = 2.0  # 重试间隔
FINAL = ("done", "failed", "cancelled", "expired")  # 终态
_CONFIRM_RE = re.compile(r"需要(你|您|主人)?确认|确认[吗么？?]|[回说答].{0,2}确认")  # 网关在问确认的特征


@dataclass
class Task:
    """一条后台任务。"""
    id: int
    text: str
    user: str  # 网关会话键，确认续跑时复用
    reply_when_done: bool = True
    status: str = "pending"  # pending / running / awaiting_confirm / done / failed / cancelled / expired
    result: str = ""
    attempt: int = 0  # 第几次执行（首跑=1，确认续跑+1）；旧执行的迟到结果按它丢弃
    t_submit: float = field(default_factory=time.perf_counter)
    t_done: float = 0.0
    t_awaiting: float = 0.0  # 进入待确认的时刻


def strip_english(text: str) -> str:
    """去掉不含中文的整段/整行英文旁白（如 "The automation creation requires..."），只留中文内容；全是英文则原样返回。"""
    kept = [ln for ln in text.splitlines() if re.search(r"[一-鿿]", ln)]
    return "\n".join(kept).strip() or text.strip()


class BackOffice:
    """后台任务表：每次执行独立线程，另起看门狗线程做超时 / 待确认过期。"""

    def __init__(self, on_done: Callable[[Task], None] | None = None) -> None:
        self.on_done = on_done
        self.tasks: dict[int, Task] = {}
        self._ids = itertools.count(1)
        self._run_tag = time.strftime("%m%d%H%M%S")  # 进程启动标记，拼进会话键防止重启后撞到旧会话
        self._lock = threading.Lock()  # 状态迁移互斥：同一次执行只结案一次

    # ---------- 对外 ----------
    def dispatch(self, cmd: dict) -> None:
        """执行前台本轮给出的指令：新任务 {"task":...} / 确认 {"confirm":id} / 取消 {"cancel":id}。"""
        if "confirm" in cmd:
            self.confirm(cmd["confirm"])
        elif "cancel" in cmd:
            self.cancel(cmd["cancel"])
        elif cmd.get("task"):
            self.submit(cmd["task"], bool(cmd.get("reply_when_done", True)))

    def submit(self, text: str, reply_when_done: bool = True) -> Task:
        tid = next(self._ids)
        t = Task(id=tid, text=text, user=f"{BACK_USER}-{self._run_tag}-{tid}", reply_when_done=reply_when_done)
        self.tasks[t.id] = t
        log.info("task#%d submitted (user=%s): %s", t.id, t.user, text)
        self._start(t, text, BACK_SYS)
        return t

    def confirm(self, task_id: int) -> None:
        """主人确认：同一个 user 只发"确认"，网关凭自身历史续跑原任务。"""
        t = self.tasks.get(task_id)
        with self._lock:
            if not t or t.status != "awaiting_confirm":
                log.warning("task#%s confirm ignored (status=%s)", task_id, t and t.status)
                return
        log.info("task#%d confirmed → 同会话续跑", t.id)
        self._start(t, "确认", None)

    def cancel(self, task_id: int) -> None:
        """主人否定：只在本地标记取消，不再联系网关（守卫待确认 3 分钟后自然失效）。"""
        t = self.tasks.get(task_id)
        with self._lock:
            if not t or t.status != "awaiting_confirm":
                return
            t.status, t.result, t.t_done = "cancelled", "主人取消了", time.perf_counter()
        log.info("task#%d cancelled", t.id)

    def active(self) -> list[tuple[int, str, float]]:
        """进行中的任务：(编号, 内容, 已耗时秒)，供前台注入 system。"""
        now = time.perf_counter()
        return [(t.id, t.text, now - t.t_submit) for t in self.tasks.values() if t.status in ("pending", "running")]

    def awaiting(self) -> tuple[int, str] | None:
        """最近一个仍在有效期内的待确认任务 (编号, 内容)。"""
        now = time.perf_counter()
        cands = [t for t in self.tasks.values()
                 if t.status == "awaiting_confirm" and now - t.t_awaiting < CONFIRM_TTL_SEC]
        if not cands:
            return None
        t = max(cands, key=lambda x: x.t_awaiting)
        return t.id, t.text

    # ---------- 内部 ----------
    def _start(self, t: Task, message: str, system: str | None) -> None:
        with self._lock:
            t.attempt += 1
            t.status = "running"
            attempt = t.attempt
        threading.Thread(target=self._run, args=(t, attempt, message, system),
                         name=f"task-{t.id}-{attempt}", daemon=True).start()
        threading.Thread(target=self._timeout_wd, args=(t, attempt), name=f"task-{t.id}-{attempt}-wd",
                         daemon=True).start()

    def _finish(self, t: Task, attempt: int, status: str, result: str) -> None:
        """结案 / 转待确认：只对当前这次执行生效一次，落日志并回调前台。"""
        with self._lock:
            if t.attempt != attempt or t.status != "running":
                return
            t.status, t.result, t.t_done = status, result, time.perf_counter()
            if status == "awaiting_confirm":
                t.t_awaiting = t.t_done
        lvl = logging.ERROR if status == "failed" else logging.INFO
        log.log(lvl, "task#%d %s (%.1fs): %s", t.id, status, t.t_done - t.t_submit, result[:200])
        if status == "awaiting_confirm":
            threading.Thread(target=self._expire_wd, args=(t, attempt), name=f"task-{t.id}-exp", daemon=True).start()
        self._notify(t)

    def _notify(self, t: Task) -> None:
        if self.on_done:
            try:
                self.on_done(t)
            except Exception:  # noqa: BLE001
                log.exception("task#%d on_done callback error", t.id)

    def _timeout_wd(self, t: Task, attempt: int) -> None:
        time.sleep(TASK_TIMEOUT_SEC)
        self._finish(t, attempt, "failed", f"超时：{TASK_TIMEOUT_SEC:.0f} 秒还没办完")

    def _expire_wd(self, t: Task, attempt: int) -> None:
        """待确认超过有效期：作废并告知主人。"""
        time.sleep(CONFIRM_TTL_SEC)
        with self._lock:
            if t.attempt != attempt or t.status != "awaiting_confirm":
                return
            t.status, t.result, t.t_done = "expired", "三分钟内没等到确认，这个任务已作废", time.perf_counter()
        log.info("task#%d expired", t.id)
        self._notify(t)

    def _run(self, t: Task, attempt: int, message: str, system: str | None) -> None:
        try:
            brain = Brain(model=BACK_MODEL, user=t.user, timeout=TASK_TIMEOUT_SEC + 5)
            try:
                for i in range(1, CONNECT_RETRIES + 1):
                    log.info("task#%d → 网关（执行 %d，第 %d 次）", t.id, attempt, i)
                    try:
                        result = brain.ask(message, system=system).strip()
                        break
                    except httpx.ConnectError as e:
                        log.warning("task#%d 网关连接失败（%d/%d）：%s", t.id, i, CONNECT_RETRIES, e)
                        if i == CONNECT_RETRIES:
                            raise
                        time.sleep(CONNECT_RETRY_SEC)
            finally:
                brain.close()
            result = strip_english(result) if result else "（后台没有返回内容）"
            status = "awaiting_confirm" if _CONFIRM_RE.search(result) else "done"
            self._finish(t, attempt, status, result)
        except Exception as e:  # noqa: BLE001  线程里任何异常都要落 ERROR + Traceback，并回调前台
            log.exception("task#%d 执行异常", t.id)
            self._finish(t, attempt, "failed", f"执行出错：{type(e).__name__}: {e}")
