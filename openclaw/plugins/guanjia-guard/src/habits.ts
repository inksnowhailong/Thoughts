import type { Store } from "./store";

/**
 * 主动侧窄版（6b / D15）：夜间脚本产出的周期性事件草案，
 * 由这里在"当天用户第一次开口"时口头问一次，用户说好才变成 cron。
 *
 *   [draft] --当天首次开口--> [asked] --"好"--> [accepted] (建 cron)
 *                                  └----"不"--> [rejected] (进否决表，永不再问)
 *                                  └--没答/答非所问--> 次日再问一次
 */

export interface HabitDraft {
  /** 稳定键：星期-时刻-归一化文本，用于去重与否决表 */
  key: string;
  /** 0=周日 … 6=周六 */
  weekday: number;
  /** "HH:MM" */
  time: string;
  /** 提醒内容（本地抽取，未上云） */
  text: string;
  /** 在多少个不同的周里出现过 */
  weeks: number;
  status: "draft" | "asked" | "accepted" | "rejected";
  askedAt?: string;
  decidedAt?: string;
  cronJobId?: string;
}

export interface HabitDraftsFile {
  drafts: HabitDraft[];
  /** 被否决的 key，夜间脚本与这里都不会再提 */
  rejected: string[];
  /** 最近一次发问的日期（YYYY-MM-DD），保证一天只问一次 */
  lastAskDate?: string;
  updatedAt?: string;
}

const WEEKDAY_CN = ["日", "一", "二", "三", "四", "五", "六"];
const YES_RE = /^(好|好的|好啊|行|可以|要|要的|嗯|对|是的|没问题|ok|okay|yes|加吧|设吧)/i;
const NO_RE = /^(不|不要|不用|别|算了|no|免了)/i;

/** 本地日期串，避免 toISOString 的 UTC 日期错位 */
export const localDate = (d = new Date()) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

/** 草案转一句口语问法 */
export const askSentence = (d: HabitDraft) => `要不要以后每周${WEEKDAY_CN[d.weekday]} ${d.time} 提醒你${d.text}？`;

/** 创建 cron 的最小接口（对应 PluginHookGatewayCronService.add） */
export type CronAdd = (input: any) => Promise<unknown>;

/**
 * 处理一次用户开口：先看是否在回答昨天/今天问过的草案，再决定今天要不要发问。
 * @returns 需要追加给模型的上下文（没有则 undefined）
 */
export async function onUserTurn(
  store: Store,
  userText: string,
  cronAdd: CronAdd | undefined,
  tz: string,
  now = new Date(),
): Promise<string | undefined> {
  const file = store.readJson<HabitDraftsFile>("habit-drafts.json", { drafts: [], rejected: [] });
  const text = userText.trim();
  const today = localDate(now);

  // 1) 回答中的草案：只认最近 24 小时内问过的
  const asked = file.drafts.find((d) => d.status === "asked" && d.askedAt && now.getTime() - Date.parse(d.askedAt) < 24 * 3600_000);
  if (asked) {
    if (NO_RE.test(text)) {
      asked.status = "rejected";
      asked.decidedAt = now.toISOString();
      if (!file.rejected.includes(asked.key)) file.rejected.push(asked.key);
      save(store, file);
      store.audit({ kind: "habit", action: "rejected", key: asked.key });
      return "（系统：用户拒绝了刚才的周期提醒建议，已记入否决列表不再询问。简短回应即可。）";
    }
    if (YES_RE.test(text)) {
      const [hh, mm] = asked.time.split(":").map(Number);
      let jobId: string | undefined;
      let error: string | undefined;
      try {
        if (!cronAdd) throw new Error("cron scheduler unavailable");
        const job: any = await cronAdd({
          declarationKey: `guanjia-habit:${asked.key}`,
          name: `guanjia-habit ${WEEKDAY_CN[asked.weekday]}${asked.time} ${asked.text}`.slice(0, 80),
          description: "由管家习惯草案经用户口头确认后创建（S3 6b）",
          enabled: true,
          schedule: { kind: "cron", expr: `${mm} ${hh} * * ${asked.weekday}`, tz },
          sessionTarget: "main",
          wakeMode: "now",
          payload: { kind: "systemEvent", text: `[guanjia:habit-reminder] 到点提醒用户：${asked.text}（一句话说完）` },
        });
        jobId = job?.id ?? job?.job?.id;
      } catch (err: any) {
        error = String(err?.message ?? err);
      }
      asked.decidedAt = now.toISOString();
      if (error) {
        store.audit({ kind: "habit", action: "accept-failed", key: asked.key, error });
        save(store, file);
        return `（系统：用户同意了周期提醒，但创建定时任务失败：${error}。请如实告诉用户没设成功。）`;
      }
      asked.status = "accepted";
      asked.cronJobId = jobId;
      save(store, file);
      store.audit({ kind: "habit", action: "accepted", key: asked.key, cronJobId: jobId });
      return `（系统：用户同意，已创建每周${WEEKDAY_CN[asked.weekday]} ${asked.time} 的提醒。简短确认一句即可。）`;
    }
    // 答非所问：不纠缠，保持 asked，明天再问
  }

  // 2) 今天第一次开口且不在深夜：挑一个未否决的草案发问
  const hour = now.getHours();
  if (file.lastAskDate === today || hour < 7 || hour >= 23) return undefined;
  const next = file.drafts.find((d) => (d.status === "draft" || d.status === "asked") && !file.rejected.includes(d.key));
  if (!next) return undefined;
  next.status = "asked";
  next.askedAt = now.toISOString();
  file.lastAskDate = today;
  save(store, file);
  store.audit({ kind: "habit", action: "asked", key: next.key });
  return `（系统：今天用户第一次开口。请在回复开头先问一句："${askSentence(next)}"，然后再回应用户这句话。整体仍不超过两句。）`;
}

function save(store: Store, file: HabitDraftsFile) {
  file.updatedAt = new Date().toISOString();
  store.writeJson("habit-drafts.json", file);
}
