"""主动搭话的富上下文组装器（富上下文版生成的前置件）。

设计理念（发起人定稿）：记忆是背景知识，不是话题清单——
不要"检索到迪奥就每次说迪奥"；上下文只提供"你了解这个家的什么"，
说什么由生成模型每次自由判断，话题多样性靠模型创造力保证。

所有文件读取全部容错：文件不存在/读失败 → 对应段落静默省略，绝不抛异常
（富上下文是加分项，不能因为组装失败挂掉整条搭话管线）。
"""

from __future__ import annotations

from datetime import datetime
import json
import re
from pathlib import Path

_WORKSPACE = Path.home() / '.openclaw' / 'workspace'
_PEOPLE_DIR = _WORKSPACE / 'memory' / 'people'
_GEMS_DIR = _WORKSPACE / 'memory' / 'gems'

# 记忆文件里的人物名 → 管家口中的称呼（与 identity._GENDER_TO_WHO 对齐）
_WHO_ALIASES = {'主人': '先生', '老婆': '大小姐', '先生': '先生', '大小姐': '大小姐'}

# 管家自知（固定文本）：自我认知随每次生成注入，"知道自己回复慢"是人设的一部分
_SELF_AWARENESS = (
    '你知道自己回复速度比较慢，也不擅长帮人选东西；'
    '这些可以在话里自然提及，不必每次都说。'
)

# 时事锚点：(月, 日, 名称, 提前预热天数)。农历节日用公历近似窗口，够给生成模型起话题即可
_ANCHORS: tuple[tuple[int, int, str, int], ...] = (
    (1, 1, '元旦', 7),
    (2, 14, '情人节', 7),
    (3, 8, '妇女节', 3),
    (4, 1, '愚人节', 2),
    (5, 1, '五一假期', 5),
    (6, 1, '儿童节', 3),
    (6, 7, '高考', 5),
    (9, 10, '教师节', 3),
    (10, 1, '国庆假期', 6),
    (11, 11, '双十一', 18),
    (12, 12, '双十二', 8),
    (12, 25, '圣诞节', 7),
)

_WEEKDAYS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')


