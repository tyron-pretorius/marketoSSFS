##### Marketo REST API #####
#
# Most flow steps never need this file: Marketo sends you everything in the
# invocation payload and you send your answer back in the callback. Capture
# Consent is the exception - it has to read the form the person filled out,
# and write to their program membership, neither of which arrives in the
# payload. That means calling Marketo's own REST API.
#
# Set these three in your environment (.env locally, the Secrets pane on
# Replit). They come from Admin -> Web Services in your subscription:
#
#   MARKETO_BASE_URL       https://123-ABC-456.mktorest.com
#   MARKETO_CLIENT_ID      the API user's client id
#   MARKETO_CLIENT_SECRET  the API user's client secret
#
import json
import os
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

BASE_URL = (os.environ.get("MARKETO_BASE_URL") or "").rstrip("/")
CLIENT_ID = os.environ.get("MARKETO_CLIENT_ID")
CLIENT_SECRET = os.environ.get("MARKETO_CLIENT_SECRET")

TIMEOUT = 60

# Sync Program Member Data accepts at most 300 records per call.
PROGRAM_MEMBER_BATCH = 300

# Marketo allows 100 API calls per 20 seconds and 10 concurrent calls across
# the whole subscription. Capture Consent makes about five calls per batch -
# the campaign lookup, the program lookup, the form fields, then one write per
# 300 leads - so it stays well inside both without any pacing.
#
# A service that calls Marketo *per lead* would not. If you build one, put a
# rate limiter in _call below before you run it against a real batch, or the
# instance starts answering 606 and the failures look like your bug.
_token_lock = threading.Lock()
_token: Dict[str, object] = {"value": None, "expires_at": 0.0}


def _call(method: str, url: str, **kwargs):
    """Every Marketo request goes through here."""
    kwargs.setdefault("timeout", TIMEOUT)
    return requests.request(method, url, **kwargs)


