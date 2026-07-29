#!/usr/bin/env python3
"""Small standard-library HTTP client for the AIO Sandbox bash API."""

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

    def request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = None
        headers = {"Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-AIO-API-Key"] = self.api_key
        request = urllib.request.Request(self.base_url + "/" + path.lstrip("/"), data=payload, headers=headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Sandbox HTTP {exc.code}: {raw[:2000]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Sandbox connection failed: {exc.reason}") from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Sandbox returned non-JSON data") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Sandbox returned an unexpected JSON value")
        return value


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
    return parser


def main() -> int:
    args = build_parser().parse_args()
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
        else:
            result = client.request("/v1/sandbox")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.action == "exec" and result.get("status") == "completed":
            return 0 if result.get("exit_code") == 0 else 1
        if args.action == "exec" and result.get("status") in {"timed_out", "killed"}:
            return 1
        return 0
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
