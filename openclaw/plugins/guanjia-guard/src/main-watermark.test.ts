import { recordMainWatermark } from "./main-watermark.ts";

const state: Record<string, any> = {};
const store: any = {
  readJson: (_name: string, fallback: any) => state.watermark ?? fallback,
  writeJson: (_name: string, value: any) => { state.watermark = value; },
  audit: (_entry: any) => {},
};
const check = (value: unknown, label: string) => { if (!value) throw new Error(`FAIL ${label}`); console.log(`PASS ${label}`); };

check(recordMainWatermark(store, { sessionKey: "agent:main:main", text: "[语音] 开灯", now: 10 }), "main voice records");
check(state.watermark.source === "voice" && state.watermark.seq === 1, "voice source and sequence persisted");
check(recordMainWatermark(store, { sessionKey: "agent:main:main", text: "你好", channel: "openclaw-weixin", accountId: "18f8cd8761c6-im-bot", now: 11 }), "bound wechat records");
check(state.watermark.source === "wechat" && state.watermark.seq === 2, "wechat channel and account increment sequence");
check(recordMainWatermark(store, { sessionKey: "agent:main:main", text: "你好", channel: "openclaw-weixin", now: 12 }), "missing WeChat account remains observable");
check(state.watermark.source === "unknown", "missing WeChat account fails closed to unknown");
check(!recordMainWatermark(store, { sessionKey: "agent:main:task-a", text: "任务", now: 12 }), "task session excluded");
check(!recordMainWatermark(store, { sessionKey: "agent:main:main", text: "__guanjia_approved_retry__", now: 12 }), "internal retry marker excluded");
console.log("ALL PASS main-watermark.test");
