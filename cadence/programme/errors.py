"""Failures the program engine refuses to paper over."""

from __future__ import annotations


class ProgramBuildError(RuntimeError):
    """A session could not be built legally, and shipping it anyway would be worse.

    The engine's habit is to clamp and substitute rather than reject (D-019), so this is reserved
    for the cases where there is nothing left to substitute *to*: a day type whose compound row
    has no legal movement in the whole library, or a plan whose rows the validator refuses. Both
    mean a screen that would lie about what the household can do.
    """

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        self.problems = list(problems or [])
        detail = ("\n  " + "\n  ".join(self.problems)) if self.problems else ""
        super().__init__(f"{message}{detail}")
