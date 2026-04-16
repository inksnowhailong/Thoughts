# thoughts-plugin

`thoughts-plugin` is a Claude Code plugin that adds a companion-style workflow with:

- profile onboarding
- generated personality
- prompt-time personality injection hooks
- project-local memory files

## Install

Add the marketplace and install the plugin:

```bash
claude plugin marketplace add inksnowhailong/thoughts-plugin
claude plugin install thoughts@thoughts
```

## Commands

After installation, use:

```text
/thoughts:thoughts
/thoughts:thoughts-onboarding
/thoughts:thoughts-stop
```

## Structure

- `.claude-plugin/plugin.json`: plugin manifest
- `.claude-plugin/marketplace.json`: marketplace manifest
- `skills/`: plugin skills
- `hooks/`: prompt injection hook

## Development

Validate the plugin locally:

```bash
claude plugin validate .
```
TODO
- 资料存全局
- 弹窗消息的对话形式
- git迁移位置
- 每次都携带了用户的画像，这个在上下文中 应该不用每次都输入
