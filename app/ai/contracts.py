from pathlib import Path
from typing import Literal, Protocol
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Costume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    costume_detected: bool
    costume_name: str = Field(max_length=100)
    description: str = Field(max_length=500)
    certainty: Literal["unknown", "possible", "likely"]
    people_count: int = Field(ge=0, le=30)

    @classmethod
    def unknown(cls):
        return cls(costume_detected=False, costume_name="unknown", description="Unclear image",
                   certainty="unknown", people_count=0)


# Defense in depth, not a substitute for provider moderation and field testing.
UNSAFE = re.compile(r"\b(fuck|shit|bitch|idiot|stupid|sexy|kill yourself)\b|https?://|[\w.+-]+@[\w.-]+|\d{3}[ -]?\d{3}[ -]?\d{4}|\b(your name|your age|how old|your address|phone number|contact details|your body|you weigh)\b", re.I)


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_text: str
    tone: Literal["playful", "friendly"]
    ends_conversation: bool

    @field_validator("response_text")
    @classmethod
    def bounded_safe_text(cls, text):
        text = text.strip()
        if not text or len(text.split()) > 25 or len(text) > 300 or text.count("?") > 1 or UNSAFE.search(text):
            raise ValueError("Unsafe or unbounded response")
        return text


class Provider(Protocol):
    async def vision(self, jpeg: bytes) -> Costume: ...
    async def reply(self, costume: Costume, history: list[dict], final: bool) -> Reply: ...
    async def synthesize(self, text: str, path: Path) -> None: ...
    async def transcribe(self, pcm: bytes) -> str: ...
    async def close(self) -> None: ...
