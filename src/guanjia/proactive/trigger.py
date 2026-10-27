"""C 主动触发引擎（议会定稿）：视觉沿 → 候选 → 评分门控 → 模型评审 → 生成 → speak 投递。

接线 self_loop 骨架（议会 §1-C）：
- SelfLoopCandidate：候选 DTO（value/urgency/freshness/interruption_cost 四因子）
- ReviewPolicy：确定性评分——定位为预算/安全闸（SX-2：判断权在模型评审，公式只管预算）
- ExpressionPolicy：说话预算/话题冷却/游戏态压制（F 组件边界，QC-8 收敛机制）
- SelfLoopState：内存态（第一版无 store，重启重置——后续接 store 持久化）

时序（QC-2/JG-6）：沿直达评审（异步线程即时），30 分钟兜底轮询不做发声路径。
评审与生成分离（SX-9）：评审输入小、否决掉不值得生成的候选，省下的生成成本大于评审成本。
在途作废（JG-16）：投递前查最新视觉状态，人已离开即丢弃。
全局开关（JG-8/QC-17）：proactive.json enabled=false 全链停——评审、生成一并停，关停不烧额度。
独立限流（SX-12/JG-19）：评审调用每小时上限，防沿风暴拖垮被动链。

反馈闭环（A）：投递后 60 秒窗口检测用户回应——被无视自动收敛间隔/暂停陪伴/当天停说。
事实降级（B）：生成 prompt 明确约束——不确定的事实要么不说，要么说「我猜」。
夜间豁免（C）：urgency >= 0.8 的紧急提醒允许夜间通过静默闸。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from guanjia.brain import load_gateway_token
from guanjia.proactive.config import ProactiveConfig, is_night_hour
from guanjia.proactive.context_builder import ContextBuilder
from guanjia.proactive.identity import IdentityTracker
from guanjia.self_loop.contracts import SelfLoopCandidate
from guanjia.self_loop.policy import ExpressionPolicy, ReviewPolicy
from guanjia.self_loop.state import SelfLoopState

_REVIEW_SESSION = 'agent:main:proactive-review'
_GATEWAY_URL = 'http://127.0.0.1:18789/v1/chat/completions'

_REVIEW_PROMPT = (
    '你是家庭管家的主动开口评审。刚发生事件：{event}。当前在场：{presence}。'
    '北京时间：{local_time}。画面区域：{area}。'
    '判断：此刻管家主动说一句话值得吗？原则：不打扰、不说废话、说错的代价大于不说；'
    '家人正在说话/休息/专注时不说。只输出一个 JSON 对象：'
    '{{"say": true 或 false, "what_hint": "说什么方向（一句）", '
    '"kind": "information（有用信息/提醒）或 companion（纯陪伴闲聊）", '
    '"urgency": 0到1, "reason": "一句理由"}}，不要 markdown。'
)

# 富上下文版生成 prompt（D 升级）：上下文由 ContextBuilder 组装成自然段落，
# 话题由模型自由创造——记忆只是背景，不规定这次说什么（发起人核心理念）
_GENERATE_PROMPT = (
    '{context}\n\n'
    '基于以上你了解的一切，此刻跟{who}自然地说一句话。\n'
    '说什么由你自己判断——可以是有价值的建议、可以是天气/时事的关心、'
    '可以是随意的闲聊。但不要重复你之前说过的话题。\n'
    '不要说"我在呢"这种空洞问候。要有信息量、有趣、或者有用。\n'
    '重要：不要编造你不确定的事实（具体商品名/新品/价格/日期）。\n'
    '如果不确定某件事是否真实，要么不说，要么说「我猜/好像是」。\n'
    '宁可说天气和眼前的画面，也不要编造新闻和商品信息。\n'
    '{address_rule}'
    '只输出要直接说出口的口语：每句不超过 30 个字、最多两句、无 Markdown、无表情符号。'
    '直接输出那句话本身。'
)


class FeedbackTracker:
    """开口后的用户反馈检测与频率收敛（反馈闭环 A）。

    投递成功后 60 秒窗口内检测：
    - 有语音活动（voice.py 的 note_voice_activity 被调用）→ 回应
    - 语音轮产生（主会话有新 user 消息）→ 回应
    - 无任何活动 → 被无视

    收敛规则：
    - 连续 1 次无视：下次搭话间隔 × 1.5
    - 连续 2 次无视：暂停纯陪伴型 4 小时
    - 连续 3 次无视：当天停止主动搭话
    - 获得回应：连续无视归零，间隔逐步恢复
    """

    def __init__(
        self,
        config: ProactiveConfig,
        get_voice_activity: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        # 读最近一次语音活动时间戳（monotonic；0.0 表示从未有过对话）
        self._get_voice_activity = get_voice_activity or (lambda: 0.0)
        self._ignore_streak = 0                  # 连续被无视计数
        self._interval_factor = 1.0              # 间隔收敛倍率（被无视 ×1.5，回应 ×0.8 恢复）
        self._last_delivered_at = 0.0             # 最近投递时刻（monotonic）
        self._delivered_activity = 0.0            # 投递时刻的语音活动快照（比对"新"活动的基线）
        self._window_sec = config.num('feedback_window_sec', 60.0)
        self._base_gap = config.num('feedback_min_gap_sec', 300.0)
        self._companion_paused_until = 0.0        # 纯陪伴型暂停截止（monotonic）
        self._all_paused_until = 0.0              # 全类型暂停截止（当天停止）

    @property
    def window_sec(self) -> float:
        """回应检测窗口时长（秒）。"""
        return self._window_sec

    @property
    def ignore_streak(self) -> int:
        """当前连续被无视次数。"""
        return self._ignore_streak

    def note_delivered(self) -> None:
        """投递成功后调用——开始计时窗口，并快照当前语音活动基线。"""
        self._last_delivered_at = time.monotonic()
        self._delivered_activity = self._get_voice_activity()

    def check_response(self) -> str:
        """检查窗口内是否有回应。返回 'responded' / 'ignored' / 'pending'。

        简单实现：读 voice.py 维护的 last_voice_activity 时间戳——
        窗口满后时间戳比投递时前进（有新语音轮）即视为回应。
        """
        if self._last_delivered_at <= 0.0:
            return 'pending'
        if time.monotonic() - self._last_delivered_at < self._window_sec:
            return 'pending'  # 窗口未满，尚不能下结论
        return 'responded' if self._get_voice_activity() > self._delivered_activity else 'ignored'

    def apply_outcome(self, outcome: str) -> None:
        """把检测结论落到收敛账本：无视累计、间隔收敛、暂停窗。"""
        if outcome == 'responded':
            self._ignore_streak = 0
            self._interval_factor = max(1.0, self._interval_factor * 0.8)  # 间隔逐步恢复
        elif outcome == 'ignored':
            self._ignore_streak += 1
            self._interval_factor = min(self._interval_factor * 1.5, 12.0)
            if self._ignore_streak >= 2:
                # 连续 2 次：纯陪伴型暂停 4 小时
                self._companion_paused_until = time.monotonic() + 4 * 3600.0
            if self._ignore_streak >= 3:
                # 连续 3 次：当天停止——暂停到本地 23:59:59（wall clock 换算成 monotonic 时长）
                now_local = datetime.now()
                midnight = now_local.replace(hour=23, minute=59, second=59, microsecond=0)
                remain = max(60.0, (midnight - now_local).total_seconds())
                self._all_paused_until = time.monotonic() + remain

    def should_throttle(self, kind: str) -> tuple[bool, str]:
        """根据历史反馈判断是否限流。kind: 'information' / 'companion'。

        返回 (是否允许, 原因)——允许时原因为空串。
        """
        now = time.monotonic()
        if now < self._all_paused_until:
            return False, '连续3次被无视：当天停止主动搭话'
        if kind == 'companion' and now < self._companion_paused_until:
            return False, '连续2次被无视：纯陪伴型暂停4小时'
        if self._last_delivered_at > 0.0:
            gap = self._base_gap * self._interval_factor
            if now - self._last_delivered_at < gap:
                return False, f'被无视后间隔收敛中（×{self._interval_factor:.1f}）'
        return True, ''


class ProactiveTrigger:
    """沿触发的主动开口管线；全部模型调用异步，绝不阻塞视觉/语音线程。"""

    def __init__(
        self,
        config: ProactiveConfig | None = None,
        identity: IdentityTracker | None = None,
        speak: Callable[[str], bool] | None = None,
        log_path: Path | None = None,
        timeline: object | None = None,
        habits: object | None = None,
    ) -> None:
        self._config = config or ProactiveConfig()
        self._identity = identity or IdentityTracker(self._config)
        self._speak = speak or self._default_speak
        self._timeline = timeline
        self._token = load_gateway_token()
        # 直调智谱（经保险丝）的端点与密钥——评审/生成不再走网关会话（加速 10-27）
        from guanjia.config_hub import ConfigHub
        hub = ConfigHub()
        self._zhipu_key = hub.key('zhipu')
        self._model_url = str(hub.get('models.brain.fuse', 'http://127.0.0.1:18793')).rstrip('/') + '/api/paas/v4/chat/completions'
        self._log_path = log_path or (
            Path(str(self._config.path)).parent / 'proactive-log.jsonl'
        )
        # self_loop 骨架件：评分闸 + 表达边界 + 内存态
        self._review_policy = ReviewPolicy(
            minimum_score=self._config.num('review_minimum_score', 0.35),
            urgent_threshold=self._config.num('review_urgent_threshold', 0.9),
            review_interval_seconds=1,  # 沿直达评审：不做周期节流（QC-2）
        )
        # 富上下文组装器：生成前把家庭画像/gems/环境/时事/最近话题拼成自然段落
        self._context_builder = ContextBuilder(self._config, habits)
        self._expression_policy = ExpressionPolicy(
            daily_budget=int(self._config.num('expression_daily_budget', 8)),  # 防爆值不是配额（V12b）
            topic_cooldown_seconds=int(self._config.num('topic_cooldown_sec', 3600)),
            minimum_score=self._config.num('review_minimum_score', 0.35),
        )
        self._state = SelfLoopState()
        self._lock = threading.Lock()
        self._review_times: list[float] = []          # 独立限流（每小时上限）
        self._expression_times: list[float] = []      # 开口时刻（滑动窗口预算）
        self._last_voice_activity: float = 0.0         # 对话进行中静默（QC-22）
        self._mute_until: float = 0.0                  # 住户急停静默窗（YH-8）
        self._busy = False                             # 同一时刻只跑一条管线
        self._gaming: bool = False                     # 游戏态压制输入（voice.py 接线）
        # 反馈闭环（A）：voice.py 调 note_voice_activity 更新时间戳，
        # FeedbackTracker 拿它当"用户回应"信号
        self._feedback = FeedbackTracker(
            self._config, lambda: self._last_voice_activity)

    # ---------- 对外接口 ----------

    def on_edge(self, record: dict[str, object], edge: str) -> None:
        """vision_loop 沿钩子入口。entered/left→管线；alone→独处计时管线的搭话机会。"""
        if not self._enabled():
            return
        if edge == 'entered':
            self._pipeline_async(record, 'entered')
        elif edge == 'left':
            self._pipeline_async(record, 'left')
        elif edge == 'alone':
            # 独处计时（发起人定 1 小时）：人持续在场无沿 → 模拟一次搭话机会
            self._pipeline_async(record, 'alone')

    def note_voice_activity(self) -> None:
        """voice.py 每轮对话调用：对话进行中静默窗（QC-22）计时起点。"""
        self._last_voice_activity = time.monotonic()

    def emergency_mute(self, minutes: float = 30.0) -> None:
        """住户急停（YH-8）：本地确定性命令，不经大模型；拉长表达静默窗。"""
        self._mute_until = time.monotonic() + minutes * 60.0

    def set_gaming(self, gaming: bool) -> None:
        """游戏态输入（ExpressionPolicy 原生支持 gaming 压制）。"""
        self._gaming = bool(gaming)

    # ---------- 管线 ----------

    def _pipeline_async(self, record: dict[str, object], edge: str) -> None:
        with self._lock:
            if self._busy:
                return  # 同一时刻一条管线：沿风暴时自然排队丢弃，不堆积
            self._busy = True

        def runner() -> None:
            try:
                self._pipeline(record, edge)
            except Exception as exc:  # noqa: BLE001
                self._log({'ok': False, 'stage': 'pipeline', 'error': f'{type(exc).__name__}: {exc}'})
            finally:
                with self._lock:
                    self._busy = False

        threading.Thread(target=runner, name='proactive-pipeline', daemon=True).start()

    def _pipeline(self, record: dict[str, object], edge: str) -> None:
        # 端到端延迟测量（A）：管线起点——沿触发到投递的总耗时基准
        pipeline_start = time.monotonic()
        review_ms = 0    # 评审耗时（模型评审调用）
        generate_ms = 0  # 生成耗时（富上下文生成调用；预设命中时记 0）
        # 访客模式（B）：全停主动搭话（访客在场时不暴露家庭信息）——本地判定，不经模型
        if self._config.values.get('visitor_mode', False):
            self._log({'ok': True, 'stage': 'visitor_mode_blocked', 'edge': edge})
            return
        # 住户急停静默窗（YH-8）：mute 期内一切主动开口管线直接丢弃（本地判定，不经模型）
        if time.monotonic() < self._mute_until:
            self._log({'ok': True, 'stage': 'muted', 'edge': edge})
            return
        # 反馈闭环（A）：管线入口限流——上次开口被无视时，间隔收敛/陪伴暂停/当天停说
        allowed, throttle_reason = self._feedback.should_throttle('information')
        if not allowed:
            self._log({'ok': True, 'stage': 'feedback_throttled', 'edge': edge,
                       'reason': throttle_reason})
            return
        now = datetime.now(timezone.utc)
        who = self._identity.presence()
        presence_text = str((who or {}).get('who') or '未知')
        area = str(record.get('area') or '未知区域')
        local_time = datetime.now().strftime('%m月%d日 %H:%M')
        if edge == 'entered':
            event = f'有人进入画面（{presence_text}，{area}）'
        elif edge == 'left':
            event = f'有人离开画面（{presence_text}，{area}）'
        else:
            elapsed = int(record.get('alone_elapsed_sec') or 3600)
            event = f'{presence_text}已持续在场{elapsed // 60}分钟（{area}），一直没有交流'

        # 1) 候选（沿事件 → 四因子；entered 价值高于 left）
        candidate = SelfLoopCandidate(
            topic=f'vision_{edge}',
            summary=event,
            value=self._config.num(f'{edge}_value', 0.6 if edge == 'entered' else 0.35),
            urgency=self._config.num(f'{edge}_urgency', 0.4 if edge == 'entered' else 0.2),
            freshness=0.9,
            interruption_cost=self._config.num(f'{edge}_interruption_cost', 0.5),
            observed_at=now,
        )
        # 夜间静默（发起人裁决 9b，V1 修复）：夜里出来上厕所也不说话——本地时钟判定，
        # 不进模型评审（评审 prompt 不知道深夜，不能依赖它自觉）。
        # 紧急豁免（C）：urgency >= 0.8 的紧急提醒夜间也允许通过
        if (is_night_hour(datetime.now().hour, self._config.night_start_hour, self._config.night_end_hour)
                and candidate.urgency < 0.8):
            self._log({'ok': True, 'stage': 'night_suppressed', 'edge': edge})
            return
        # 2) 评分门控：预算/安全闸（SX-2：被公式拦下的不进模型评审，只记日志）
        gate = self._review_policy.decide(candidate, self._state, now)
        if not gate.eligible:
            self._log({'ok': True, 'stage': 'gate', 'edge': edge, 'score': round(gate.score, 3),
                       'blocked': gate.reason})
            return
        # 3) 独立限流（SX-12）：每小时评审上限，防沿风暴
        if not self._acquire_review_slot():
            self._log({'ok': True, 'stage': 'rate_limit', 'edge': edge})
            return
        # 3.5) 开口防爆窗（V12b）：宽松滑动窗口 + 日总——只防"真话痨爆发"，
        # 不挤正常密度（早晨流程各事件独立成话题，由细 topic 冷却管重复）。
        now_mono = time.monotonic()
        window_sec = self._config.num('speak_window_sec', 6 * 3600)
        window_cap = int(self._config.num('speak_window_capacity', 4))
        with self._lock:
            self._expression_times = [t for t in self._expression_times if now_mono - t < window_sec]
            if len(self._expression_times) >= window_cap:
                self._log({'ok': True, 'stage': 'window_budget', 'edge': edge})
                return
        # 日总兜底（ExpressionPolicy 的 daily_budget，防真话痨；额度放到宽裕值）
        if self._state.expressions_today(now) >= self._expression_policy.daily_budget:
            self._log({'ok': True, 'stage': 'budget_precheck', 'edge': edge})
            return
        last = self._state.last_expression(candidate.topic)
        if last is not None and now - last < self._expression_policy.topic_cooldown:
            self._log({'ok': True, 'stage': 'cooldown_precheck', 'edge': edge})
            return
        # 4) 说话内容来源——预热句快路（发起人 10-27 要求加速）：
        # 当日预设（时间轴预热）生成时已过评审规格约束，直接用，省掉 9 秒级模型评审；
        # 没有预设才走"值得说吗"模型评审（直调小上下文，2~3 秒）。
        preset = None
        if self._timeline is not None:
            try:
                preset = self._timeline.preset_for(presence_text, datetime.now().hour)
            except Exception:
                preset = None
        if preset:
            parsed = {'say': True, 'kind': 'companion', 'what_hint': 'preset'}
            review_ms = 0
            self._log({'ok': True, 'stage': 'preset_fastpath', 'edge': edge})
        else:
            review_start = time.monotonic()
            verdict = self._ask_gateway(_REVIEW_PROMPT.format(
                event=event, presence=presence_text, local_time=local_time, area=area))
            review_ms = int((time.monotonic() - review_start) * 1000)
            parsed = self._parse_json(verdict)
            if not parsed or not parsed.get('say'):
                self._log({'ok': True, 'stage': 'review_rejected', 'edge': edge,
                           'reason': str((parsed or {}).get('reason') or 'no_json')[:80]})
                return
        # 反馈闭环（A）：评审给出话题类型后复查限流——纯陪伴型有更严的被无视收敛
        kind = str(parsed.get('kind') or 'information')
        allowed, throttle_reason = self._feedback.should_throttle(kind)
        if not allowed:
            self._log({'ok': True, 'stage': 'feedback_throttled', 'edge': edge,
                       'kind': kind, 'reason': throttle_reason})
            return
        # 4.5) 话题细化（V12b，发起人指出"早晨流程密集且每件事各有价值"）：
        # 粗 topic（vision_entered）会让早晨三件不同的事互相挤兑冷却；
        # 评审给出的 what_hint 才是话题本体——"带鸡蛋提醒"和"天气提醒"是两个话题，
        # 各自能说；真正的烦人是"同一件事重复说"，由细 topic 的 1 小时冷却掐住。
        hint = re.sub(r'\s+', '', str(parsed.get('what_hint') or ''))[:16]
        fine_topic = f'{edge}:{hint}' if hint else candidate.topic
        from dataclasses import replace as _dc_replace
        candidate = _dc_replace(candidate, topic=fine_topic)
        # 5) 表达边界（F）：细 topic 冷却 / 窗口与日总防爆 / 游戏态 / 对话中静默
        decision = self._expression_policy.decide(
            candidate, self._state, now, gaming=getattr(self, '_gaming', False),
            do_not_disturb=self._voice_silence_active())
        if not decision.eligible:
            self._log({'ok': True, 'stage': 'expression_blocked', 'edge': edge,
                       'blocked': decision.reason})
            return
        # 6) 生成（D 富上下文版；身份置信不足时不带称呼——QC-5）
        address_rule = (
            '称呼对方为"{}"。'.format(presence_text)
            if self._identity.should_address_by_name() and presence_text not in ('未知',) else ''
        )
        # 发起人要求称"先生"；对先生用"你"自然相称即可，不必每句都喊称呼
        if presence_text == '先生' and self._identity.should_address_by_name():
            address_rule = '对先生说话，自然称呼即可。'
        # 混合时序（裁决 7）：预设已在第 4 步取好（快路），箱空现想
        if preset:
            text = preset  # 预热生成时已约束 30 字/两句规格
            self._log({'ok': True, 'stage': 'preset_hit', 'edge': edge})
        else:
            # 富上下文生成（D 升级）：评审通过才组装（组装有读文件开销，被否决的候选不花这份钱）；
            # 上下文里已含时间/画面/最近说过的话，event/hint 不再单独传——话题判断权全交模型
            context = self._context_builder.build(edge, presence_text, record)
            generate_start = time.monotonic()
            message = self._ask_gateway(_GENERATE_PROMPT.format(
                context=context, who=presence_text, address_rule=address_rule))
            generate_ms = int((time.monotonic() - generate_start) * 1000)
            text = (message or '').strip()
        if not text:
            self._log({'ok': False, 'stage': 'generate_empty', 'edge': edge})
            return
        text = re.sub(r'\s+', ' ', text)[:120]
        # 7) 在途作废（JG-16）：投递前查最新视觉状态——人已离开且事件是 entered，则丢弃
        if edge == 'entered' and not self._person_still_present():
            self._log({'ok': True, 'stage': 'dropped_left', 'edge': edge,
                       'note': '人已离开，在途话语作废'})
            return
        # 8) 投递 + 记账（细 topic 冷却 + 窗口/日总防爆双账本）
        delivered = self._speak(text)
        self._state.record_expression(candidate, datetime.now(timezone.utc))
        with self._lock:
            self._expression_times.append(time.monotonic())
        # 端到端延迟测量（A）：投递成功后落账——管线总耗时 + 分段耗时
        pipeline_end = time.monotonic()
        total_ms = int((pipeline_end - pipeline_start) * 1000)
        self._log({'ok': True, 'stage': 'delivered', 'edge': edge, 'text': text,
                   'topic': fine_topic,
                   'delivered': bool(delivered), 'score': round(decision.score, 3),
                   'pipeline_ms': total_ms,  # 管线总耗时（从沿触发到投递）
                   'review_ms': review_ms,    # 评审耗时
                   'generate_ms': generate_ms})  # 生成耗时
        # 反馈闭环（A）：投递成功后开启 60 秒回应检测窗口，异步回看有无被无视
        if delivered:
            self._feedback.note_delivered()
            threading.Thread(
                target=self._watch_feedback, name='proactive-feedback-watch', daemon=True,
            ).start()

    # ---------- 内部 ----------

    def _enabled(self) -> bool:
        """全局开关热重读（V4 修复）：每次沿事件重读 proactive.json——改文件立即生效。

        关停语义（JG-8/QC-17）：enabled=false 在 on_edge 入口拦下，
        评审/生成完全不发生，关停不烧额度；无需重启进程。
        """
        try:
            self._config = ProactiveConfig(path=self._config.path)
        except Exception:
            pass
        return bool(self._config.values.get('proactive_enabled', True))

    def _acquire_review_slot(self) -> bool:
        limit = int(self._config.num('review_max_per_hour', 10))
        now = time.monotonic()
        with self._lock:
            self._review_times = [t for t in self._review_times if now - t < 3600.0]
            if len(self._review_times) >= limit:
                return False
            self._review_times.append(now)
            return True

    def _watch_feedback(self) -> None:
        """反馈闭环（A）：投递 60 秒后回看——有回应归零收敛，被无视则收紧频率。"""
        try:
            time.sleep(self._feedback.window_sec)
            outcome = self._feedback.check_response()
            if outcome != 'pending':
                self._feedback.apply_outcome(outcome)
            self._log({'ok': True, 'stage': 'feedback_checked', 'outcome': outcome,
                       'ignore_streak': self._feedback.ignore_streak})
        except Exception as exc:  # noqa: BLE001
            self._log({'ok': False, 'stage': 'feedback_watch',
                       'error': f'{type(exc).__name__}: {exc}'})

    def _voice_silence_active(self) -> bool:
        """对话进行中静默（QC-22）：最近 N 分钟有语音活动则压制主动开口。"""
        window = self._config.num('conversation_quiet_min', 5) * 60
        return (time.monotonic() - self._last_voice_activity) < window if self._last_voice_activity else False

    def _person_still_present(self) -> bool:
        """投递前在途检查：读 latest.json 的 person_present。"""
        try:
            latest = json.loads(
                Path(str(record_path(self._config))).read_text(encoding='utf-8'))
            return bool(latest.get('person_present'))
        except Exception:
            return True  # 读不到就投——fail-open（在途作废是加分项不是安全闸）

    def _ask_gateway(self, prompt: str) -> str | None:
        """直调智谱（经保险丝）——评审/生成 prompt 自包含，不驮网关会话上下文。

        原来走网关会话（agent:main:proactive-review）要编译全套 system prompt +
        记忆检索再发模型，首响 9 秒起（发起人 10-27 要求加速）。直调小 prompt
        2~3 秒内出结果；thinking/low 由保险丝统一注入，模型仍走 glm-5.3-flash。
        """
        key = self._zhipu_key
        if not key:
            return None
        body = {
            'model': 'glm-5.3-flash',
            'messages': [{'role': 'user', 'content': prompt}],
            'stream': True,  # 流式避免保险丝 25 秒首字窗误杀
        }
        headers = {
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream',
        }
        parts: list[str] = []
        try:
            with httpx.Client(timeout=25.0, trust_env=False) as client:
                with client.stream('POST', self._model_url, headers=headers, json=body) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if data == '[DONE]':
                            break
                        chunk = json.loads(data)
                        for choice in chunk.get('choices') or []:
                            parts.append((choice.get('delta') or {}).get('content') or '')
            return ''.join(parts).strip() or None
        except Exception:
            return None

    @staticmethod
    def _parse_json(reply: str | None) -> dict[str, object] | None:
        if not reply:
            return None
        match = re.search(r'\{.*\}', reply, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _default_speak(text: str) -> bool:
        """speak 投递：POST 语音进程 :18795/say（SAY_MAX_PENDING 队列保护兜底）。"""
        try:
            with httpx.Client(timeout=4.0, trust_env=False) as client:
                response = client.post('http://127.0.0.1:18795/say', json={'text': text})
                return int(response.status_code) == 200
        except Exception:
            return False

    def _log(self, entry: dict[str, object]) -> None:
        """管线日志（jsonl 追加）：验收 1 的日志回放抽检数据源。"""
        try:
            payload = dict(entry)
            payload['ts'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + '\n')
        except Exception:
            pass


def record_path(config: ProactiveConfig) -> str:
    """latest.json 路径（与 proactive.json 同目录）。"""
    return str(Path(str(config.path)).parent / 'latest.json')


__all__ = ['ProactiveTrigger', 'FeedbackTracker']
