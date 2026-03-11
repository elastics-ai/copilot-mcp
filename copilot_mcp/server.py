#!/usr/bin/env python3
"""
GitHub Copilot MCP Server
Exposes GitHub Copilot AI capabilities (all models) as MCP tools for Codex CLI.
Auth: Uses 'gh auth token' or GITHUB_TOKEN — no separate Copilot token exchange needed.
"""

import asyncio
import json
import logging
import os
import shlex
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("copilot-mcp")

COPILOT_API_BASE = "https://api.githubcopilot.com"
DEFAULT_MODEL = os.environ.get("COPILOT_MODEL", "claude-sonnet-4.6")

_COPILOT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version": "vscode/1.85.0",
    "Editor-Plugin-Version": "copilot-chat/0.12.0",
}


def get_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    raise RuntimeError(
        "No GitHub token found. Set GITHUB_TOKEN env var or run 'gh auth login'."
    )


def auth_headers() -> dict[str, str]:
    return {**_COPILOT_HEADERS, "Authorization": f"Bearer {get_github_token()}"}


async def chat_completion(
    messages: list[dict[str, str]],
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{COPILOT_API_BASE}/chat/completions",
            headers=auth_headers(),
            json={
                "model": model or DEFAULT_MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            },
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Agent tool definitions — these are exposed to the Copilot model during
# the agent loop so it can autonomously read/write files and run commands.
# ---------------------------------------------------------------------------

_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"},
                    "content": {"type": "string", "description": "Full file content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command and return stdout + stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories at a path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (defaults to '.')"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": (
                "Call this ONLY when the task is fully done. "
                "Provide a concise summary of every file changed and every command run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Summary of all work completed",
                    }
                },
                "required": ["summary"],
            },
        },
    },
    # ---- GitHub / Git tools ------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "git_clone",
            "description": "Clone a GitHub repository into a local directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository in 'owner/repo' format (e.g. elastics-ai/my-service)",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Local directory to clone into (defaults to repo name)",
                    },
                },
                "required": ["repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_checkout_branch",
            "description": (
                "Create and switch to a new feature branch. "
                "NEVER use 'main' or 'master' as the branch name — those are protected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "New feature branch name (e.g. 'copilot/fix-auth-error')",
                    },
                },
                "required": ["branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit_all",
            "description": "Stage all changes (git add -A) and commit them with a message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_push",
            "description": "Push the current branch to the remote origin.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "Branch name to push (if omitted, uses current branch)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_pull_request",
            "description": (
                "Open a Pull Request on GitHub using the gh CLI. "
                "The branch must already be pushed to origin before calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository in 'owner/repo' format (e.g. elastics-ai/my-service)",
                    },
                    "title": {"type": "string", "description": "PR title"},
                    "body": {"type": "string", "description": "PR description / body"},
                    "base": {
                        "type": "string",
                        "description": "Base branch to merge into (defaults to 'main')",
                    },
                    "draft": {
                        "type": "boolean",
                        "description": "Open as a draft PR (default: false)",
                    },
                },
                "required": ["repo", "title", "body"],
            },
        },
    },
]


def _exec_read_file(args: dict, cwd: str) -> str:
    path = Path(args["path"])
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        text = path.read_text(errors="replace")
        if len(text) > 20_000:
            text = text[:20_000] + "\n... [truncated at 20,000 chars]"
        return text
    except Exception as e:
        return f"Error reading file: {e}"


def _exec_write_file(args: dict, cwd: str) -> str:
    path = Path(args["path"])
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"])
        return f"Written {len(args['content'])} chars to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


def _exec_bash(args: dict, cwd: str) -> str:
    command = args["command"]
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, cwd=cwd, timeout=60
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += result.stderr
        if result.returncode != 0:
            output += f"\n[exit code {result.returncode}]"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 60s"
    except Exception as e:
        return f"Error running command: {e}"


def _exec_list_files(args: dict, cwd: str) -> str:
    path = Path(args.get("path") or ".")
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = []
        for e in entries:
            prefix = "📁 " if e.is_dir() else "📄 "
            lines.append(f"{prefix}{e.name}")
        return "\n".join(lines) or "(empty directory)"
    except Exception as e:
        return f"Error listing directory: {e}"


