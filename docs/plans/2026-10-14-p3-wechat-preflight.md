# P3 微信真机前只读验收

运行：`F:\coding\guanjia\.venv\Scripts\python.exe F:\coding\guanjia\scripts\p3-wechat-preflight.py`

脚本只读取 `openclaw.json`，不连接微信、不发消息、不改配置或绑定。它检查：
- openclaw-weixin 插件的配置与 enabled；
- 主人微信并入 `agent:main:main` 的 binding；
- owner/commands allowFrom、审批路由模式、guard conversation hook；
- 未来微信水位、主人主线与家人支线的预期键；
- reset 后 delivery/注入观察点及确认类消息的审计观察点。

## 主人最少真机动作（上线后才做）

1. 主人微信向机器人发“水位验收”。证据：Guard 审计有 `main-watermark activity`，watermark `source=wechat`、`seq` 递增；回复只回主人。
2. 家人账号发一句普通消息。证据：路由为家人支线，主线 watermark 不变，主线 `lastInteractionAt` 不前移。
3. 主人语音和主人微信各发一次短句。证据：两者均落主线，watermark 分别为 `voice`/`wechat`，不向微信误投语音回复。
4. 仅在已配置 owner/allowFrom 后，让家人发送一次确认类消息。证据：Guard 审计显示准确 `ctx.sessionKey`、channel/accountId 和审批投递目标；不批准任何破坏性实际操作。
5. reset 功能以后另行上线时，再做 reset 后投递和未消费注入的观察；本 preflight 不调用 reset。
