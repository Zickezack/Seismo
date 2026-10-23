"""Result of an all-or-nothing bulk action (requirements.md, "Destructive and Bulk Actions", item 2)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BulkResult:
    processed: int
    blocked: list[str]  # display names of the items that kept the whole action from running
