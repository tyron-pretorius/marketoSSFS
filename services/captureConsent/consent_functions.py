##### Reading SMS consent wording off a Marketo form #####
import html
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytz

logger = logging.getLogger(__name__)

# Marketo caps a program member *string* field at 255 characters. Program
# member fields have no text/textarea type, so longer consent wording is split
# across two string fields and re-joined by simple concatenation.
FIELD_LIMIT = 255

# The form field the consent wording belongs to. The rich text block sitting
# directly below this checkbox is the SMS consent copy; the other rich text on
# the form is the general submit-consent notice.
CONSENT_CHECKBOX_FIELD = "sms_opt_in_program"

RICH_TEXT_TYPES = ("htmltext", "richtext")

# Formats Marketo's date tokens have been seen to render in, most specific
# first. The token is rendered by Marketo, not chosen by us, so several
# spellings have to be accepted.
_TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y",
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%b %d, %Y",
    "%B %d, %Y",
)


def html_to_plain_text(raw_html: str) -> str:
    """The wording a person actually read, with the markup taken out.

    Tags become spaces rather than being deleted, so text in adjacent blocks
    doesn't run together, and HTML entities are decoded (&amp; -> &).
    """
    if not raw_html:
        return ""

    # Script/style content is never visible wording.
    without_code = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", raw_html,
                          flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r"<[^>]+>", " ", without_code)
    decoded = html.unescape(without_tags)
    # Non-breaking spaces read as whitespace to a person, so treat them that way.
    return re.sub(r"\s+", " ", decoded.replace("\xa0", " ")).strip()


def find_consent_rich_text(fields: List[Dict[str, Any]],
                           checkbox_field: str = CONSENT_CHECKBOX_FIELD) -> str:
    """The raw HTML of the rich text block below the SMS opt-in checkbox.

    Picking it by position rather than by keyword means rewording the consent
    copy - which legal does - cannot break the lookup.
    """
    checkbox_index: Optional[int] = None
    for i, field in enumerate(fields):
        if str(field.get("id", "")) == checkbox_field:
            checkbox_index = i
            break

    if checkbox_index is None:
        raise RuntimeError(
            f"The form has no '{checkbox_field}' field, so there is no SMS "
            "opt-in checkbox to read the consent wording from"
        )

    for field in fields[checkbox_index + 1:]:
        if str(field.get("dataType", "")).strip().casefold() in RICH_TEXT_TYPES:
            text = field.get("text") or field.get("htmlText") or field.get("value") or ""
            if not str(text).strip():
                raise RuntimeError(
                    f"The rich text block below '{checkbox_field}' is empty"
                )
            return str(text)

    raise RuntimeError(
        f"No rich text block appears below '{checkbox_field}' on the form - the "
        "consent wording has to sit directly under the opt-in checkbox"
    )


def split_for_fields(text: str, limit: int = FIELD_LIMIT) -> Tuple[str, str, str]:
    """Split consent text across the two program member string fields.

    Returns (first, overflow, error). The split is at an exact character count,
    not a word boundary, so first + overflow reproduces the original text.
    `error` is non-empty when the text is too long for both fields, in which
    case nothing should be written - a consent record cut short is worse than
    no record at all.
    """
    if len(text) <= limit:
        return text, "", ""

    first = text[:limit]
    overflow = text[limit:]

    if len(overflow) > limit:
        return "", "", (
            f"Consent wording is {len(text)} characters, which will not fit in "
            f"two {limit}-character fields ({2 * limit} max). Shorten the rich "
            "text on the form or add a third field."
        )

    return first, overflow, ""


def parse_consent_timestamp(raw: str, tz: Any, now: Optional[datetime] = None) -> Tuple[str, str]:
    """Turn a form-fill time into a value Marketo's datetime field accepts.

    Returns (iso_8601, source). `source` records where the value came from, so
    the log shows what a record is based on.

    The "Fill Out Form" trigger carries no timestamp - confirmed against a real
    invocation - so in practice this always takes the fallback: the moment the
    flow step ran, in the timezone the caller passes. On a trigger campaign
    that is seconds after the submission.
    """
    now = now or datetime.now(tz)

    candidate = str(raw or "").strip()
    if not candidate:
        return now.isoformat(timespec="seconds"), "flow step run time (the trigger carries no timestamp)"

    # Marketo renders some tokens with a trailing timezone name, e.g.
    # "2026-09-26 16:06:35 PDT", which strptime cannot read. AM/PM looks the
    # same to that pattern and must survive, or every afternoon becomes morning.
    trailing = re.search(r"\s+\(?([A-Za-z]{2,5})\)?$", candidate)
    if trailing and trailing.group(1).upper() not in ("AM", "PM"):
        cleaned = candidate[:trailing.start()].strip()
    else:
        cleaned = candidate
    # "Z+0000" appears in Marketo asset timestamps and is not valid ISO.
    cleaned = re.sub(r"Z\+0000$", "+0000", cleaned)

    for fmt in _TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = tz.localize(parsed) if hasattr(tz, "localize") else parsed.replace(tzinfo=tz)
        return parsed.isoformat(timespec="seconds"), "form fill token"

    try:
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = tz.localize(parsed) if hasattr(tz, "localize") else parsed.replace(tzinfo=tz)
        return parsed.isoformat(timespec="seconds"), "form fill token"
    except ValueError:
        pass

    raise RuntimeError(
        f"Could not read '{candidate}' as a date and time. Check the token in "
        "the flow step resolves to a form fill date."
    )


