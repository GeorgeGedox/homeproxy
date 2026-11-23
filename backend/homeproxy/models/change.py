"""The model for one change in plain words, as the pending bar and the History page show it."""

from typing import Literal

from pydantic import BaseModel

Action = Literal["added", "changed", "removed"]


class ChangeSummary(BaseModel):
    """One change, pending or made by an apply. `label` names what changed, such as
    `example.com/test`, a zone, a settings section or a file path, and never holds a value."""

    kind: Literal["route", "domain", "settings", "basic_auth", "file"]
    action: Action
    label: str
