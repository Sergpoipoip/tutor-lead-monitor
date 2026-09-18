from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, repr=False)
class LeadView:
    id: UUID
    score: int
    reasons: tuple[str, ...]
    subject: str
    grade: int | None
    goals: tuple[str, ...]
    format: str
    location: str | None
    urgency: str
    budget: str | None
    source: str
    published_at: datetime | None
    excerpt: str
    url: str | None


@dataclass(frozen=True)
class Button:
    label: str
    data: str


@dataclass(frozen=True, repr=False)
class Message:
    html: str
    buttons: tuple[Button, ...] = ()


class FailureKind(StrEnum):
    RETRYABLE = "retryable"
    RATE_LIMITED = "rate_limited"
    AUTHORIZATION = "authorization"
    MALFORMED = "malformed"
    PERMANENT = "permanent"
    AMBIGUOUS = "ambiguous"


class DeliveryError(Exception):
    def __init__(self, kind: FailureKind, *, retry_after: float = 0) -> None:
        self.kind = kind
        self.retry_after = max(0, retry_after)
        super().__init__(kind.value)


class Notifier(Protocol):
    async def send(self, recipient: int, message: Message) -> str: ...

    async def answer(self, callback_id: str, text: str) -> None: ...


@dataclass
class FakeNotifier:
    """Deterministic transport; no Telegram objects or network access."""

    outcomes: list[DeliveryError | None] = field(default_factory=list, repr=False)
    calls: list[tuple[int, Message]] = field(default_factory=list, repr=False)
    sent: list[tuple[int, Message]] = field(default_factory=list, repr=False)
    answers: list[tuple[str, str]] = field(default_factory=list, repr=False)

    async def send(self, recipient: int, message: Message) -> str:
        self.calls.append((recipient, message))
        if self.outcomes:
            error = self.outcomes.pop(0)
            if error:
                raise error
        self.sent.append((recipient, message))
        return str(len(self.sent))

    async def answer(self, callback_id: str, text: str) -> None:
        self.answers.append((callback_id, text))
