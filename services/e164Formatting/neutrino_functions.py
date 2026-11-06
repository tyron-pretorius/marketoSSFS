##### Neutrino phone validation #####
import os
import time
from typing import Any, Dict, List

import requests

# The one spelling of "we could not determine this". Values arrive from
# OpenAI and from Neutrino in whatever case each happens to use; normalising
# them means a column does not read Unknown in one row and unknown in the
# next, which looks like two states when it is one.
UNKNOWN = "Unknown"

# Neutrino's multi endpoint takes up to 2,500 sub-requests in one call, so a
# Marketo batch - 1,000 leads at most - always fits in a single request.
MULTI_URL = "https://neutrinoapi.net/multi"
TIMEOUT = 30


def _known(raw: Any) -> str:
    """A Neutrino value, or UNKNOWN when there isn't one.

    Neutrino answers a field it cannot determine with an empty string rather
    than leaving the key out, so `get(key, default)` never reaches its
    default. It also returns "unknown" in lower case as a real value, which
    is the same state written differently.
    """
    text = str(raw or "").strip()
    if not text or text.casefold() == "unknown":
        return UNKNOWN
    return text


def validate_phones_bulk(phone_numbers: List[str],
                         max_retries: int = 3) -> Dict[str, Dict[str, Any]]:
    """Validate every number in one request. Returns {phone: {...}}.

    Distinct numbers only. The same mobile often belongs to two records - a
    work one and a personal one - and Neutrino bills per sub-request, so
    sending it twice costs twice and answers the same. Results come back
    keyed by number, so every lead holding one still gets its answer.
    """
    seen = set()
    numbers = []
    for phone in phone_numbers:
        if phone and phone != UNKNOWN and phone not in seen:
            seen.add(phone)
            numbers.append(phone)

    if not numbers:
        return {}

    user_id = os.environ.get("NEUTRINO_USER_ID")
    api_key = os.environ.get("NEUTRINO_API_KEY")
    if not user_id or not api_key:
        return {phone: {"phone_type": UNKNOWN, "phone_country": UNKNOWN,
                        "valid": "false", "error": "Neutrino credentials not set"}
                for phone in numbers}

    body = {
        "user_id": user_id,
        "api_key": api_key,
        "output_case": "snake",
        # Position in `numbers` is the id, so results map straight back.
        "data": [{"endpoint": "phone-validate", "number": phone, "id": str(i)}
                 for i, phone in enumerate(numbers)],
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(MULTI_URL, json=body, timeout=TIMEOUT)

            if response.status_code == 200:
                results = {}
                for item in response.json():
                    index = int(item.get("id", 0))
                    if index < len(numbers):
                        results[numbers[index]] = {
                            "phone_type": _known(item.get("type")),
                            "phone_country": _known(item.get("country")),
                            "valid": str(item.get("valid", False)).lower(),
                            "full_response": item,
                            "error": None,
                        }
                return results

            print(f"Neutrino attempt {attempt + 1}: HTTP {response.status_code}")

        except Exception as e:
            print(f"Neutrino attempt {attempt + 1} failed: {e}")

        if attempt < max_retries - 1:
            time.sleep((2 ** attempt) * 2)

    # One timeout fails the whole batch together: every number is reported
    # rather than silently dropped.
    return {phone: {"phone_type": UNKNOWN, "phone_country": UNKNOWN,
                    "valid": "false", "error": "Neutrino unavailable"}
            for phone in numbers}