class ContextBuilder:
    """组装给生成模型的富上下文：自然段落文字，不是 JSON。"""

    def __init__(self, config: object, habits: object | None = None) -> None:
        self._config = config
        self._habits = habits
        # 最近话题与 trigger 的管线日志同源（proactive.json 同目录的 proactive-log.jsonl）
        self._log_path = Path(str(getattr(config, 'path', ''))).parent / 'proactive-log.jsonl'

    def build(self, edge: str, presence: str, record: dict) -> str:
        """组装富上下文；任何一段读失败都静默省略，永远返回可用文字。"""
        now = datetime.now()
        sections = [
            self._profiles(),
            self._gems(),
            self._habits_brief(now),
            self._environment(edge, presence, record, now),
            self._anchors(now),
            self._recent_topics(),
            _SELF_AWARENESS,
        ]
        body = '\n'.join(section for section in sections if section)
        return f'你是这个家的管家，叫大痔。你对这个家的了解：\n{body}'

    # ---------- 家庭画像 ----------

    def _profiles(self) -> str:
        """家庭画像：people/*.md 只取关键词摘要，不喂全文（省 token，防画像喧宾夺主）。"""
        lines: list[str] = []
        try:
            files = sorted(_PEOPLE_DIR.glob('*.md'))
        except Exception:
            return ''
        for path in files:
            try:
                text = path.read_text(encoding='utf-8')
            except Exception:
                continue
            summary = self._summarize_profile(text)
            if summary:
                name = _WHO_ALIASES.get(path.stem, path.stem)
                lines.append(f'{name}：{summary}')
        return '\n'.join(lines)

    @staticmethod
    def _summarize_profile(text: str, max_lines: int = 3, line_limit: int = 60) -> str:
        """把画像 markdown 压成关键词摘要：去标记、取前几行、每行截断。"""
        fragments: list[str] = []
        for raw in text.splitlines():
            line = re.sub(r'[#>*`_\-\[\]]', '', raw).strip()
            line = re.sub(r'\s+', ' ', line)
            if len(line) < 4:
                continue  # 跳过空行与短碎行
            fragments.append(line[:line_limit])
            if len(fragments) >= max_lines:
                break
        return '；'.join(fragments)

    # ---------- 最近宝石 ----------

    def _gems(self) -> str:
        """最近宝石：最新 gems 文件的最后 3 条（园丁提取的高价值记忆，只当背景）。"""
        try:
            files = sorted(_GEMS_DIR.glob('gems-*.md'))
            if not files:
                return ''
            lines = [
                line.strip()
                for line in files[-1].read_text(encoding='utf-8').splitlines()
                if line.strip().startswith('- ')
            ]
            if not lines:
                return ''
            entries = [re.sub(r'^-\s*', '', line)[:80] for line in lines[-3:]]
            return '最近家里发生的事：' + '；'.join(entries)
        except Exception:
            return ''

    # ---------- 习惯观察 ----------

    def _habits_brief(self, now: datetime) -> str:
        """当前时段的习惯观察（habits 库时间键直读）："这个点通常……"。"""
        if self._habits is None:
            return ''
        try:
            rows = self._habits.habits_around(now.hour, now.minute, window_min=30)
        except Exception:
            return ''
        facts = [str(row.get('fact') or '').strip() for row in rows[:2]]
        facts = [fact for fact in facts if fact]
        if not facts:
            return ''
        return '这个时段的日常：' + '；'.join(facts)

    # ---------- 当前环境 ----------

    def _environment(self, edge: str, presence: str, record: dict, now: datetime) -> str:
        """当前环境：时间/星期 + 天气缓存（有才带）+ 画面描述。"""
        parts = [f'现在是 {now.strftime("%m月%d日 %H:%M")}，{_WEEKDAYS[now.weekday()]}。']
        weather = self._weather()
        if weather:
            parts.append(f'天气{weather}。')
        area = str(record.get('area') or '').strip()
        move = '刚进入画面' if edge == 'entered' else '刚离开画面'
        if presence in ('未知', ''):
            parts.append(f'画面里有人{move}' + (f'（{area}）' if area else '') + '。')
        else:
            parts.append(f'{presence}{move}' + (f'（{area}）' if area else '') + '。')
        return ''.join(parts)

    def _weather(self) -> str:
        """天气缓存（有 weather.json 才带，没有就省略——不引入联网依赖）。"""
        try:
            path = Path(str(getattr(self._config, 'path', ''))).parent / 'weather.json'
            data = json.loads(path.read_text(encoding='utf-8'))
            text = str(data.get('text') or data.get('weather') or '').strip()
            return text[:30]
        except Exception:
            return ''

    # ---------- 时事锚点 ----------

    def _anchors(self, now: datetime) -> str:
        """时事锚点：临近的节日/购物节点，给模型一个"聊当下"的抓手。"""
        notes: list[str] = []
        for month, day, name, lead in _ANCHORS:
            anchor = now.replace(month=month, day=day)
            gap = (anchor - now).days
            if 0 <= gap <= lead:
                notes.append(f'快到{name}了' if gap > 0 else f'今天是{name}')
        # 国庆假期进行中（10/1~10/7）
        if now.month == 10 and 1 <= now.day <= 7:
            notes.append('国庆假期正在进行中')
        # 春节（农历）：公历近似窗口 1 月下旬 ~ 2 月中旬
        if (now.month == 1 and now.day >= 20) or (now.month == 2 and now.day <= 15):
            notes.append('临近春节（按农历为准）')
        return ''.join(note + '。' for note in notes[:2])

    # ---------- 最近话题 ----------

    def _recent_topics(self, limit: int = 3) -> str:
        """最近说过的话：管线日志里最近 3 条 delivered 的 text（防重复参照）。"""
        try:
            lines = self._log_path.read_text(encoding='utf-8').splitlines()
        except Exception:
            return ''
        texts: list[str] = []
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get('stage') == 'delivered' and entry.get('text'):
                texts.append(str(entry['text'])[:60])
                if len(texts) >= limit:
                    break
        if not texts:
            return ''
        numbered = '；'.join(f'{i}. {text}' for i, text in enumerate(reversed(texts), 1))
        return f'你之前说过的话：{numbered}'


__all__ = ['ContextBuilder']