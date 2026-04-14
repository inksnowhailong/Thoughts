# Open Questions

## user-profile-onboarding - 2026-04-14

- [ ] Onboarding 的 system prompt 具体措辞如何设计？需要引导 AI 收集哪些最低限度的信息（如称呼），还是完全交给 AI 自由发挥？ — 影响 onboarding 对话质量和用户体验
- [ ] `PROFILE_UPDATE` 的 merge 策略：当前计划是 shallow merge（顶层字段覆盖）。如果 AI 输出嵌套结构，是否需要 deep merge？ — 影响画像数据的完整性
- [ ] Onboarding session 是否需要在完成后清理（删除 OpenClaw 侧的 session 上下文）？ — 影响存储占用，但 v1 可暂不处理
- [ ] 画像查看弹窗是否需要支持用户手动编辑？ — v1 建议只读，后续迭代再加编辑

## thoughts-mode-skill - 2026-04-14

- [ ] Hook 注入的 memory.md 截取多少行合适？当前计划 tail -50，但上下文窗口消耗需要实测确认 — 影响 AI 回复质量和 token 消耗
- [ ] personality.json 的随机生成边界：是否需要预设几个"人格模板"供随机组合，还是完全让 AI 自由发挥？ — 影响人格质量的可控性
- [ ] Cron 主动聊天的表现形式：Claude Code 中 Cron 触发时是否会直接在终端输出？用户不在终端时的行为是什么？ — 需要实测 CronCreate 的实际行为
- [ ] 潜意识 Agent 的触发时机：是在主 SKILL.md 的 prompt 中指示"每次对话后启动 Agent"，还是通过其他机制？主 Skill 只在 /thoughts 调用时执行一次，后续普通对话不会再走 SKILL.md — 这是一个关键的架构问题，需要确认 Hook 能否触发 Agent 调用
- [ ] .thoughts/ 目录是否应该加入 .gitignore？包含用户隐私数据 — 影响数据安全
