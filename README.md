# hermes-plugin-skill-openviking-sync

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that automatically syncs agent-created skills to your [OpenViking](https://docs.openviking.ai) knowledge base.

## What it does

Hermes learns from experience by creating local skills (`skill_manage` tool). This plugin watches that process and keeps your OpenViking skills registry in sync:

1. Listens to `post_tool_call` events for `skill_manage` mutations (`create` / `patch` / `write_file`)
2. Locates the affected skill directory under the Hermes skills root
3. Uploads it to OpenViking via `ov add-skill` (idempotent upsert by skill name) in a background thread — never blocks the agent loop
4. Content-hash dedup: unchanged skills are not re-uploaded

Upload failures are logged as warnings only; the agent is never interrupted.

## Requirements

- Hermes Agent
- [`ov` CLI](https://docs.openviking.ai) installed and configured (endpoint + account). If `ov` is missing, the plugin logs a warning and stays inert.

## Install

```bash
hermes plugins install JunfXiao/hermes-plugin-skill-openviking-sync --enable
```

Takes effect on the next Hermes session (restart the desktop app / CLI / gateway).

## Update

```bash
hermes plugins update
```

## Limitations

- Skill **deletions are not propagated** — remove stale skills from OpenViking manually (`ov` / HTTP API `DELETE /api/v1/skills/<name>`).
- Sync is one-way: local Hermes → OpenViking.

## How it works

`__init__.py` registers a `post_tool_call` hook via `ctx.register_hook()`. Hook errors are isolated by Hermes and logged, never crashing the agent. Uploads run in a daemon thread because `post_tool_call` is a timeout-bounded hot-path hook.

## License

MIT
