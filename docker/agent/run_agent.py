from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_PROMPT = (
    "You are editing a demo git repository at /workspace. "
    "Make a small, useful change: update or create files under data/ and docs/ "
    "(prefer JSON or Markdown). Work in small steps and make **atomic commits** "
    "on the current branch — typically 2–4 commits, each with one clear purpose "
    "and a focused message (e.g. add a file, then update related docs). "
    "Do not squash everything into a single commit. "
    "Do not push. Do not modify .git configuration."
)

SESSION_ID_MARKER = "GIT_PG_CLAUDE_SESSION_ID="


def _log(msg: str) -> None:
    print(msg, flush=True)
    sys.stdout.flush()


def _message_line(message: object) -> str | None:
    """Best-effort one-line summary of an SDK stream message."""
    cls = type(message).__name__
    for attr in ("result", "content", "text", "error", "subtype", "session_id"):
        if hasattr(message, attr):
            value = getattr(message, attr)
            if value is None or value == "":
                continue
            text = str(value)
            if len(text) > 500:
                text = text[:500] + "…"
            return f"[{cls}] {attr}={text}"
    return f"[{cls}]"


def _extract_session_id(message: object) -> str | None:
    if hasattr(message, "session_id"):
        value = message.session_id  # type: ignore[attr-defined]
        if value:
            return str(value)
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        nested = data.get("session_id")
        if nested:
            return str(nested)
    return None


async def _run_claude(
    cwd: Path,
    prompt: str,
    *,
    resume: str | None,
) -> tuple[str, str | None]:
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query
    except ImportError as exc:
        msg = "claude-agent-sdk not installed in agent image"
        raise RuntimeError(msg) from exc

    options = ClaudeAgentOptions(
        cwd=str(cwd),
        sandbox={"enabled": False},
        setting_sources=[],
        # Unattended Docker sandbox: allow git/bash without interactive approval.
        permission_mode="bypassPermissions",
        resume=resume,
    )
    result_text = ""
    session_id: str | None = resume
    if resume:
        _log(f"claude: resuming session {resume}")
    else:
        _log("claude: starting agent turn")
    async for message in query(prompt=prompt, options=options):
        found = _extract_session_id(message)
        if found:
            session_id = found
        line = _message_line(message)
        if line is not None:
            _log(line)
        if hasattr(message, "result") and message.result:
            result_text = str(message.result)
    _log("claude: turn finished")
    return result_text, session_id


def _ensure_git_identity(cwd: Path) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "config", "user.email", "agent@git-pg.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(cwd), "config", "user.name", "git-pg agent"],
        check=True,
    )


def main() -> None:
    cwd = Path("/workspace")
    prompt = os.environ.get("GIT_PG_AGENT_PROMPT", DEFAULT_PROMPT)
    resume = os.environ.get("GIT_PG_CLAUDE_RESUME") or None
    _log(f"workspace={cwd} branch={_current_branch(cwd)}")
    _log(f"prompt={prompt[:200]}{'…' if len(prompt) > 200 else ''}")
    _ensure_git_identity(cwd)
    result, session_id = asyncio.run(_run_claude(cwd, prompt, resume=resume))
    if session_id:
        _log(f"{SESSION_ID_MARKER}{session_id}")
    # Commit any leftover unstaged edits if the agent forgot.
    status = subprocess.run(
        ["git", "-C", str(cwd), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        _log("committing leftover workspace changes")
        subprocess.run(["git", "-C", str(cwd), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(cwd), "commit", "-m", "agent sandbox edits"],
            check=True,
        )
    _log(result or "agent turn complete")


def _current_branch(cwd: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "branch", "--show-current"],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() or "(unknown)"


if __name__ == "__main__":
    main()
