import fs from "node:fs";
import path from "node:path";

/**
 * workspace/guanjia/ 下的小型 JSON 存储 + 审计日志。
 * 所有 S3 自有状态都放这里，不碰 S2 的 workspace/memory/。
 */
export type Store = ReturnType<typeof createStore>;

/**
 * 创建存储句柄。
 * @param workspaceDir OpenClaw 工作区根目录
 * @param defaultsDir 插件自带默认文件目录（levels.json / family-facts.json 首次缺失时复制）
 */
export function createStore(workspaceDir: string, defaultsDir: string) {
  const dir = path.join(workspaceDir, "guanjia");
  fs.mkdirSync(dir, { recursive: true });

  // 首次启动：把默认分级表与家庭事实模板铺进工作区，之后以工作区文件为准（用户可直接编辑）
  for (const name of ["levels.json", "family-facts.json"]) {
    const target = path.join(dir, name);
    if (!fs.existsSync(target)) fs.copyFileSync(path.join(defaultsDir, name), target);
  }

  const file = (name: string) => path.join(dir, name);

  /** 读 JSON；文件缺失或损坏时返回 fallback（损坏会记一条审计，方便发现手改出错） */
  function readJson<T>(name: string, fallback: T): T {
    try {
      return JSON.parse(fs.readFileSync(file(name), "utf8").trimStart()) as T; // trimStart 顺带去掉 BOM
    } catch (err: any) {
      if (err?.code !== "ENOENT") audit({ kind: "store-error", file: name, error: String(err?.message ?? err) });
      return fallback;
    }
  }

  /** 原子写 JSON：先写临时文件再 rename，避免写一半被读到 */
  function writeJson(name: string, data: unknown) {
    const tmp = file(name) + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2), "utf8");
    fs.renameSync(tmp, file(name));
  }

  /** 追加一条审计记录到 audit.jsonl（调用方负责不放原文） */
  function audit(entry: Record<string, unknown>) {
    fs.appendFileSync(file("audit.jsonl"), JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n", "utf8");
  }

  return { dir, file, readJson, writeJson, audit };
}
