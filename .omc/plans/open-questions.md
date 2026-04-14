# Open Questions

## user-profile-onboarding - 2026-04-14

- [ ] Onboarding 的 system prompt 具体措辞如何设计？需要引导 AI 收集哪些最低限度的信息（如称呼），还是完全交给 AI 自由发挥？ — 影响 onboarding 对话质量和用户体验
- [ ] `PROFILE_UPDATE` 的 merge 策略：当前计划是 shallow merge（顶层字段覆盖）。如果 AI 输出嵌套结构，是否需要 deep merge？ — 影响画像数据的完整性
- [ ] Onboarding session 是否需要在完成后清理（删除 OpenClaw 侧的 session 上下文）？ — 影响存储占用，但 v1 可暂不处理
- [ ] 画像查看弹窗是否需要支持用户手动编辑？ — v1 建议只读，后续迭代再加编辑
