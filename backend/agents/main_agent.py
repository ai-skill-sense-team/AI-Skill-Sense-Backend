from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import os
from random import uniform
import time
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

MAIN_AGENT_MAX_ATTEMPTS = 5
MAIN_AGENT_RETRYABLE_STATUSES = {429, 502, 503, 504}
MAIN_AGENT_RETRYABLE_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)
MAIN_AGENT_CONNECT_TIMEOUT_SECONDS = 300.0
MAIN_AGENT_READ_TIMEOUT_SECONDS = 300.0
MAIN_AGENT_RETRY_DELAY_CAP_SECONDS = 300

# Standard browser User-Agent to bypass Cloudflare Bot Management on *.onrender.com
DEFAULT_USER_AGENT = "ai-skill-sense-backend/1.0"

# Persistent singleton client with connection pooling and HTTP/2 enabled
_client_instance: Optional[httpx.Client] = None


def _get_agent_url() -> str:
    return os.getenv(
        "AGENT_SERVICE_URL",
        "http://127.0.0.1:8001",
    ).rstrip("/")


def _get_persistent_client() -> httpx.Client:
    """
    Returns a persistent singleton httpx.Client configured with HTTP/2 and keep-alive 
    connection pooling to prevent Cloudflare handshake rate-limiting.
    """
    global _client_instance
    if _client_instance is None:
        agent_url = _get_agent_url()
        _client_instance = httpx.Client(
            base_url=agent_url,
            timeout=httpx.Timeout(
                MAIN_AGENT_READ_TIMEOUT_SECONDS,
                connect=MAIN_AGENT_CONNECT_TIMEOUT_SECONDS,
                write=30.0,
                pool=10.0,
            ),
            limits=httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=30.0,
            ),
            follow_redirects=True,
        )
    return _client_instance


def _backoff_delay(attempt: int) -> float:
    # Start with a gentler 10s base, scale up, and add random jitter (0-5s)
    base_delay = min(10 * (2 ** attempt), MAIN_AGENT_RETRY_DELAY_CAP_SECONDS)
    jitter = uniform(0, 5)
    return base_delay + jitter

RATE_LIMIT_DELAY_SECONDS = 60


def _rate_limit_delay(response: httpx.Response, attempt: int) -> float:
    """On 429, honor Retry-After if present, else wait a long flat interval."""
    if response.headers.get("Retry-After"):
        return _retry_delay(response, attempt)
    return RATE_LIMIT_DELAY_SECONDS + uniform(0, 5)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(
                max(float(retry_after), 0),
                MAIN_AGENT_RETRY_DELAY_CAP_SECONDS,
            )
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return min(
                    max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0),
                    MAIN_AGENT_RETRY_DELAY_CAP_SECONDS,
                )
            except (TypeError, ValueError, OverflowError):
                logger.warning("Agent returned an invalid Retry-After header.")
    return _backoff_delay(attempt)


def _public_headers() -> dict[str, str]:
    """Headers for unauthenticated GET requests (like health/wake-up pings)."""
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }


def _agent_headers() -> dict[str, str]:
    """Headers for authenticated POST triggers to the Agent service."""
    secret = os.getenv("AGENT_SECRET_KEY")
    if not secret:
        raise RuntimeError("AGENT_SECRET_KEY is not configured.")
    return {
        **_public_headers(),
        "X-Agent-Secret": secret,
        "Content-Type": "application/json",
    }

def ping_agent_service() -> dict:
    """Check whether the separate agent service is reachable."""
    agent_url = _get_agent_url()
    client = _get_persistent_client()

    try:
        response = client.get(
            f"{agent_url}/",
            headers=_public_headers(),
            timeout=15.0,  # Increased to tolerate slight Render latency
        )
        response.raise_for_status()
        return {
            "status": "connected",
            "message": "Agent service is reachable.",
        }
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            logger.warning(
                "Agent service is reachable but rate-limited its startup health check."
            )
            return {
                "status": "rate_limited",
                "message": "Agent service responded to the health check with HTTP 429.",
            }
        logger.warning("Agent service health check failed: %s", exc)
        return {
            "status": "unavailable",
            "message": "Backend started, but agent service is not reachable.",
        }
    except httpx.HTTPError as exc:
        logger.warning("Agent service is not reachable yet: %s", exc)
        return {
            "status": "unavailable",
            "message": "Backend started, but agent service is not reachable.",
        }


