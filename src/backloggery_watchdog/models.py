from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class GameState:
    ra_game_id: int
    title: str
    console: str
    last_played: str
    rich_presence: str | None
    online: bool
    earned: int
    total: int
    beaten: bool
    mastered: bool
    region: str | None

    @property
    def status(self) -> str:
        if self.mastered:
            return "Completed"
        if self.beaten:
            return "Beaten"
        if self.earned > 0:
            return "Unfinished"
        return "Unplayed"

    @property
    def fingerprint(self) -> str:
        return "\x1f".join(
            [self.rich_presence or "", self.last_played, str(self.earned), str(self.total)]
        )


@dataclass(slots=True)
class SyncPlan:
    action: str
    ra_game_id: int
    title: str
    reason: str
    backloggery_entry_id: int | None = None
    candidate_entry_ids: list[int] = field(default_factory=list)
    changes: dict[str, dict[str, Any]] = field(default_factory=dict)
    proposed_payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
