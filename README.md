# copilot-mcp

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that exposes **GitHub Copilot AI** — including Claude Sonnet, GPT-5, Gemini, and all other Copilot models — as tools for any MCP client.

Works with **Codex CLI**, **Claude Code**, **Gemini CLI**, and any other MCP-compatible agent.

---

## What it does

- 💬 **Chat** with any Copilot model (Claude Sonnet 4.6, GPT-5.4, Gemini 2.5 Pro, etc.)
- ✍️ **Complete** code from a prompt
- 📖 **Explain** code snippets
- 🔧 **Fix** bugs
- 🔍 **Review** code for bugs, security, and performance issues
- 🧪 **Generate** unit tests
- 🤖 **Agent mode** — spin up an autonomous coding agent that can read/write files, run shell commands, and **open Pull Requests** on GitHub — all in a multi-turn loop

---

## Prerequisites

- Python 3.10+
- A **GitHub account with Copilot subscription** (Individual, Business, or Enterprise)
- [`gh` CLI](https://cli.github.com) authenticated (`gh auth login`) **or** a `GITHUB_TOKEN` env var

---

## Installation

```bash
pip install git+https://github.com/elastics-ai/copilot-mcp
```

Or clone and install locally:

```bash
git clone https://github.com/elastics-ai/copilot-mcp
cd copilot-mcp
pip install -e .
```

Verify it works:

```bash
copilot-mcp --help    # should print nothing and exit (it's an MCP stdio server)
python -c "import copilot_mcp; print('OK')"
```

---

## Authentication

The server uses your existing `gh` CLI session — no extra setup needed:

```bash
gh auth login   # one-time setup
```

Or set an environment variable:

```bash
export GITHUB_TOKEN=ghp_your_token_here
```

---

## Client configuration

### Codex CLI

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.copilot]
command = "copilot-mcp"
startup_timeout_sec = 15
```

### Claude Code

```bash
claude mcp add github-copilot copilot-mcp
```

Or add to `~/.claude.json` manually:

```json
{
  "mcpServers": {
    "github-copilot": {
      "command": "copilot-mcp"
    }
  }
}
```

### Gemini CLI

Add to `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "github-copilot": {
      "command": "copilot-mcp"
    }
  }
}
```

> **Note:** All three clients use the same `copilot-mcp` command — no path needed after `pip install`.

---

## Tools

| Tool | Description |
|------|-------------|
| `copilot_chat` | General coding chat — questions, architecture, debugging |
| `copilot_complete` | Code generation / completion |
| `copilot_explain` | Explain a code snippet |
| `copilot_fix` | Identify and fix bugs |
| `copilot_review` | Code review (bugs, security, performance) |
| `copilot_test` | Generate unit tests |
| `copilot_list_models` | List all available Copilot models |
| `copilot_agent` | Autonomous coding agent loop (see below) |

Every tool accepts an optional `model` parameter. Default is `claude-sonnet-4.6`.

### Changing the default model

```bash
COPILOT_MODEL=gpt-5.4 codex
COPILOT_MODEL=claude-opus-4.6 claude
```

### Available models

Run `copilot_list_models` from any client, or check programmatically:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"copilot_list_models","arguments":{}}}' \
  | copilot-mcp
```

Notable models: `claude-sonnet-4.6`, `claude-opus-4.6`, `gpt-5.4`, `gpt-5.3-codex`, `gemini-2.5-pro`, `gpt-4o-2024-11-20`

---

## Agent mode (`copilot_agent`)

The `copilot_agent` tool spins up a fully autonomous coding agent. You give it a task; it loops until done.

### Agent tools

The agent has access to: `read_file`, `write_file`, `bash`, `list_files`, `git_clone`, `git_checkout_branch`, `git_commit_all`, `git_push`, `create_pull_request`

### Example tasks

```
"Add input validation to the POST /orders endpoint in elastics-ai/my-service and open a PR"
"Fix all TypeScript errors in src/ and write tests for the changed files"
"Refactor the database connection module to use a connection pool"
```

### GitHub PR workflow

When given a task involving a remote repo, the agent autonomously:

1. `git_clone` the repository
2. Explores with `list_files` / `read_file`
3. `git_checkout_branch` — creates a feature branch (e.g. `copilot/add-validation`)
4. Makes changes with `write_file`
5. Runs tests/linters with `bash`
6. `git_commit_all` with a descriptive message
7. `git_push` the feature branch
8. `create_pull_request` — opens a PR with title and description
9. Returns a summary with the PR URL

### Safety guardrails

- ❌ **Never pushes to `main` or `master`** — blocked at the tool level
- ❌ **Never approves or merges PRs** — only opens them
- ✅ Always creates a feature branch before committing
- ✅ Max 20 agent iterations by default (configurable)

---

## Usage examples

### Codex CLI

```
Ask Copilot to review src/auth.ts for security issues
Use copilot_agent to add rate limiting to elastics-ai/api-service and open a PR
```

### Claude Code

```
Use the copilot_chat tool to explain how the order matching algorithm works
```

### Direct (for testing)

```python
import asyncio, sys
sys.path.insert(0, '.')
from copilot_mcp.server import call_tool

async def test():
    r = await call_tool("copilot_chat", {"message": "What is a deadlock?"})
    print(r.content[0].text)

asyncio.run(test())
```

---

## Configuration reference

| Env var | Default | Description |
|---------|---------|-------------|
| `COPILOT_MODEL` | `claude-sonnet-4.6` | Default model for all tools |
| `GITHUB_TOKEN` | *(from gh CLI)* | GitHub PAT — falls back to `gh auth token` |
| `GH_TOKEN` | *(from gh CLI)* | Alternative GitHub token env var |

---

## How it works

The server calls `https://api.githubcopilot.com/chat/completions` directly using your GitHub token — the same API that powers Copilot in VS Code. No token exchange or proxy is needed.

**Key implementation note:** The Copilot API proxy drops `tool_calls` from Claude model responses when `tool_choice: "auto"`. The agent loop works around this by using `tool_choice: "required"` with a `task_complete` sentinel tool.

---

## License

MIT © [Elastics](https://github.com/elastics-ai)