def _run(cmd: str, cwd: str, timeout: int = 120) -> str:
    """Run a shell command and return combined stdout+stderr."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            out += f"\n[exit code {r.returncode}]"
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


def _exec_git_clone(args: dict, cwd: str) -> str:
    repo = args["repo"]
    directory = args.get("directory") or repo.split("/")[-1]
    return _run(f"gh repo clone {repo} {directory}", cwd=cwd, timeout=120)


def _exec_git_checkout_branch(args: dict, cwd: str) -> str:
    branch = args["branch"]
    if branch.lower() in {"main", "master"}:
        return f"Error: '{branch}' is a protected branch. Choose a feature branch name (e.g. 'copilot/your-task')."
    return _run(f"git checkout -b {branch}", cwd=cwd)


def _exec_git_commit_all(args: dict, cwd: str) -> str:
    message = args["message"].replace('"', '\\"')
    # Configure git identity if not already set (needed in some CI/clean envs)
    _run('git config user.email "copilot-agent@github.com" 2>/dev/null; git config user.name "Copilot Agent" 2>/dev/null', cwd=cwd)
    return _run(f'git add -A && git commit -m "{message}"', cwd=cwd)


def _exec_git_push(args: dict, cwd: str) -> str:
    branch = args.get("branch") or ""
    # Safety: never allow pushing directly to main/master
    protected = {"main", "master"}
    if branch.lower() in protected:
        return f"Error: pushing directly to '{branch}' is not allowed. Create a feature branch first."
    if branch:
        return _run(f"git push -u origin {branch}", cwd=cwd)
    # Determine current branch and block if it's protected
    current = _run("git rev-parse --abbrev-ref HEAD", cwd=cwd).strip()
    if current.lower() in protected:
        return f"Error: currently on '{current}' — refusing to push. Use git_checkout_branch to create a feature branch first."
    return _run("git push -u origin HEAD", cwd=cwd)


def _exec_create_pull_request(args: dict, cwd: str) -> str:
    repo = args["repo"]
    title = args["title"].replace('"', '\\"')
    body = args["body"].replace('"', '\\"').replace("\n", "\\n")
    base = args.get("base") or "main"
    # Safety: only --draft is supported; no --merge, --approve, or --fill flags
    draft_flag = "--draft" if args.get("draft") else ""
    cmd = f'gh pr create --repo {repo} --title "{title}" --body "{body}" --base {base} {draft_flag}'
    return _run(cmd.strip(), cwd=cwd)


def _dispatch_tool_call(tool_name: str, args: dict, cwd: str) -> str:
    if tool_name == "read_file":
        return _exec_read_file(args, cwd)
    elif tool_name == "write_file":
        return _exec_write_file(args, cwd)
    elif tool_name == "bash":
        return _exec_bash(args, cwd)
    elif tool_name == "list_files":
        return _exec_list_files(args, cwd)
    elif tool_name == "git_clone":
        return _exec_git_clone(args, cwd)
    elif tool_name == "git_checkout_branch":
        return _exec_git_checkout_branch(args, cwd)
    elif tool_name == "git_commit_all":
        return _exec_git_commit_all(args, cwd)
    elif tool_name == "git_push":
        return _exec_git_push(args, cwd)
    elif tool_name == "create_pull_request":
        return _exec_create_pull_request(args, cwd)
    else:
        return f"Unknown tool: {tool_name}"


async def run_agent_loop(
    task: str,
    cwd: str,
    model: str,
    max_iterations: int = 20,
) -> str:
    """
    Run a Copilot-powered agentic coding loop.
    Uses tool_choice='required' to ensure Claude models always emit tool calls
    (the Copilot API proxy drops tool_calls when tool_choice='auto' for Claude).
    The agent calls task_complete() when done to provide a final summary.
    """
    system_prompt = textwrap.dedent(f"""
        You are a GitHub Copilot coding agent running autonomously.
        Working directory: {cwd}

        You MUST call a tool on every response — never reply with plain text.
        Available tools: read_file, write_file, bash, list_files,
                         git_clone, git_checkout_branch, git_commit_all, git_push, create_pull_request.

        STRICT RULES — never violate these:
        - NEVER push to 'main' or 'master'. Always create a feature branch with git_checkout_branch first.
        - NEVER approve, merge, or auto-merge PRs. Only open them (create_pull_request).
        - All PRs must target 'main' as the base but be pushed from a feature branch.

        Typical GitHub PR workflow:
        1. git_clone the repo into the working directory.
        2. list_files / read_file to explore the code.
        3. git_checkout_branch to create a feature branch (e.g. 'copilot/fix-description').
        4. write_file to make changes.
        5. bash to run tests / linters if available.
        6. git_commit_all with a descriptive message.
        7. git_push to push the feature branch.
        8. create_pull_request with a clear title and body.
        9. task_complete with a summary including the PR URL.

        When working on a local directory (no clone needed), skip steps 1, 7, 8.
        Always call task_complete when the task is fully done.
    """).strip()

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    log_lines: list[str] = [f"🤖 Copilot agent starting task: {task}\n"]
    iterations = 0

    async with httpx.AsyncClient() as client:
        while iterations < max_iterations:
            iterations += 1
            resp = await client.post(
                f"{COPILOT_API_BASE}/chat/completions",
                headers=auth_headers(),
                json={
                    "model": model,
                    "messages": messages,
                    "tools": _AGENT_TOOLS,
                    "tool_choice": "required",  # must call a tool; avoids proxy dropping tool_calls for Claude
                    "max_tokens": 4096,
                    "temperature": 0.2,
                    "stream": False,
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
            tool_calls = msg.get("tool_calls") or []

            # Build assistant history entry
            assistant_entry: dict[str, Any] = {"role": "assistant"}
            if msg.get("content"):
                assistant_entry["content"] = msg["content"]
            if tool_calls:
                assistant_entry["tool_calls"] = tool_calls
            messages.append(assistant_entry)

            if not tool_calls:
                # Model didn't call any tool despite tool_choice=required — treat as done
                final_text = msg.get("content") or "(task complete)"
                log_lines.append(f"\n✅ Agent finished after {iterations} iteration(s):\n{final_text}")
                break

            tool_results = []
            done = False
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    fn_args = {}

                if fn_name == "task_complete":
                    summary = fn_args.get("summary", "(no summary provided)")
                    log_lines.append(f"\n✅ Agent finished after {iterations} iteration(s):\n{summary}")
                    done = True
                    # Still need to send a tool result back to satisfy the protocol
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "Task marked as complete.",
                    })
                else:
                    log_lines.append(f"🔧 [{iterations}] {fn_name}({json.dumps(fn_args)})")
                    result_text = _dispatch_tool_call(fn_name, fn_args, cwd)
                    log_preview = result_text[:500] + "..." if len(result_text) > 500 else result_text
                    log_lines.append(f"   → {log_preview}\n")
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_text,
                    })

            messages.extend(tool_results)
            if done:
                break

        else:
            log_lines.append(f"\n⚠️  Agent hit iteration limit ({max_iterations}). Task may be incomplete.")

    return "\n".join(log_lines)


app = Server("github-copilot")

TOOLS = [
    Tool(
        name="copilot_chat",
        description=(
            "Chat with GitHub Copilot. Use for coding questions, architecture advice, "
            "debugging, explanations, or any software development task. "
            "Supports all Copilot models including Claude Sonnet 4.6, GPT-5.4, Gemini 2.5 Pro."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Your question or request"},
                "context": {"type": "string", "description": "Optional: extra context such as file contents or error messages"},
                "model": {"type": "string", "description": f"Optional: model to use (default: {DEFAULT_MODEL})"},
            },
            "required": ["message"],
        },
    ),
    Tool(
        name="copilot_complete",
        description="Ask GitHub Copilot to complete or generate code from a prompt or partial code snippet.",
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Code or comment to complete"},
                "language": {"type": "string", "description": "Programming language (e.g. python, typescript, go)"},
                "instructions": {"type": "string", "description": "Optional: extra instructions"},
                "model": {"type": "string", "description": f"Optional: model override (default: {DEFAULT_MODEL})"},
            },
            "required": ["prompt"],
        },
    ),
    Tool(
        name="copilot_explain",
        description="Ask GitHub Copilot to explain a code snippet in detail.",
        inputSchema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Code to explain"},
                "language": {"type": "string", "description": "Programming language"},
                "focus": {"type": "string", "description": "Optional: focus area (e.g. 'performance', 'security', 'algorithm')"},
                "model": {"type": "string", "description": f"Optional: model override (default: {DEFAULT_MODEL})"},
            },
            "required": ["code"],
        },
    ),
    Tool(
        name="copilot_fix",
        description="Ask GitHub Copilot to identify and fix a bug in code.",
        inputSchema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Code with the bug"},
                "problem": {"type": "string", "description": "Optional: description of the bug or error message"},
                "language": {"type": "string", "description": "Programming language"},
                "model": {"type": "string", "description": f"Optional: model override (default: {DEFAULT_MODEL})"},
            },
            "required": ["code"],
        },
    ),
    Tool(
        name="copilot_review",
        description=(
            "Ask GitHub Copilot to review code for bugs, security vulnerabilities, "
            "performance issues, and best practice violations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Code to review"},
                "language": {"type": "string", "description": "Programming language"},
                "focus": {"type": "string", "description": "Optional: review focus (e.g. 'security', 'performance', 'readability')"},
                "model": {"type": "string", "description": f"Optional: model override (default: {DEFAULT_MODEL})"},
            },
            "required": ["code"],
        },
    ),
    Tool(
        name="copilot_test",
        description="Ask GitHub Copilot to generate unit tests for a function or module.",
        inputSchema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Code to write tests for"},
                "language": {"type": "string", "description": "Programming language"},
                "framework": {"type": "string", "description": "Optional: test framework (e.g. pytest, jest, go test, vitest)"},
                "model": {"type": "string", "description": f"Optional: model override (default: {DEFAULT_MODEL})"},
            },
            "required": ["code"],
        },
    ),
    Tool(
        name="copilot_list_models",
        description="List all GitHub Copilot models available to your account.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="copilot_agent",
        description=(
            "Spin up an autonomous GitHub Copilot coding agent to complete a task. "
            "The agent runs a multi-turn loop with tools: read_file, write_file, bash, "
            "list_files, git_clone, git_checkout_branch, git_commit_all, git_push, "
            "create_pull_request. "
            "It can clone a remote repo, make code changes, and open a PR — all autonomously. "
            "Example tasks: "
            "'Clone elastics-ai/my-service, add input validation to POST /orders, open a PR'; "
            "'Fix all TypeScript errors in elastics-ai/frontend and open a draft PR'. "
            "Returns a full log of every action taken plus a final summary."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Clear description of the coding task to complete",
                },
                "working_directory": {
                    "type": "string",
                    "description": "Absolute path to the working directory (defaults to current directory)",
                },
                "model": {
                    "type": "string",
                    "description": f"Model to use (default: {DEFAULT_MODEL}). Recommended: claude-sonnet-4.6 or gpt-5.4",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Max agent loop iterations before stopping (default: 20)",
                    "default": 20,
                },
            },
            "required": ["task"],
        },
    ),
]


@app.list_tools()
async def list_tools() -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


def _opt(d: dict, key: str) -> str:
    v = d.get(key, "")
    return str(v) if v else ""


def _hint(label: str, value: str) -> str:
    return f"{label}: {value}\n" if value else ""


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> CallToolResult:
    try:
        model = _opt(arguments, "model") or None

        if name == "copilot_list_models":
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{COPILOT_API_BASE}/models", headers=auth_headers(), timeout=10
                )
                resp.raise_for_status()
                raw_models = resp.json().get("data", [])

            # Keep only chat-capable models: filter out embedding models and
            # deduplicate aliases (prefer the versioned/dated canonical ID).
            seen_families: dict[str, str] = {}
            chat_models: list[dict] = []
            for m in raw_models:
                mid = m.get("id") or m.get("name", "")
                caps = m.get("capabilities", {})
                family = caps.get("family", mid)
                # Skip embedding models
                if "embedding" in mid.lower() or "embedding" in family.lower():
                    continue
                # For duplicate families keep the first (most specific/versioned) ID
                if family in seen_families:
                    continue
                seen_families[family] = mid
                chat_models.append({"id": mid, "family": family})

            lines = [f"- {m['id']}" for m in chat_models]
            result = f"{len(lines)} chat models available:\n" + "\n".join(lines)

        elif name == "copilot_chat":
            message = arguments["message"]
            context = _opt(arguments, "context")
            body = f"{message}\n\n<context>\n{context}\n</context>" if context else message
            messages = [
                {"role": "system", "content": "You are GitHub Copilot, an AI programming assistant. Be concise and accurate."},
                {"role": "user", "content": body},
            ]
            result = await chat_completion(messages, model)

        elif name == "copilot_complete":
            prompt = arguments["prompt"]
            lang = _opt(arguments, "language")
            instr = _opt(arguments, "instructions")
            body = _hint("Language", lang) + _hint("Instructions", instr) + f"Complete this code:\n\n```\n{prompt}\n```"
            messages = [
                {"role": "system", "content": "You are GitHub Copilot. Return only the completed code unless asked for explanation."},
                {"role": "user", "content": body},
            ]
            result = await chat_completion(messages, model)

        elif name == "copilot_explain":
            code = arguments["code"]
            lang = _opt(arguments, "language")
            focus = _opt(arguments, "focus")
            body = _hint("Language", lang) + _hint("Focus on", focus) + f"Explain this code:\n\n```\n{code}\n```"
            messages = [
                {"role": "system", "content": "You are GitHub Copilot. Explain code clearly: what it does, how it works, and important details."},
                {"role": "user", "content": body},
            ]
            result = await chat_completion(messages, model)

        elif name == "copilot_fix":
            code = arguments["code"]
            problem = _opt(arguments, "problem")
            lang = _opt(arguments, "language")
            body = _hint("Language", lang) + _hint("Problem/Error", problem) + f"Fix this code:\n\n```\n{code}\n```"
            messages = [
                {"role": "system", "content": "You are GitHub Copilot. Fix the bug. Return the fixed code and briefly explain what was wrong."},
                {"role": "user", "content": body},
            ]
            result = await chat_completion(messages, model)

        elif name == "copilot_review":
            code = arguments["code"]
            lang = _opt(arguments, "language")
            focus = _opt(arguments, "focus")
            body = _hint("Language", lang) + _hint("Review focus", focus) + f"Review this code:\n\n```\n{code}\n```"
            messages = [
                {"role": "system", "content": "You are GitHub Copilot performing a code review. Identify bugs, security issues, performance problems, and style improvements. Be specific."},
                {"role": "user", "content": body},
            ]
            result = await chat_completion(messages, model)

        elif name == "copilot_test":
            code = arguments["code"]
            lang = _opt(arguments, "language")
            fw = _opt(arguments, "framework")
            body = _hint("Language", lang) + _hint("Test framework", fw) + f"Write tests for:\n\n```\n{code}\n```"
            messages = [
                {"role": "system", "content": "You are GitHub Copilot. Write comprehensive unit tests: happy paths, edge cases, and error conditions."},
                {"role": "user", "content": body},
            ]
            result = await chat_completion(messages, model)

        elif name == "copilot_agent":
            task = arguments["task"]
            cwd = _opt(arguments, "working_directory") or os.getcwd()
            max_iter = int(arguments.get("max_iterations") or 20)
            agent_model = _opt(arguments, "model") or DEFAULT_MODEL
            result = await run_agent_loop(task, cwd, agent_model, max_iter)

        else:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Unknown tool: {name}")],
                isError=True,
            )

        return CallToolResult(content=[TextContent(type="text", text=result)])

    except httpx.HTTPStatusError as e:
        msg = f"Copilot API error {e.response.status_code}: {e.response.text}"
        logger.error(msg)
        return CallToolResult(content=[TextContent(type="text", text=msg)], isError=True)
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e)
        return CallToolResult(content=[TextContent(type="text", text=f"Error: {e}")], isError=True)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def run():
    """Console script entry point (`copilot-mcp` command)."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
