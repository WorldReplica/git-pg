from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRunConfig:
    cwd: str
    prompt: str
    sandbox_enabled: bool = True


async def run_agent_turn(config: AgentRunConfig) -> str:
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query
    except ImportError as exc:
        msg = "claude-agent-sdk not installed; use uv sync --extra agent"
        raise RuntimeError(msg) from exc

    options = ClaudeAgentOptions(
        cwd=config.cwd,
        sandbox={"enabled": config.sandbox_enabled} if config.sandbox_enabled else None,
        setting_sources=[],
        permission_mode="bypassPermissions",
    )
    result_text = ""
    async for message in query(prompt=config.prompt, options=options):
        if hasattr(message, "result") and message.result:
            result_text = str(message.result)
    return result_text
