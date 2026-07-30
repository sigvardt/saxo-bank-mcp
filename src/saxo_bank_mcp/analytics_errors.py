from __future__ import annotations


class AnalyticsContractError(ValueError):
    """Base error for a rejected analytics contract."""

    def __init__(self, code: str, message: str) -> None:
        """Store a stable error code without retaining rejected input."""
        super().__init__(message)
        self.code = code


class AnalyticsPrivacyError(AnalyticsContractError):
    """Raised when a value cannot be included in public analytics evidence."""
