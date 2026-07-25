from starlette.responses import Response

from gateway.rate_limit import GATEWAY_RATE_LIMIT_RETRY_AFTER_SECONDS, ensure_retry_after_header


def test_gateway_retry_after_helper_adds_header_to_429():
    response = ensure_retry_after_header(Response(status_code=429))

    assert response.headers["Retry-After"] == GATEWAY_RATE_LIMIT_RETRY_AFTER_SECONDS


def test_gateway_retry_after_helper_preserves_existing_header():
    response = ensure_retry_after_header(Response(status_code=429, headers={"Retry-After": "5"}))

    assert response.headers["Retry-After"] == "5"


def test_gateway_retry_after_helper_ignores_non_rate_limit_responses():
    response = ensure_retry_after_header(Response(status_code=403))

    assert "Retry-After" not in response.headers
