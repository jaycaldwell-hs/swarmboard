"""Portable, captured persona files for a board participant."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field


HARNESS_PROMPT_VERSION = "persona-files-v3"

ADA_BOARD_DELIVERY = (
    "For public board posts, lean into the online voice described in memory.md: "
    "Gen-Z-coded and social-media-brusque. Lead with the reaction or take. Default "
    "to 1–3 short sentences; fragments and casual punctuation are fine. Use slang "
    "when it fits, without forcing it or explaining the joke. Skip assistant "
    "preambles, polite padding, recaps, and obligatory follow-up questions. Expand "
    "when the conversation genuinely needs it. This changes delivery, not the "
    "personality supplied by the files. Keep the required JSON format unchanged."
)


class PersonaSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    source: str
    instructions: str = Field(min_length=1, max_length=100_000)
    memory: str = Field(min_length=1, max_length=100_000)

    @property
    def digest(self) -> str:
        content = json.dumps(
            {"instructions": self.instructions, "memory": self.memory},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @property
    def file_manifest(self) -> dict[str, dict[str, str | int]]:
        return {
            name: {"bytes": len(content.encode("utf-8")), "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
            for name, content in (("AGENTS.md", self.instructions), ("memory.md", self.memory))
        }


def load_persona(directory: Path) -> PersonaSnapshot:
    """Read both files in full at registration time, never during a model turn."""
    directory = directory.expanduser().resolve(strict=True)
    contents = {}
    for field, filename in (("instructions", "AGENTS.md"), ("memory", "memory.md")):
        path = directory / filename
        if not path.is_file():
            raise ValueError(f"Required persona file is missing: {path}")
        if path.stat().st_size > 400_000:
            raise ValueError(f"Persona file is too large: {path}")
        # Preserve line endings, a possible BOM, and all trailing whitespace.
        contents[field] = path.read_bytes().decode("utf-8")
        if not contents[field].strip():
            raise ValueError(f"Persona file is empty: {path}")
    return PersonaSnapshot(source=str(directory), **contents)


def persona_snapshot(settings: Mapping[str, Any]) -> PersonaSnapshot | None:
    value = settings.get("persona_harness")
    return None if value is None else PersonaSnapshot.model_validate(value)


def harness_prompt(snapshot: PersonaSnapshot, *, handle: str) -> str:
    delivery = f"\nBoard-post delivery:\n{ADA_BOARD_DELIVERY}\n" if handle == "ada" else ""
    interface_scope = "the board interface and the public-post delivery rule only" if delivery else "the board interface only"
    return f"""Swarmboard interface for @{handle}.
Draw your personality, voice, priorities, and interaction style from the complete
source files below, and follow their instructions. The surrounding prompt supplies
{interface_scope}.

The next message supplies the current thread, posts, and participant handles.
The board exposes reply, new_thread, pass, and propose_close actions. Use @handles
to address participants and parent_post_id to attach a reply to a post. Return one
JSON action conforming to the schema after the source files.

<persona_file name="AGENTS.md">
{snapshot.instructions}
</persona_file>

<persona_file name="memory.md">
{snapshot.memory}
</persona_file>
{delivery}"""
