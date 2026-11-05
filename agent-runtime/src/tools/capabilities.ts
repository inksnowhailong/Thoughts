/**
 * 能力登记表：分级、可见性与执行器声明（Wave1-B 六件套 + screen_understand）。
 *
 * 分级真相来源：openclaw/plugins/guanjia-guard/defaults/levels.json（迁移）
 * 与 openclaw 工具插件描述（ps-tools.ts / speak.ts）；
 * 必须与 Python 侧 src/guanjia/bridge/capability_levels.py 一致。
 */
import path from 'node:path';
import { z } from 'zod';
import { MEDIA_DIR, TOOLS_DIR, resolvePython } from './paths.js';
import type { Capability } from './types.js';

const LAUNCH_APP_PS1 = path.join(TOOLS_DIR, 'launch-app.ps1');
const SCREEN_CAPTURE_PY = path.join(TOOLS_DIR, 'screen-capture.py');
const PY = resolvePython();

/** 与旧 ps-tools.ts 的 APP_NAME_RE 一致：拒绝控制字符，最长 80 码点，不进 shell。 */
const APP_NAME_SCHEMA = z
  .string()
  .min(1, { message: 'name 不能为空' })
  .refine((value) => [...value].length <= 80 && ![...value].some((c) => (c.codePointAt(0) ?? 0) < 32), {
    message: 'name 最长 80 字且不能含控制字符',
  });