# ---------- AUTH ----------
def _access_token() -> str:
    """Client-credentials token, cached process-wide until 60s before expiry.

    Marketo counts the token call against the same API limit as everything
    else, so fetching a fresh one per request wastes half your allowance.
    """
    with _token_lock:
        if _token["value"] and time.time() < float(_token["expires_at"]):
            return str(_token["value"])

        if not (BASE_URL and CLIENT_ID and CLIENT_SECRET):
            raise RuntimeError(
                "Missing Marketo REST secrets: set MARKETO_BASE_URL, "
                "MARKETO_CLIENT_ID and MARKETO_CLIENT_SECRET"
            )

        r = _call(
            "GET",
            f"{BASE_URL}/identity/oauth/token",
            params={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        )
        r.raise_for_status()
        body = r.json()

        _token["value"] = body["access_token"]
        _token["expires_at"] = time.time() + int(body.get("expires_in", 3600)) - 60
        logger.info("🔑 Marketo access token refreshed")
        return str(_token["value"])


def _auth_header() -> Dict[str, str]:
    # The token goes in the Authorization header, not a query parameter. As a
    # query parameter Marketo answers 603 "Access denied", which reads like a
    # permissions problem and is not one.
    return {"Authorization": f"Bearer {_access_token()}"}


def _check(body: Dict, what: str) -> Dict:
    """Marketo returns HTTP 200 with success:false, so status codes aren't enough."""
    if not body.get("success", False):
        raise RuntimeError(f"{what} failed: {json.dumps(body.get('errors', body))}")
    return body


# ---------- WHICH PROGRAM CALLED US ----------
def get_program_for_campaign(campaign_id) -> Tuple[int, str]:
    """Resolve the program that owns a smart campaign.

    Marketo's invocation payload only carries campaignId. The smart campaign
    asset record has a `folder` reference, which is either the program itself
    (type "Program") or a campaign folder nested inside one, so walk upwards
    until a Program turns up.

    Returns (program_id, program_name). Raises if the campaign isn't in a program.
    """
    if not campaign_id:
        raise RuntimeError("No campaignId in the Marketo payload")

    r = _call(
        "GET",
        f"{BASE_URL}/rest/asset/v1/smartCampaign/{campaign_id}.json",
        headers=_auth_header(),
    )
    body = _check(r.json(), f"Lookup of smart campaign {campaign_id}")
    result = body.get("result") or []
    if not result:
        raise RuntimeError(f"Smart campaign {campaign_id} not found")

    ref = result[0].get("folder") or {}

    # Bounded walk so a malformed folder tree can't spin forever.
    for _ in range(10):
        if not ref or not ref.get("id"):
            break
        if str(ref.get("type", "")).lower() == "program":
            program_id = int(ref["id"])
            return program_id, get_program_name(program_id)

        fr = _call(
            "GET",
            f"{BASE_URL}/rest/asset/v1/folder/{int(ref['id'])}.json",
            headers=_auth_header(),
        )
        fresult = _check(fr.json(), f"Lookup of folder {ref['id']}").get("result") or []
        if not fresult:
            break
        ref = fresult[0].get("parent") or {}

    raise RuntimeError(
        f"Smart campaign {campaign_id} is not inside a program - Capture Consent "
        "must be called from a smart campaign that lives in a program"
    )


def get_program(program_id: int) -> Dict[str, Any]:
    """The program asset record.

    Carries its own `url` into the Marketo UI, which is worth using rather
    than building: the fragment differs by program type (#ME for a marketing
    event, and so on), so a hand-built link would be wrong for some programs.
    """
    r = _call(
        "GET",
        f"{BASE_URL}/rest/asset/v1/program/{int(program_id)}.json",
        headers=_auth_header(),
    )
    result = _check(r.json(), f"Lookup of program {program_id}").get("result") or []
    if not result:
        raise RuntimeError(f"Program {program_id} not found")
    return result[0]


def get_program_name(program_id: int) -> str:
    """Program name for a program id."""
    return get_program(program_id)["name"]


# ---------- PROGRAM MEMBER FIELDS ----------
def sync_program_member_fields(program_id: int,
                               values: List[Tuple[int, Dict[str, Any]]]) -> Dict[int, Optional[str]]:
    """Write several program member fields for many leads, 300 per call.

    `values` is [(lead_id, {field_api_name: value, ...}), ...]. Returns
    {lead_id: None if written else error}.

    This endpoint updates existing program members only - it will not enroll
    anyone - so a lead who is not a member of the program comes back rejected.
    That is the single most common reason this service reports a failure, and
    it is a campaign design problem rather than a bug: the flow step has to
    run inside the program whose membership it is writing to.
    """
    outcome: Dict[int, Optional[str]] = {}

    for start in range(0, len(values), PROGRAM_MEMBER_BATCH):
        chunk = values[start:start + PROGRAM_MEMBER_BATCH]

        try:
            r = _call(
                "POST",
                f"{BASE_URL}/rest/v1/programs/{int(program_id)}/members.json",
                headers={**_auth_header(), "Content-Type": "application/json"},
                json={
                    "input": [
                        {"leadId": int(lead_id), **fields}
                        for lead_id, fields in chunk
                    ]
                },
            )
            body = r.json()
        except Exception as e:
            for lead_id, _ in chunk:
                outcome[lead_id] = f"Program member update request failed: {e}"
            continue

        if not body.get("success"):
            err = json.dumps(body.get("errors", body))
            for lead_id, _ in chunk:
                outcome[lead_id] = f"Program member update failed: {err}"
            continue

        results = body.get("result") or []
        for i, (lead_id, _) in enumerate(chunk):
            # Prefer matching on leadId, then seq, then input order.
            entry = (
                next((x for x in results if x.get("leadId") == lead_id), None)
                or next((x for x in results if x.get("seq") == i), None)
                or (results[i] if i < len(results) else None)
            )

            if entry is None:
                outcome[lead_id] = "No result returned by Marketo"
                continue

            status = str(entry.get("status", "")).lower()
            if status in ("updated", "created", "added"):
                outcome[lead_id] = None
            else:
                reasons = entry.get("reasons") or []
                detail = "; ".join(
                    x.get("message", json.dumps(x)) for x in reasons
                ) or "no reason given"
                outcome[lead_id] = (
                    f"Program member update {status or 'skipped'} - {detail}. "
                    "The person must already be a member of this program."
                )

        logger.info(f"🧾 Program member fields written for {len(chunk)} leads")

    return outcome


# ---------- FORMS ----------
def get_form_fields(form_id: int, status: str = "approved") -> List[Dict[str, Any]]:
    """Every field on one version of a form, in the order they appear.

    Order matters here: Capture Consent finds the consent wording by position
    (the rich text block below the opt-in checkbox), not by searching for
    keywords, so that legal can reword the copy without breaking the lookup.
    """
    r = _call(
        "GET",
        f"{BASE_URL}/rest/asset/v1/form/{int(form_id)}/fields.json",
        headers=_auth_header(),
        params={"status": status},
    )
    body = _check(r.json(), f"Field listing for form {form_id} ({status})")
    return body.get("result") or []
