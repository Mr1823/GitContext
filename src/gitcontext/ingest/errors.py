"""Errors raised while communicating with GitHub as an upstream provider."""


class GitHubAPIError(RuntimeError):
    """A safe-to-report GitHub request failure, separate from bad user input."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
