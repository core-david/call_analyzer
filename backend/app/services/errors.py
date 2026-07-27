"""Provider-agnostic error taxonomy.

Services translate their provider's failures into these two classes; the
worker branches on the class (retry vs. fail) and persists `error_code`.
This module never imports a provider SDK — that translation belongs to
the service that owns the provider.
"""


class ProviderError(Exception):
    """Base: a failure talking to an external provider."""

    def __init__(self, error_code: str, detail: str = "") -> None:
        self.error_code = error_code
        super().__init__(f"{error_code}: {detail}" if detail else error_code)


class RetryableProviderError(ProviderError):
    """Transient — worth retrying with backoff (429, 5xx, timeouts)."""


class PermanentProviderError(ProviderError):
    """Definitive — retrying cannot help (bad audio, bad credentials)."""


def classify_http_status(
    status: int | None, *, permanent_code: str, detail: str = ""
) -> ProviderError:
    """Map an HTTP status from any provider into the taxonomy.

    Transport-level meanings are universal (429 = slow down, 5xx = their
    problem, 401/402/403 = your account). Only the residual 4xx is
    provider-specific: `permanent_code` names what a rejection means for
    this provider (transcription: `audio_unreadable`).
    `status=None` means the error had no status at all (connection-level)
    — treated as retryable.
    """
    if status in (401, 402, 403):
        return PermanentProviderError("provider_auth", detail)
    if status == 429:
        return RetryableProviderError("provider_rate_limited", detail)
    if status is None or status == 408 or status >= 500:
        return RetryableProviderError("provider_unavailable", detail)
    return PermanentProviderError(permanent_code, detail)