def _post_to_agent(path: str, payload: dict, caller_name: str) -> dict:
    """Shared HTTP POST dispatcher with persistent connection pooling and retries."""
    headers = _agent_headers()
    client = _get_persistent_client()

    for attempt in range(MAIN_AGENT_MAX_ATTEMPTS):
        try:
            response = client.post(path, json=payload, headers=headers)
        except MAIN_AGENT_RETRYABLE_ERRORS as exc:
            if attempt + 1 == MAIN_AGENT_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"Failed to communicate with {caller_name} after "
                    f"{MAIN_AGENT_MAX_ATTEMPTS} attempts."
                ) from exc
            delay = _backoff_delay(attempt)
            logger.warning(
                "%s request failed transiently; retrying in %.0f "
                "seconds (attempt %d/%d): %s",
                caller_name, delay, attempt + 2, MAIN_AGENT_MAX_ATTEMPTS, exc,
            )
            time.sleep(delay)
            continue
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to communicate with {caller_name}.") from exc

        if response.status_code == 429:
            # Diagnostic: shows whether Cloudflare (error 1015) or Render answered
            logger.warning(
                "%s got 429. body=%r headers=%s",
                caller_name,
                response.text[:300],
                dict(response.headers),
            )

        if (
            response.status_code in MAIN_AGENT_RETRYABLE_STATUSES
            and attempt + 1 < MAIN_AGENT_MAX_ATTEMPTS
        ):
            if response.status_code == 429:
                delay = _rate_limit_delay(response, attempt)
            else:
                delay = _retry_delay(response, attempt)
            logger.warning(
                "%s returned HTTP %d; retrying in %.0f seconds (attempt %d/%d).",
                caller_name, response.status_code, delay,
                attempt + 2, MAIN_AGENT_MAX_ATTEMPTS,
            )
            time.sleep(delay)
            continue

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"{caller_name} service returned HTTP "
                f"{response.status_code} after {attempt + 1} attempt(s)."
            ) from exc

        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"{caller_name} service returned invalid JSON after a successful response."
            ) from exc

    raise RuntimeError(f"{caller_name} trigger exhausted retries without a response.")


def trigger_main_agent(
    update_id: str,
    user_id: str,
    action_required: str,
) -> dict:
    """
    Dispatches the main agent trigger payload to the separate LangGraph agent service
    using an HTTP POST request and the internal AGENT_SECRET_KEY for authentication.
    """
    payload = {
        "update_id": update_id,
        "user_id": user_id,
        "action_required": action_required,
    }
    agent_response = _post_to_agent(
        "/agents/main-agent/trigger",
        payload,
        "Main Agent",
    )
    return {
        "status": "success",
        "message": "Main Agent triggered successfully.",
        "agent_response": agent_response,
    }


def trigger_main_agent_in_background(
    update_id: str,
    user_id: str,
    action_required: str,
) -> None:
    try:
        trigger_main_agent(update_id, user_id, action_required)
    except RuntimeError as exc:
        logger.error(
            "Main Agent nudge failed after retries for update_id=%s "
            "user_id=%s: %s",
            update_id,
            user_id,
            exc,
        )


def trigger_extractor(url_id: str, url: str) -> dict:
    payload = {"url_id": url_id, "url": url, "is_visited": "No"}
    return _post_to_agent(
        "/agents/extractor/trigger",
        payload,
        "Extractor Agent",
    )


def trigger_quest_maker(skill_id: str) -> dict:
    payload = {"skill_id": skill_id}
    return _post_to_agent(
        "/agents/quest-maker/trigger",
        payload,
        "Quest Maker Agent",
    )


def trigger_quest_maker_in_background(skill_id: str) -> None:
    try:
        trigger_quest_maker(skill_id)
    except (httpx.HTTPError, RuntimeError):
        logger.exception(
            "Could not trigger Quest Maker for newly created skill %s.",
            skill_id,
        )