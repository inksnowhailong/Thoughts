import { createLaunchAppTool } from "./ps-tools.ts";

const check = (value: unknown, name: string) => {
  if (!value) throw new Error(`FAIL ${name}`);
  console.log(`PASS ${name}`);
};

let args: string[] = [];
const tool = createLaunchAppTool({
  workspaceDir: "C:\\workspace",
  runPs: async (_script, received) => {
    args = received;
    return { code: 0, stdout: JSON.stringify({ ok: true, app: "music", verified: true }) };
  },
});
const unique = await tool.execute("id", { name: "网易云音乐" });
check(args[1] === "网易云音乐" && unique.details.ok === true && /核实/.test(unique.content[0].text), "natural name stays in fixed launcher path");

const ambiguousTool = createLaunchAppTool({
  workspaceDir: "C:\\workspace",
  runPs: async () => ({ code: 2, stdout: JSON.stringify({ ok: false, error: "ambiguous", candidates: ["steam", "steamvr"] }) }),
});
const ambiguous = await ambiguousTool.execute("id", { name: "ste" });
check(ambiguous.details.ok === false && /可能是/.test(ambiguous.content[0].text), "ambiguous match asks without launch");

const unverifiedTool = createLaunchAppTool({
  workspaceDir: "C:\\workspace",
  runPs: async () => ({ code: 5, stdout: JSON.stringify({ ok: false, error: "unverified", app: "music" }) }),
});
const unverified = await unverifiedTool.execute("id", { name: "music" });
check(unverified.details.ok === false && /不能确认/.test(unverified.content[0].text), "unverified launch is not claimed successful");
console.log("ALL PASS ps-tools.test");
