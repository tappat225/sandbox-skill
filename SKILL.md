---
name: sandbox-skill
description: Access a configured AIO Sandbox over its HTTP API without installing the generated SDK. Use for shell commands, file system operations (read/write/search/edit), and browser automation (navigate/click/type/screenshot/extract) inside a remote/containerized Sandbox — for coding, repository inspection, tests, builds, web scraping, or long-running commands.
---

# Sandbox HTTP Skill

Use the bundled `scripts/sandbox_client.py` to access the configured AIO Sandbox. The client uses only Python's standard library and sends the same HTTP requests as the generated SDK. It covers the four MCP server groups AIO Sandbox exposes: **Terminal**, **File**, **Browser**, and **Document conversion** (the Markitdown-equivalent `convert_to_markdown` tool).

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

## File Operations

Paths that do not start with `/` are resolved relative to `exec_dir`.

```text
python "<skill-dir>/scripts/sandbox_client.py" file-read src/app.py --start-line 0 --end-line 50
python "<skill-dir>/scripts/sandbox_client.py" file-write notes.txt --content "hello"
python "<skill-dir>/scripts/sandbox_client.py" file-write notes.txt --content-file ./local_notes.txt --append
python "<skill-dir>/scripts/sandbox_client.py" file-list src --recursive
python "<skill-dir>/scripts/sandbox_client.py" file-search src/app.py "def\\s+\\w+"
python "<skill-dir>/scripts/sandbox_client.py" file-grep . "TODO" --include "*.py" --exclude node_modules
python "<skill-dir>/scripts/sandbox_client.py" file-glob . "**/*.ts"
python "<skill-dir>/scripts/sandbox_client.py" file-find . "*.json"
python "<skill-dir>/scripts/sandbox_client.py" file-replace notes.txt "old text" "new text"
python "<skill-dir>/scripts/sandbox_client.py" file-edit str_replace src/app.py --old-str "foo()" --new-str "bar()"
python "<skill-dir>/scripts/sandbox_client.py" file-edit create src/new_module.py --file-text "print('hi')"
```

`file-edit` mirrors the sandbox `str_replace_editor` tool; `edit_command` is one of `view`, `create`, `str_replace`, `insert`, `undo_edit`.

## Browser Operations

Browser commands act on the Sandbox's shared browser session (one active tab unless you open more with `browser-tabs new`).

```text
python "<skill-dir>/scripts/sandbox_client.py" browser-navigate "https://example.com"
python "<skill-dir>/scripts/sandbox_client.py" browser-click --selector "#submit"
python "<skill-dir>/scripts/sandbox_client.py" browser-type "hello world"
python "<skill-dir>/scripts/sandbox_client.py" browser-text
python "<skill-dir>/scripts/sandbox_client.py" browser-html --outer
python "<skill-dir>/scripts/sandbox_client.py" browser-markdown
python "<skill-dir>/scripts/sandbox_client.py" browser-tabs
python "<skill-dir>/scripts/sandbox_client.py" browser-tabs new --url "https://example.com"
python "<skill-dir>/scripts/sandbox_client.py" browser-evaluate "document.title"
python "<skill-dir>/scripts/sandbox_client.py" browser-screenshot /tmp/page.png
python "<skill-dir>/scripts/sandbox_client.py" browser-screenshot /tmp/full.png --full-page
python "<skill-dir>/scripts/sandbox_client.py" browser-screenshot /tmp/screen.png --full-screen
```

Screenshots are binary responses: the client saves the raw image bytes to the local `output` path you provide (not the Sandbox path) and reports byte count and content type. Use `--full-page` for the page-level (Playwright) screenshot of the full scrollable page, or `--full-screen` for a whole-virtual-screen capture (useful for non-browser GUI content).

## Document Conversion

```text
python "<skill-dir>/scripts/sandbox_client.py" convert-to-markdown "https://example.com"
python "<skill-dir>/scripts/sandbox_client.py" convert-to-markdown reports/output.pdf
```

Give an `http(s)://` or `data:` URI directly, or a Sandbox-side file path (absolute, or relative to `exec_dir`) which is converted to a `file://` URI automatically.

## Safety and Scope

The Sandbox is an execution backend, not a local-shell proxy. Do not use it to run secrets or destructive host commands unless the user explicitly requested that operation and the deployment intentionally mounts the relevant data. Prefer the configured workspace and avoid exposing API keys in command arguments.

Browser and file tools operate on the shared Sandbox environment — navigating the browser or overwriting files affects state other tasks may depend on. Confirm with the user before actions with a wide blast radius (e.g. `file-write` over important files, navigating away from a page mid-task).

For a normal task, use one command at a time, check its exit code (or `success` field for file/browser/document commands), and inspect the resulting files through another Sandbox command. Do not install the generated `agent-sandbox` SDK just to call these endpoints.