def build_consent_values(form_fields: List[Dict[str, Any]],
                         checkbox_field: str = CONSENT_CHECKBOX_FIELD) -> Dict[str, str]:
    """Everything the service needs from the form, worked out once per batch."""
    raw_html = find_consent_rich_text(form_fields, checkbox_field)
    plain = html_to_plain_text(raw_html)

    if not plain:
        raise RuntimeError("The consent rich text has no readable wording once the HTML is removed")

    first, overflow, error = split_for_fields(plain)
    if error:
        raise RuntimeError(error)

    logger.info(f"📝 Consent wording read: {len(plain)} characters "
                f"({len(first)} + {len(overflow)} across two fields)")

    return {
        "raw_html": raw_html,
        "consent_text": plain,
        "consent_text_1": first,
        "consent_text_2": overflow,
    }


# Marketo nests the trigger inside each objectData entry under
# "triggeringContext" - not at the top level, and not under the
# "triggerContext" name used to request it. Confirmed against a real
# "Fill Out Form" invocation:
#
#   "triggeringContext": {
#       "activityTypeName": "Fill Out Form",
#       "primaryAttributeValue": "<program name>.<form name>",
#       "primaryAttributeValueId": 7048,
#       "attributes": {"Webform ID": "7048", "Form Fields": "<php>", ...}
#   }
#
# There is no timestamp anywhere in it, so the time of consent is the moment
# the flow step runs - seconds after the fill on a trigger campaign.
FORM_FILL_ACTIVITY = "fill out form"

# Submitted checkbox values that count as ticked. Marketo sends whatever the
# form posted, which differs by field type.
_TICKED = {"yes", "true", "on", "1"}


def parse_php_serialized_map(blob: str) -> Dict[str, str]:
    """The submitted form values out of Marketo's "Form Fields" string.

    Marketo serialises them PHP-style - a:28:{s:6:"module";s:11:"leadCapture";…}
    - where each length is a byte count. Parsed by walking those lengths
    rather than by splitting on quotes, because a submitted value can
    contain quotes and semicolons of its own.
    """
    data = (blob or "").encode("utf-8")
    opening = re.match(rb"a:(\d+):\{", data)
    if not opening:
        return {}

    def read(pos: int):
        head = data[pos:pos + 2]
        if head == b"s:":
            colon = data.index(b":", pos + 2)
            length = int(data[pos + 2:colon])
            start = colon + 2                      # step over :"
            value = data[start:start + length]
            return value.decode("utf-8", "replace"), start + length + 2  # and ";
        if head in (b"i:", b"b:", b"d:"):
            end = data.index(b";", pos)
            return data[pos + 2:end].decode("utf-8", "replace"), end + 1
        if head == b"N;":
            return "", pos + 2
        raise ValueError(f"unrecognised token at byte {pos}")

    fields: Dict[str, str] = {}
    pos = opening.end()
    try:
        for _ in range(int(opening.group(1))):
            key, pos = read(pos)
            value, pos = read(pos)
            fields[key] = value
    except (ValueError, IndexError):
        # A truncated or unexpected blob yields what was read so far rather
        # than nothing - a partial answer still beats discarding the lot.
        pass
    return fields


def read_trigger_context(obj: Dict[str, Any]) -> Dict[str, Any]:
    """What the "Fill Out Form" trigger says about this person's submission.

    Returns the form id and name, the activity type, and the submitted form
    values. The id comes straight from the trigger, so there is no lookup by
    name and no ambiguity between cloned programs that share a form name.
    """
    trigger = obj.get("triggeringContext") or {}
    attributes = trigger.get("attributes") or {}

    # "<program name>.<form name>" - the form name is after the last dot,
    # and a program name may contain dots of its own.
    qualified = str(trigger.get("primaryAttributeValue", "") or "")
    form_name = qualified.rsplit(".", 1)[-1].strip() if qualified else ""

    form_id = trigger.get("primaryAttributeValueId") or attributes.get("Webform ID") or ""
    try:
        form_id = int(str(form_id).strip())
    except (TypeError, ValueError):
        form_id = 0

    form_fields = parse_php_serialized_map(attributes.get("Form Fields", ""))

    # The page the form was filled out on. Marketo reports it as the referrer
    # of the submission; the form itself posts the same value as _mktoReferrer,
    # which covers a trigger that reports no referrer.
    landing_page_url = (
        str(attributes.get("Referrer URL", "") or "").strip()
        or str(form_fields.get("_mktoReferrer", "") or "").strip()
    )

    return {
        "form_id": form_id,
        "form_name": form_name,
        "qualified_name": qualified,
        "activity_type": str(trigger.get("activityTypeName", "") or ""),
        "landing_page_url": landing_page_url,
        "form_fields": form_fields,
        "present": bool(trigger),
    }


def consent_was_ticked(form_fields: Dict[str, str], checkbox_field: str) -> Tuple[bool, str]:
    """Whether the opt-in box was actually ticked on the submission.

    Returns (ticked, submitted_value). The submitted value is what makes the
    record defensible: it is what the person sent, not what a smart list
    believed about them.
    """
    value = str(form_fields.get(checkbox_field, "") or "").strip()
    return value.casefold() in _TICKED, value


def describe_context(context: Any, limit: int = 600) -> str:
    """The trigger verbatim, for an error that has to explain what did arrive."""
    try:
        rendered = json.dumps(context, ensure_ascii=False, default=str)
    except Exception:
        rendered = repr(context)
    return rendered if len(rendered) <= limit else rendered[:limit] + "…"
