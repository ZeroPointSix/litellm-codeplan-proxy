"""Gateway rate-limit response helpers."""

from starlette.responses import Response

GATEWAY_RATE_LIMIT_RETRY_AFTER_SECONDS = "60"


def ensure_retry_after_header(response: Response) -> Response:
    """Add a stable Retry-After fallback to LiteLLM 429 responses."""
    if response.status_code == 429 and "retry-after" not in response.headers:
        response.headers["Retry-After"] = GATEWAY_RATE_LIMIT_RETRY_AFTER_SECONDS
    return response