export const CAPABILITIES: Capability[] = [
  {
    id: 'launch_app',
    title: 'Launch app',
    description:
      'Launch a game or program registered in apps.json on the home PC (英雄联盟/LOL, Steam, WeGame, 网易云 ...). ' +
      'Input: apps.json key or a natural Chinese/English/pinyin name. Exact aliases win; ' +
      'a unique fallback match stays inside the same controlled map.',
    inputSchema: z.object({ name: APP_NAME_SCHEMA }),
    latencyClass: 'instant',
    sideEffect: 'reversible',
    riskLevel: 'L1',
    mainVisible: true,
    workerVisible: false,
    executor: {
      kind: 'local-process',
      command: [
        'powershell',
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        LAUNCH_APP_PS1,
        '-Name',
        '{{name}}',
      ],
      timeoutMs: 20_000,
    },
  },
  {
    id: 'get_weather',
    title: 'Get weather',
    description:
      'Current weather plus today\'s and tomorrow\'s forecast (temperature, rain chance, whether to bring an umbrella). ' +
      'Input: optional city name (Chinese or pinyin). Without a city it uses the home PC\'s own location.',
    inputSchema: z.object({ city: z.string().optional() }),
    latencyClass: 'instant',
    sideEffect: 'none',
    riskLevel: 'L0',
    mainVisible: true,
    workerVisible: true,
    executor: { kind: 'http-python', path: 'guanjia/bridge/tools_impl.py::get_weather' },
  },
  {
    id: 'get_location',
    title: 'Get location',
    description:
      "Get the home PC's current latitude/longitude from the Windows location service (not IP-based, so a VPN does not move it). " +
      "Falls back to Beijing. Use for weather or 'where am I' questions.",
    inputSchema: z.object({}),
    latencyClass: 'instant',
    sideEffect: 'none',
    riskLevel: 'L0',
    mainVisible: true,
    workerVisible: true,
    executor: { kind: 'http-python', path: 'guanjia/bridge/tools_impl.py::get_location' },
  },
  {
    id: 'speak',
    title: 'Speak aloud',
    description:
      'Speak a short sentence aloud on the home PC speakers in the butler\'s own voice (SCV). ' +
      'Use when the owner asks to read or say something out loud on the computer (念一句 / 读出来 / 在电脑上说). ' +
      'Input: text, up to 300 characters. Fails with a clear reason if the voice daemon is not running, muted, or the owner is gaming. ' +
      'IMPORTANT: the spoken text will also be delivered to the owner\'s ears — do NOT repeat the same sentence in your final reply; ' +
      'keep the final reply empty or say something new, otherwise the owner hears it twice.',
    inputSchema: z.object({ text: z.string().min(1, { message: 'text 不能为空' }).max(300) }),
    latencyClass: 'instant',
    sideEffect: 'reversible',
    riskLevel: 'L1',
    mainVisible: true,
    workerVisible: false,
    executor: { kind: 'http-python', path: 'guanjia/bridge/tools_impl.py::speak' },
  },
  {
    id: 'web_fetch',
    title: 'Fetch web page',
    description:
      'Fetch a web page or JSON API over HTTP(S) and return the extracted text (first 4000 characters). ' +
      'Use for lookups the other tools cannot answer (查一下 / 总结这篇). Input: url.',
    inputSchema: z.object({
      url: z
        .string()
        .url({ message: 'url 必须是合法 URL' })
        .refine((value) => /^https?:\/\//i.test(value), { message: 'url 只支持 http(s)' }),
    }),
    latencyClass: 'instant',
    sideEffect: 'none',
    riskLevel: 'L0',
    mainVisible: true,
    workerVisible: true,
    executor: { kind: 'http-python', path: 'guanjia/bridge/tools_impl.py::web_fetch' },
  },
  {
    id: 'screen_capture',
    title: 'Screenshot the screen',
    description:
      '截取电脑屏幕画面。主人问游戏攻略/屏幕上有什么/帮我看看屏幕时用这个工具' +
      '（特别是游戏模式中——先截屏看清游戏画面再给建议）。返回截图文件路径。',
    inputSchema: z.object({ label: z.string().optional() }),
    latencyClass: 'instant',
    sideEffect: 'none',
    riskLevel: 'L0',
    mainVisible: true,
    workerVisible: true,
    executor: {
      kind: 'local-process',
      command: [PY, '-X', 'utf8', SCREEN_CAPTURE_PY, 'shot', '--label', '{{label}}'],
      timeoutMs: 15_000,
    },
  },
  {
    id: 'screen_understand',
    title: 'Look at the screen',
    description:
      '用视觉模型看一眼电脑屏幕并回答问题：先截屏，再由 VLM 描述/分析画面内容。' +
      '主人问"屏幕上是什么/这个界面什么意思/看看我屏幕"时用这个工具；' +
      'question 填想问的问题（缺省=描述屏幕内容），capture_path 可复用 screen_capture 的已有截图。',
    inputSchema: z.object({
      question: z.string().optional(),
      capture_path: z.string().optional(),
    }),
    latencyClass: 'instant',
    sideEffect: 'none',
    riskLevel: 'L0',
    mainVisible: true,
    workerVisible: true,
    executor: {
      kind: 'builtin-vlm',
      captureCommand: [PY, '-X', 'utf8', SCREEN_CAPTURE_PY, 'shot', '--label', 'vlm'],
      timeoutMs: 15_000,
    },
  },
  {
    id: 'task_start',
    title: 'Start background task',
    description:
      '创建持久化后台任务（预计超过 5 秒的多步任务才用；只排队不执行命令、不下载、不跑模型）。' +
      '定时任务给 when（本地时间，如 2026-10-25T20:00 或 20:00）；' +
      '可验收的任务给 verify（如 {"kind":"process","name":"cloudmusic"} / ' +
      '{"kind":"file","path":"D:/out.zip","min_kb":1} / {"kind":"camera_contains","text":"沙发"} / ' +
      '{"kind":"screen_contains","text":"结算"}）——验收不过任务不算完成。',
    inputSchema: z.object({
      summary: z.string().min(1, { message: 'summary 不能为空' }),
      goal: z.string().min(1, { message: 'goal 不能为空' }),
      when: z.string().optional(),
      verify: z.record(z.string(), z.unknown()).optional(),
    }),
    latencyClass: 'instant',
    sideEffect: 'reversible',
    riskLevel: 'L1',
    mainVisible: true,
    workerVisible: false,
    executor: { kind: 'http-python', path: 'guanjia/bridge/tools_impl.py::task_start' },
  },
  {
    id: 'task_status',
    title: 'Task status',
    description: '查询后台任务的进度与状态。Input: 任务的 id。',
    inputSchema: z.object({ id: z.string().min(1, { message: 'id 不能为空' }) }),
    latencyClass: 'instant',
    sideEffect: 'none',
    riskLevel: 'L1',
    mainVisible: true,
    workerVisible: false,
    executor: { kind: 'http-python', path: 'guanjia/bridge/tools_impl.py::task_status' },
  },
  {
    id: 'task_cancel',
    title: 'Cancel task',
    description: '主人叫停后台任务时用（协作式取消：阻止后续步骤，不硬杀进程）。Input: 任务的 id。',
    inputSchema: z.object({
      id: z.string().min(1, { message: 'id 不能为空' }),
      reason: z.string().optional(),
    }),
    latencyClass: 'instant',
    sideEffect: 'reversible',
    riskLevel: 'L1',
    mainVisible: true,
    workerVisible: false,
    executor: { kind: 'http-python', path: 'guanjia/bridge/tools_impl.py::task_cancel' },
  },
];

/** 截屏产物目录（screen_capture / screen_understand 共用，与 screen-capture.py 一致）。 */
export const SCREEN_MEDIA_DIR = MEDIA_DIR;
