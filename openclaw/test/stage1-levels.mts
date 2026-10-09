// 阶段 1 分级表用例：ls / process 只读动作 / 只读 PowerShell 管道 / format 误伤修正。
// 运行：cd F:\coding\guanjia\openclaw && npx --yes tsx test\stage1-levels.mts
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { classify, type LevelsFile } from "../plugins/guanjia-guard/src/levels";

const pluginDir = path.resolve(import.meta.dirname, "../plugins/guanjia-guard");
const levels = JSON.parse(fs.readFileSync(path.join(pluginDir, "defaults/levels.json"), "utf8")) as LevelsFile;
const lv = (toolName: string, params: Record<string, unknown>) => classify(levels, { toolName, params }, false).level;
const sh = (command: string) => lv("exec", { command });

// ls 与 process 的只读动作
assert.equal(lv("ls", { path: "C:\\Users" }), "L0", "ls");
const readActions = ["list", "poll", "log"];
const writeActions = ["kill", "write", "send-keys", "submit", "paste", "clear", "remove"];
for (const action of readActions) assert.equal(lv("process", { action, sessionId: "s" }), "L0", `process ${action}`);
for (const action of writeActions) assert.equal(lv("process", { action, sessionId: "s" }), "L2", `process ${action} 仍要确认`);

// 只读 PowerShell 管道：放行
const allow = [
  "Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 Name,CPU",
  "tasklist | findstr /i notepad",
  'tasklist | findstr /i "^t"',
  "Get-Service | Where-Object Status -eq Running | Format-Table -AutoSize",
  "Get-CimInstance Win32_OperatingSystem | Select-Object LastBootUpTime",
  "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime",
  'powershell -NoProfile -Command "Get-Process notepad | Select-Object Id,Name"',
  'powershell -NoProfile -Command "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime"',
  "Get-ChildItem C:\\Users -Directory -Force | Select-Object Name",
  "Get-Volume | Format-List",
  "Get-Date -Format yyyy-MM-dd",
  "Get-Content C:\\Users\\inksn\\notes.txt | Select-String -Pattern 电费",
  "dir C:\\", "echo hello", "ipconfig", "ipconfig /all", "ipconfig /all | findstr IPv4",
  'powershell -NoProfile -Command "Get-Date"',
  "type C:\\Users\\inksn\\.openclaw\\workspace\\AGENTS.md",
  "dir C:\\Users",
  "Get-Date",
];
for (const c of allow) assert.equal(sh(c), "L0", `应放行：${c}`);

// 仍然拦住：危险结构一律落到 L3
const deny = [
  "Get-Process | Stop-Process",
  "Get-ChildItem C:\\tmp | Remove-Item",
  "Get-Process | ForEach-Object Kill",
  "Get-ChildItem | ForEach-Object { $_.Delete() }",
  "Get-Content a.txt | Set-Content b.txt",
  "Get-Process | Out-File C:\\p.txt",
  "Get-Process > C:\\p.txt",
  "Get-Date; notepad",
  "Get-Date && notepad",
  "Get-ChildItem $env:TEMP",
  "Get-Item x | Invoke-Item",
  "(Get-Item C:\\x.txt).Delete()",
  "Get-Process | Select-Object @{n='x';e={1}}",
  "Get-Process-Evil",
  "iex (Get-Content a.ps1)",
  "Get-Content a.ps1 | iex",
  "powershell -NoProfile -EncodedCommand ZQBjAGgAbwA=",
  "Get-WmiObject Win32_Process | Invoke-WmiMethod -Name Terminate",
  "tasklist | findstr notepad > out.txt",
  "cmd /c tasklist | findstr x",
  "format D: /q",
  "Format-Volume -DriveLetter D",
  "Get-ChildItem C:\\ | Remove-Item -Recurse",
  "echo (iex ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('R2V0LURhdGU='))))",
  "Get-Content (New-Item C:\\Users\\inksn\\x.txt -Force)",
  "type (Set-Content C:\\Users\\inksn\\.openclaw\\workspace\\guanjia\\levels.json x)",
  "dir (Copy-Item C:\\Users\\inksn\\.openclaw\\openclaw.json C:\\Users\\Public\\k.txt)",
  "Get-Date @(1)", "Get-Date [x]",
  'Get-Content C:\\w\\ftp.txt | ft"p" -n -i',
  'Get-Date | fl"tmc" unload WdFilter',
  'type"perf" -cf c.txt -o C:\\Users\\inksn\\.openclaw\\openclaw.json -y',
  'powershell -NoProfile -Command "Get-Content C:\\w\\f.txt | ft"p" -n"',
  '(type"perf" -qx)',
  "ipconfig /release", "ipconfig /renew | findstr IPv4", "ipconfig /all /release", "ipconfig -flushdns",
  "type C:\\Users\\inksn\\.openclaw\\openclaw.json",
  "Get-Content C:\\Users\\inksn\\.openclaw\\openclaw.json | Select-String apiKey",
  "Get-Item Env:PATH",
  "Test-Path \\\\evil\\x",
  "dir C:\\Users\\inksn\\.openclaw\\openclaw-weixin",
  "type C:\\Users\\inksn\\.openclaw\\openclaw.json.bak-stage1-20261009",
];
for (const c of deny) assert.equal(sh(c), "L3", `应拦住：${c}`);

const sensitiveRead: Array<[string, "L0" | "L3"]> = [
  ["C:\\Users\\inksn\\.openclaw\\openclaw.json", "L3"],
  ["C:\\Users\\inksn\\.openclaw\\workspace\\guanjia\\family-facts.json", "L3"],
  ["C:\\Users\\inksn\\.openclaw\\workspace\\memory\\timeline.md", "L0"],
];
for (const [filePath, level] of sensitiveRead) assert.equal(lv("read", { path: filePath }), level, `read ${filePath}`);

console.log(`PASS stage1 levels (${1 + readActions.length + writeActions.length + allow.length + deny.length + sensitiveRead.length} cases)`);
