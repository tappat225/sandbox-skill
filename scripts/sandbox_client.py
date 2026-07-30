#!/usr/bin/env python3
"""Small standard-library HTTP client for the AIO Sandbox HTTP API.

Covers the Terminal (bash), File, Browser, and Document-conversion
("Markitdown"-equivalent) MCP server groups exposed by AIO Sandbox.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR.parent / "config.json"


def load_config(path: str | None) -> dict[str, Any]:
    config_path = Path(path or os.environ.get("SANDBOX_SKILL_CONFIG", DEFAULT_CONFIG))
    if not config_path.exists():
        raise RuntimeError(f"Missing config: {config_path}. Copy config.example.json to config.json first.")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read config: {config_path}") from exc
    if not isinstance(data, dict) or not data.get("base_url"):
        raise RuntimeError("Config must contain a non-empty base_url")
    return data


class SandboxHttp:
    def __init__(self, config: dict[str, Any]):
        self.base_url = str(config["base_url"]).rstrip("/")
        self.api_key = str(config.get("api_key") or "")
        self.request_timeout = float(config.get("request_timeout", 120))
        self.poll_wait = float(config.get("poll_wait", 20))

    def _build_url(self, path: str, query: dict[str, Any] | None) -> str:
        url = self.base_url + "/" + path.lstrip("/")
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                # bool -> lowercase string so servers see "true"/"false"
                encoded = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in clean.items()}
                url += "?" + urllib.parse.urlencode(encoded)
        return url

    def _open(self, path: str, *, method: str, body: dict[str, Any] | None = None, query: dict[str, Any] | None = None):
        payload = None
        headers = {"Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-AIO-API-Key"] = self.api_key
        url = self._build_url(path, query)
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            return urllib.request.urlopen(request, timeout=self.request_timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Sandbox HTTP {exc.code}: {raw[:2000]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Sandbox connection failed: {exc.reason}") from exc

    def request(self, path: str, body: dict[str, Any] | None = None, *, query: dict[str, Any] | None = None, method: str | None = None) -> dict[str, Any]:
        """Call a JSON endpoint. Uses POST when a body is given, GET otherwise."""
        resolved_method = method or ("POST" if body is not None else "GET")
        with self._open(path, method=resolved_method, body=body, query=query) as response:
            raw = response.read().decode("utf-8")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Sandbox returned non-JSON data") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Sandbox returned an unexpected JSON value")
        return value

    def request_binary(self, path: str, *, query: dict[str, Any] | None = None, method: str = "GET", body: dict[str, Any] | None = None) -> tuple[bytes, str]:
        """Call an endpoint that returns raw bytes (e.g. screenshots). Returns (data, content_type)."""
        with self._open(path, method=method, body=body, query=query) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "application/octet-stream")
        return data, content_type


def data_of(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Sandbox response has no data object")
    return data


def command_result(response: dict[str, Any]) -> dict[str, Any]:
    data = data_of(response)
    return {
        "success": response.get("success"),
        "message": response.get("message"),
        "session_id": data.get("session_id"),
        "command_id": data.get("command_id"),
        "command": data.get("command"),
        "status": data.get("status"),
        "stdout": data.get("stdout") or "",
        "stderr": data.get("stderr") or "",
        "exit_code": data.get("exit_code"),
        "offset": data.get("offset"),
        "stderr_offset": data.get("stderr_offset"),
    }


def run_exec(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"command": args.command}
    body["exec_dir"] = args.directory or config.get("exec_dir")
    body["async_mode"] = args.no_wait
    body["timeout"] = args.timeout if args.timeout is not None else config.get("sync_wait", 30)
    body["hard_timeout"] = args.hard_timeout if args.hard_timeout is not None else config.get("hard_timeout")
    body["max_output_length"] = args.max_output if args.max_output is not None else config.get("max_output_length")
    body = {key: value for key, value in body.items() if value is not None}
    result = command_result(client.request("/v1/bash/exec", body))
    if args.no_wait or result["status"] != "running":
        return result

    stdout = result["stdout"]
    stderr = result["stderr"]
    offset = result.get("offset") or len(stdout.encode("utf-8"))
    stderr_offset = result.get("stderr_offset") or len(stderr.encode("utf-8"))
    while result["status"] == "running":
        response = client.request(
            "/v1/bash/output",
            {
                "session_id": result["session_id"],
                "command_id": result["command_id"],
                "offset": offset,
                "stderr_offset": stderr_offset,
                "wait": True,
                "wait_timeout": client.poll_wait,
            },
        )
        data = data_of(response)
        stdout += data.get("stdout") or ""
        stderr += data.get("stderr") or ""
        offset = data.get("offset", offset)
        stderr_offset = data.get("stderr_offset", stderr_offset)
        command = data.get("command") or {}
        result.update(
            status=command.get("status", result["status"]),
            exit_code=command.get("exit_code"),
            offset=offset,
            stderr_offset=stderr_offset,
            stdout=stdout,
            stderr=stderr,
        )
    return result


def _resolve_path(client_path: str | None, config: dict[str, Any]) -> str:
    """Resolve a possibly-relative path against the configured exec_dir."""
    if client_path and client_path.startswith("/"):
        return client_path
    exec_dir = str(config.get("exec_dir") or "").rstrip("/")
    if not client_path:
        return exec_dir
    return f"{exec_dir}/{client_path}"


def run_file_read(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "file": _resolve_path(args.file, config),
        "start_line": args.start_line,
        "end_line": args.end_line,
        "sudo": args.sudo,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/file/read", body)


def run_file_write(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    if args.content_file:
        content = Path(args.content_file).read_text(encoding="utf-8")
    else:
        content = args.content or ""
    body: dict[str, Any] = {
        "file": _resolve_path(args.file, config),
        "content": content,
        "encoding": args.encoding,
        "append": args.append,
        "leading_newline": args.leading_newline,
        "trailing_newline": args.trailing_newline,
        "sudo": args.sudo,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/file/write", body)


def run_file_list(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "path": _resolve_path(args.path, config),
        "recursive": args.recursive,
        "show_hidden": args.show_hidden,
        "max_depth": args.max_depth,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/file/list", body)


def run_file_search(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "file": _resolve_path(args.file, config),
        "regex": args.regex,
        "sudo": args.sudo,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/file/search", body)


def run_file_grep(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "path": _resolve_path(args.path, config),
        "pattern": args.pattern,
        "include": args.include,
        "exclude": args.exclude,
        "case_insensitive": args.case_insensitive,
        "fixed_strings": args.fixed_strings,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/file/grep", body)


def run_file_glob(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "path": _resolve_path(args.path, config),
        "pattern": args.pattern,
        "exclude": args.exclude,
        "include_hidden": args.include_hidden,
        "files_only": args.files_only,
        "max_results": args.max_results,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/file/glob", body)


def run_file_find(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body = {"path": _resolve_path(args.path, config), "glob": args.glob}
    return client.request("/v1/file/find", body)


def run_file_replace(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "file": _resolve_path(args.file, config),
        "old_str": args.old_str,
        "new_str": args.new_str,
        "sudo": args.sudo,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/file/replace", body)


def run_file_edit(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "command": args.edit_command,
        "path": _resolve_path(args.path, config),
        "file_text": args.file_text,
        "old_str": args.old_str,
        "new_str": args.new_str,
        "insert_line": args.insert_line,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/file/str_replace_editor", body)


def run_browser_navigate(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"url": args.url, "wait_until": args.wait_until, "timeout": args.timeout}
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/browser/page/navigate", body)


def run_browser_screenshot(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    path = "/v1/browser/page/screenshot" if not args.full_screen else "/v1/browser/screenshot"
    query: dict[str, Any] = {"format": args.format}
    if args.full_screen:
        query["quality"] = args.quality
    else:
        query["full_page"] = args.full_page
        query["quality"] = args.quality
    data, content_type = client.request_binary(path, query=query)
    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return {
        "success": True,
        "message": "Screenshot saved",
        "content_type": content_type,
        "bytes": len(data),
        "output": str(output_path),
    }


def run_browser_click(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "selector": args.selector,
        "index": args.index,
        "x": args.x,
        "y": args.y,
        "button": args.button,
        "click_count": args.click_count,
    }
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/browser/page/click", body)


def run_browser_type(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"text": args.text, "delay": args.delay}
    body = {k: v for k, v in body.items() if v is not None}
    return client.request("/v1/browser/page/type", body)


def run_browser_text(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    return client.request("/v1/browser/page/text")


def run_browser_html(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    return client.request("/v1/browser/page/html", query={"outer": args.outer}, method="GET")


def run_browser_markdown(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    return client.request("/v1/browser/page/markdown")


def run_browser_tabs(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    if args.tabs_action == "new":
        body: dict[str, Any] = {"url": args.url} if args.url else {}
        return client.request("/v1/browser/tabs", body)
    return client.request("/v1/browser/tabs")


def run_browser_evaluate(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    return client.request("/v1/browser/page/evaluate", {"expression": args.expression})


def run_convert_to_markdown(client: SandboxHttp, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    uri = args.uri
    if not uri.startswith(("http://", "https://", "file://", "data:")):
        uri = "file://" + _resolve_path(uri, config)
    return client.request("/v1/util/convert_to_markdown", {"uri": uri})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HTTP client for AIO Sandbox")
    parser.add_argument("--config", help="Path to config.json")
    sub = parser.add_subparsers(dest="action", required=True)

    exec_parser = sub.add_parser("exec", help="Execute a bash command")
    exec_parser.add_argument("command")
    exec_parser.add_argument("--dir", dest="directory")
    exec_parser.add_argument("--timeout", type=float)
    exec_parser.add_argument("--hard-timeout", type=float)
    exec_parser.add_argument("--max-output", type=int)
    exec_parser.add_argument("--no-wait", action="store_true")

    output_parser = sub.add_parser("output", help="Read output from an async command")
    output_parser.add_argument("session_id")
    output_parser.add_argument("command_id")
    output_parser.add_argument("--offset", type=int)
    output_parser.add_argument("--stderr-offset", type=int)
    output_parser.add_argument("--wait", action="store_true")
    output_parser.add_argument("--wait-timeout", type=float)

    kill_parser = sub.add_parser("kill", help="Terminate a bash session")
    kill_parser.add_argument("session_id")
    kill_parser.add_argument("--signal", default="SIGTERM", choices=["SIGINT", "SIGTERM", "SIGKILL"])
    sub.add_parser("health", help="Check Sandbox health")

    # --- File tools ---
    file_read_parser = sub.add_parser("file-read", help="Read a file (optionally a line range)")
    file_read_parser.add_argument("file", help="Absolute or exec_dir-relative path")
    file_read_parser.add_argument("--start-line", type=int)
    file_read_parser.add_argument("--end-line", type=int)
    file_read_parser.add_argument("--sudo", action="store_true", default=None)

    file_write_parser = sub.add_parser("file-write", help="Write (or append) content to a file")
    file_write_parser.add_argument("file", help="Absolute or exec_dir-relative path")
    write_content = file_write_parser.add_mutually_exclusive_group(required=True)
    write_content.add_argument("--content", help="Literal content to write")
    write_content.add_argument("--content-file", help="Local file whose contents will be uploaded")
    file_write_parser.add_argument("--encoding", choices=["utf-8", "base64"], default=None)
    file_write_parser.add_argument("--append", action="store_true", default=None)
    file_write_parser.add_argument("--leading-newline", action="store_true", default=None)
    file_write_parser.add_argument("--trailing-newline", action="store_true", default=None)
    file_write_parser.add_argument("--sudo", action="store_true", default=None)

    file_list_parser = sub.add_parser("file-list", help="List a directory")
    file_list_parser.add_argument("path", nargs="?", default=None)
    file_list_parser.add_argument("--recursive", action="store_true", default=None)
    file_list_parser.add_argument("--show-hidden", action="store_true", default=None)
    file_list_parser.add_argument("--max-depth", type=int)

    file_search_parser = sub.add_parser("file-search", help="Search a single file's content with a regex")
    file_search_parser.add_argument("file")
    file_search_parser.add_argument("regex")
    file_search_parser.add_argument("--sudo", action="store_true", default=None)

    file_grep_parser = sub.add_parser("file-grep", help="Grep across a file or directory tree")
    file_grep_parser.add_argument("path")
    file_grep_parser.add_argument("pattern")
    file_grep_parser.add_argument("--include", action="append")
    file_grep_parser.add_argument("--exclude", action="append")
    file_grep_parser.add_argument("--case-insensitive", action="store_true", default=None)
    file_grep_parser.add_argument("--fixed-strings", action="store_true", default=None)

    file_glob_parser = sub.add_parser("file-glob", help="Find files by glob pattern")
    file_glob_parser.add_argument("path")
    file_glob_parser.add_argument("pattern")
    file_glob_parser.add_argument("--exclude", action="append")
    file_glob_parser.add_argument("--include-hidden", action="store_true", default=None)
    file_glob_parser.add_argument("--files-only", action="store_true", default=None)
    file_glob_parser.add_argument("--max-results", type=int)

    file_find_parser = sub.add_parser("file-find", help="Find files by filename glob under a directory")
    file_find_parser.add_argument("path")
    file_find_parser.add_argument("glob")

    file_replace_parser = sub.add_parser("file-replace", help="Replace a literal string in a file")
    file_replace_parser.add_argument("file")
    file_replace_parser.add_argument("old_str")
    file_replace_parser.add_argument("new_str")
    file_replace_parser.add_argument("--sudo", action="store_true", default=None)

    file_edit_parser = sub.add_parser("file-edit", help="str_replace_editor: view/create/str_replace/insert/undo_edit")
    file_edit_parser.add_argument("edit_command", choices=["view", "create", "str_replace", "insert", "undo_edit"])
    file_edit_parser.add_argument("path")
    file_edit_parser.add_argument("--file-text", help="Content for `create`")
    file_edit_parser.add_argument("--old-str", help="String to replace for `str_replace`")
    file_edit_parser.add_argument("--new-str", help="Replacement/insert text")
    file_edit_parser.add_argument("--insert-line", type=int, help="0-based line for `insert`")

    # --- Browser tools ---
    browser_navigate_parser = sub.add_parser("browser-navigate", help="Navigate the active tab to a URL")
    browser_navigate_parser.add_argument("url")
    browser_navigate_parser.add_argument("--wait-until", choices=["load", "domcontentloaded", "networkidle", "commit"])
    browser_navigate_parser.add_argument("--timeout", type=float)

    browser_screenshot_parser = sub.add_parser("browser-screenshot", help="Capture a screenshot and save it locally")
    browser_screenshot_parser.add_argument("output", help="Local file path to save the image to")
    browser_screenshot_parser.add_argument("--full-page", action="store_true", default=None, help="Capture the full scrollable page (page-level screenshot)")
    browser_screenshot_parser.add_argument("--full-screen", action="store_true", help="Capture the whole virtual screen instead of just the page")
    browser_screenshot_parser.add_argument("--format", default="png", choices=["png", "jpg", "jpeg"])
    browser_screenshot_parser.add_argument("--quality", type=int, help="JPEG quality 0-100")

    browser_click_parser = sub.add_parser("browser-click", help="Click an element or coordinate")
    browser_click_parser.add_argument("--selector")
    browser_click_parser.add_argument("--index", type=int)
    browser_click_parser.add_argument("--x", type=float)
    browser_click_parser.add_argument("--y", type=float)
    browser_click_parser.add_argument("--button", default=None, choices=["left", "right", "middle"])
    browser_click_parser.add_argument("--click-count", type=int)

    browser_type_parser = sub.add_parser("browser-type", help="Type text into the focused element")
    browser_type_parser.add_argument("text")
    browser_type_parser.add_argument("--delay", type=float, help="Per-character delay in ms")

    sub.add_parser("browser-text", help="Get the visible text of the current page")
    browser_html_parser = sub.add_parser("browser-html", help="Get the HTML of the current page")
    browser_html_parser.add_argument("--outer", action="store_true", default=None)
    sub.add_parser("browser-markdown", help="Get the current page converted to markdown")

    browser_tabs_parser = sub.add_parser("browser-tabs", help="List tabs, or open a new one")
    browser_tabs_parser.add_argument("tabs_action", choices=["list", "new"], nargs="?", default="list")
    browser_tabs_parser.add_argument("--url", help="URL to open when tabs_action=new")

    browser_evaluate_parser = sub.add_parser("browser-evaluate", help="Evaluate a JS expression on the page")
    browser_evaluate_parser.add_argument("expression")

    # --- Document conversion (Markitdown-equivalent) ---
    convert_parser = sub.add_parser("convert-to-markdown", help="Convert a local/remote document to markdown")
    convert_parser.add_argument("uri", help="http(s) URL, data: URI, or a exec_dir-relative/absolute file path")

    return parser


def main() -> int:
    args = build_parser().parse_args()
    dispatch = {
        "file-read": run_file_read,
        "file-write": run_file_write,
        "file-list": run_file_list,
        "file-search": run_file_search,
        "file-grep": run_file_grep,
        "file-glob": run_file_glob,
        "file-find": run_file_find,
        "file-replace": run_file_replace,
        "file-edit": run_file_edit,
        "browser-navigate": run_browser_navigate,
        "browser-screenshot": run_browser_screenshot,
        "browser-click": run_browser_click,
        "browser-type": run_browser_type,
        "browser-text": run_browser_text,
        "browser-html": run_browser_html,
        "browser-markdown": run_browser_markdown,
        "browser-tabs": run_browser_tabs,
        "browser-evaluate": run_browser_evaluate,
        "convert-to-markdown": run_convert_to_markdown,
    }
    try:
        config = load_config(args.config)
        client = SandboxHttp(config)
        if args.action == "exec":
            result = run_exec(client, args, config)
        elif args.action == "output":
            body = {"session_id": args.session_id, "command_id": args.command_id}
            for key, value in (("offset", args.offset), ("stderr_offset", args.stderr_offset), ("wait", args.wait), ("wait_timeout", args.wait_timeout)):
                if value is not None:
                    body[key] = value
            result = client.request("/v1/bash/output", body)
        elif args.action == "kill":
            result = client.request("/v1/bash/kill", {"session_id": args.session_id, "signal": args.signal})
        elif args.action in dispatch:
            result = dispatch[args.action](client, args, config)
        else:
            result = client.request("/v1/sandbox")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.action == "exec" and result.get("status") == "completed":
            return 0 if result.get("exit_code") == 0 else 1
        if args.action == "exec" and result.get("status") in {"timed_out", "killed"}:
            return 1
        if args.action in dispatch and result.get("success") is False:
            return 1
        return 0
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
