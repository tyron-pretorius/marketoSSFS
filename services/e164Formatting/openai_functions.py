##### OpenAI #####
import os
import time
from typing import Optional

from openai import OpenAI

from rate_limiters import SlidingWindowRateLimiter

# Built on first use, not at import.
#
# main.py imports every service's blueprint, so anything that runs at import
# time runs for all of them. Constructing the client here meant a missing
# OPENAI_API_KEY stopped the whole app from starting - including the services
# that have nothing to do with OpenAI.
_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set, so the E164 Formatting service "
                "cannot run. Add it to .env locally or to the Secrets pane "
                "on Replit."
            )
        _client = OpenAI(api_key=api_key)
    return _client

# Requests per second, process-wide. One lead is one call, and the batch runs
# on a thread pool, so nothing else here is holding the rate down. The retry
# loop below already absorbs a 429, but absorbing one costs a visible wait
# and a lead that spends all three attempts comes back None - which this
# service records as "Unknown", a rate-limit problem wearing the disguise of
# an unreadable phone number. Pacing the calls avoids taking the 429 at all.
OPENAI_RPS = float(os.getenv("OPENAI_RPS", "20"))
_LIMITER = SlidingWindowRateLimiter(max_requests=int(OPENAI_RPS),
                                    window_seconds=1.0)

# gpt-6-luna is a reasoning model, and that changes how it has to be called:
#
#   * `max_completion_tokens`, never the legacy `max_tokens`, which it
#     rejects outright. The budget covers the model's thinking as well as its
#     answer, so a ceiling sized for the answer alone comes back empty - which
#     parses as a failure and looks like a model that cannot read a phone
#     number. Observed usage is around 60 tokens; the rest is head-room, and
#     only what is used is billed.
#
#   * No `temperature`. A reasoning model rejects the parameter rather than
#     ignoring it, so it is not sent at all.
MODEL = "gpt-6-luna"
MAX_COMPLETION_TOKENS = 2000


def completions(user_message: str, system_prompt: str,
                model: str = MODEL, max_retries: int = 3) -> Optional[str]:
    """One JSON completion, or None once the retries are spent."""
    for attempt in range(max_retries):
        try:
            _LIMITER.acquire()
            response = _get_client().chat.completions.create(
                model=model,
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
            result = response.choices[0].message.content
            return result.strip() if result else None

        except Exception as e:
            print(f"OpenAI attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                return None
            time.sleep(2 ** attempt)          # 1s, 2s, 4s
