"""Generate a one-shot demo agent task via the Anthropic Messages API."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class GeneratedTask:
    prompt: str
    model: str


class _AnthropicTextBlock(BaseModel):
    type: str
    text: str = ""


class _AnthropicMessageResponse(BaseModel):
    content: list[_AnthropicTextBlock] = Field(default_factory=list)


_META_SYSTEM = (
    "You invent short coding tasks for a sandboxed git agent demo. "
    "Reply with ONLY the task instructions for the agent — no preamble, "
    "no quotes, no markdown fences."
)


def _user_message(tree_paths: tuple[str, ...]) -> str:
    listing = "\n".join(f"- {p}" for p in tree_paths) or "- (empty repo)"
    return (
        "Invent one small, concrete task for an autonomous coding agent that "
        "edits a demo git repository mounted at /workspace.\n\n"
        "Constraints for the task you write:\n"
        "- Touch only paths under data/ and/or docs/ (create or update).\n"
        "- Prefer JSON or Markdown files.\n"
        "- Small scope: a few files at most, finishable in one short turn.\n"
        "- Instruct the agent to make **atomic git commits** on the current "
        "branch (typically 2–4 commits), each with one clear purpose and a "
        "focused message — not a single squash commit for the whole task.\n"
        "- Instruct the agent not to push and not to change .git config.\n"
        "- Make the task specific and slightly creative (not 'edit a file').\n"
        "- 3–6 sentences max.\n\n"
        f"Current files on main:\n{listing}\n"
    )


async def generate_agent_task(
    *,
    api_key: str,
    tree_paths: tuple[str, ...],
    model: str = "claude-haiku-4-5-20251001",
) -> GeneratedTask:
    if not api_key:
        msg = "ANTHROPIC_API_KEY is required to generate agent tasks"
        raise RuntimeError(msg)

    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 400,
                "system": _META_SYSTEM,
                "messages": [
                    {"role": "user", "content": _user_message(tree_paths)},
                ],
            },
        )
    if response.status_code >= 400:
        msg = (
            f"Anthropic task generation failed ({response.status_code}): "
            f"{response.text[:500]}"
        )
        raise RuntimeError(msg)

    parsed = _AnthropicMessageResponse.model_validate(response.json())
    texts = [block.text.strip() for block in parsed.content if block.type == "text"]
    prompt = "\n".join(t for t in texts if t).strip()
    if not prompt:
        msg = "Anthropic returned an empty task prompt"
        raise RuntimeError(msg)
    return GeneratedTask(prompt=prompt, model=model)
