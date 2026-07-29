---
name: sandbox-skill
description: Execute shell commands in an AIO Sandbox over its HTTP API without installing the generated SDK. Use when a task should run inside a configured remote/containerized Sandbox, especially for coding, repository inspection, tests, builds, file operations, or long-running commands.
---

# Sandbox HTTP Skill

Use the bundled `scripts/sandbox_client.py` to access the configured AIO Sandbox. The client uses only Python's standard library and sends the same HTTP requests as the generated SDK.

## Setup

1. Copy `config.example.json` to `config.json` in this skill directory.
2. Set `base_url` to the Sandbox origin, for example `http://127.0.0.1:8080`.
3. Set `api_key` when the Sandbox has `SANDBOX_API_KEY` enabled.
4. Set `exec_dir` to the mounted workspace, normally `/home/gem/workspace` or `/home/gem`.

Do not print or commit `config.json`. Keep API keys out of commands, logs, and task output.

## Command Workflow

Resolve `<skill-dir>` to the absolute directory containing this `SKILL.md`. Run task commands through the client rather than executing them on the local machine:

```text
python "<skill-dir>/scripts/sandbox_client.py" exec "git status"
python "<skill-dir>/scripts/sandbox_client.py" exec --dir /home/gem/workspace "pytest"
python "<skill-dir>/scripts/sandbox_client.py" health
```

The `exec` command waits for completion by default. It automatically polls `/v1/bash/output` when the server returns `running`. Use `--no-wait` when the task must be handed back immediately; retain the returned `session_id` and `command_id`, then use:

```text
python "<skill-dir>/scripts/sandbox_client.py" output SESSION_ID COMMAND_ID --wait
python "<skill-dir>/scripts/sandbox_client.py" kill SESSION_ID --signal SIGTERM
```

Interpret results as follows:

- `status=completed`: inspect `exit_code`; only `0` is success.
- `status=running`: keep polling or report the active command.
- `status=timed_out` or `status=killed`: report that execution was interrupted and include partial output.
- Preserve both `stdout` and `stderr` when reporting failures.

Use the Sandbox path in `exec_dir`, not a local host path. All filesystem changes made by commands occur in the Sandbox container or in directories explicitly mounted into it.

## Safety and Scope

The Sandbox is an execution backend, not a local-shell proxy. Do not use it to run secrets or destructive host commands unless the user explicitly requested that operation and the deployment intentionally mounts the relevant data. Prefer the configured workspace and avoid exposing API keys in command arguments.

For a normal task, use one command at a time, check its exit code, and inspect the resulting files through another Sandbox command. Do not install the generated `agent-sandbox` SDK just to call these endpoints.
