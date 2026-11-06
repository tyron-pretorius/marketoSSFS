# services/sendSMS/telnyx_functions.py
import os
import telnyx

from rate_limiters import SlidingWindowRateLimiter

# Set on first use, not at import.
#
# main.py imports every service's blueprint, so anything that runs at import
# time runs for all of them. Reading the key here meant a missing
# TELNYX_API_KEY stopped the whole app from starting - including the services
# that have nothing to do with Telnyx.
_configured = False


def _configure() -> None:
    global _configured
    if not _configured:
        api_key = os.getenv("TELNYX_API_KEY")
        if not api_key:
            raise RuntimeError(
                "TELNYX_API_KEY is not set, so the Send SMS service cannot "
                "run. Add it to .env locally or to the Secrets pane on Replit."
            )
        telnyx.api_key = api_key
        _configured = True


# Messages per minute, account-wide. Telnyx answers anything above its
# ceiling with a 429, and sending in parallel means nothing else is holding
# the rate down, so every send takes a slot here first.
ACCOUNT_RPM = int(os.getenv("TELNYX_ACCOUNT_RPM", "3000"))
_WINDOW = 60.0
_ACCOUNT_LIMITER = SlidingWindowRateLimiter(max_requests=ACCOUNT_RPM,
                                            window_seconds=_WINDOW)

# A failing provider answers with an error page, not JSON. The raw text runs
# to roughly twelve thousand characters of markup, and recording it once per
# lead fills the log and the callback to Marketo with it.
MAX_ERROR_CHARS = 500


def describe_error(exc: Exception) -> str:
    """Telnyx's own wording for a failure, capped.

    The API reports problems as a list of errors carrying a code and a title.
    Those are worth keeping and worth filtering a sheet by; the HTML page an
    outage returns instead is neither, so whatever is found is capped.
    """
    errors = getattr(exc, "errors", None)
    if errors:
        first = errors[0]
        if isinstance(first, dict):
            code = first.get("code", "")
            title = first.get("title") or first.get("detail") or ""
            text = f"{code} {title}".strip() if code else str(title)
            return text[:MAX_ERROR_CHARS]
        return str(first)[:MAX_ERROR_CHARS]
    return str(exc)[:MAX_ERROR_CHARS]


def sendSMS(to_phone: str, from_phone: str, message: str):

    _configure()

    _ACCOUNT_LIMITER.acquire()

    response = telnyx.Message.create(from_=from_phone, to=to_phone, text=message)

    return response
