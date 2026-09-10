# hermes-plugin-skill-openviking-sync

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that automatically syncs agent-created skills to your [OpenViking](https://docs.openviking.ai) knowledge base.

## What it does

Hermes learns from experience by creating local skills (`skill_manage` tool). This plugin watches that process and keeps your OpenViking skills registry in sync:

1. Listens to `post_tool_call` events for `skill_manage` mutations (`create` / `patch` / `write_file`)
2. Locates the affected skill directory under the Hermes skills root
3. Zips the skill directory and uploads it to OpenViking via the **HTTP API** (idempotent upsert by skill name) in a background thread — never blocks the agent loop
4. Content-hash dedup: unchanged skills are not re-uploaded

Upload failures are logged as warnings only; the agent is never interrupted.

## Requirements

- Hermes Agent
- **No `ov` CLI needed** — the plugin talks to the OpenViking HTTP server directly.
- OpenViking connection config: the plugin reads the same `ovcli.conf` file Hermes points at (config.yaml `openviking.ovcli_config_path`, env `OVCLI_CONFIG_PATH`, or the default `~/.openviking/ovcli.conf` locations). It extracts `url` + `api_key` from there. If no config is found, the plugin logs a warning and stays inert.

## Install

```bash
hermes plugins install JunfXiao/hermes-plugin-skill-openviking-sync --enable
```

Takes effect on the next Hermes session (restart the desktop app / CLI / gateway).

## Update

```bash
hermes plugins update
```

## Upload pipeline

1. **Local zip**: the skill directory is zipped into a temp file on disk (always removed in a `finally` block, even on failure)
2. **Stale temp cleanup**: before a new attempt, any temp file left behind by a previous failed attempt for the same skill is deleted best-effort
3. **`POST /api/v1/resources/temp_upload`** (multipart) → returns a `temp_file_id`
4. **`POST /api/v1/skills`** with `{temp_file_id, wait: true, timeout: 120}` → upserts the skill
5. **Post-success**: the temp file record is dropped from the local registry (server-side GC handles the rest)

## Retry & error handling

| Failure type | Behavior |
|---|---|
| Network errors (`URLError`, timeouts, connection resets) | **Retried** — up to 3 attempts with exponential backoff (1s → 2s → 4s) |
| HTTP 5xx / 429 | **Retried** — same backoff |
| HTTP 4xx (401 auth, validation, etc.) | **Not retried** — deterministic failure, logged as warning immediately |

Every retry is logged with the attempt number and next delay. The final failure (retryable exhausted or non-retryable) is logged as a `warning` — the agent loop is never blocked or crashed.

## Limitations

- Skill **deletions are not propagated** — remove stale skills from OpenViking manually (HTTP API `DELETE /api/v1/skills/<name>`, no `ov` CLI needed).
- Sync is one-way: local Hermes → OpenViking.

## How it works

`__init__.py` registers a `post_tool_call` hook via `ctx.register_hook()`. Hook errors are isolated by Hermes and logged, never crashing the agent. Uploads run in a daemon thread because `post_tool_call` is a timeout-bounded hot-path hook.

## License

MIT
