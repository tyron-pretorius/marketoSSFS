# services/e164Formatting/routes.py
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory
from datetime import datetime
import os, requests, pandas as pd, json, re, traceback, pytz, time
import concurrent.futures
from threading import Thread
import googlesheets_functions
from . import openai_functions
from . import neutrino_functions
from .neutrino_functions import UNKNOWN

base = "e164Formatting"
bp = Blueprint(base, __name__, url_prefix=f"/{base}")

# ---------- CONFIG ----------
SHEET_LEADS    = f"{base}Leads"
SHEET_BATCHES  = f"{base}Batches"
SPREADSHEET_ID = "xxx" #change this
MARKETO_APP    = "https://app-ab20.marketo.com" #change this
pacific        = pytz.timezone("America/Los_Angeles")
MAX_CELL   = 50_000
SAFE_SLICE = 48_000          # leave UTF-8 head-room
THREADS    = 10              # OpenAI is called once per lead; Neutrino once per batch

# ---------- AUTH ----------
def _check_auth(username, password):
    return username == os.getenv("MARKETO_USER") and password == os.getenv("MARKETO_PASSWORD")

def _split_long_text(col_name: str, value: str) -> dict[str, str]:
    if not value:
        return {col_name: ""}
    b = value.encode("utf-8")
    if len(b) <= MAX_CELL:
        return {col_name: value}

    pieces = [b[i:i+SAFE_SLICE].decode("utf-8", "ignore")
              for i in range(0, len(b), SAFE_SLICE)]
    return {f"{col_name}{'' if i==0 else '_'+str(i+1)}": txt
            for i, txt in enumerate(pieces)}

@bp.before_app_request       # runs for every request in the whole app
def require_basic_auth():
    if request.path in (
        f"/{base}/install",
        f"/{base}/serviceIcon",
        f"/{base}/brandIcon",
    ):
        return
    if request.path.startswith(f"/{base}/status") or \
       request.path.startswith(f"/{base}/submitAsyncAction"):
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm=Workflow Pro E164 Formatting'}
            )

# ---------- STATIC ICONS ----------
@bp.route("/serviceIcon")
def service_icon():
    return send_file("wfp_logo_pink.png", mimetype="image/png")

@bp.route("/brandIcon")
def brand_icon():
    return send_file("wfp_logo_pink.png", mimetype="image/png")

# ---------- E164 HELPERS ----------
SYSTEM_PROMPT = """You are a helpful assistant that formats phone numbers into E164 format. 

Your approach will always be to: 
1. See if you can convert the phone number to E164 format using the 'Phone' field on its own 

2. If you cannot determine which country the phone number is from then you can use the 'Country' field in order to get the extension needed for E164 format 

3. If no 'Country' field is supplied and the number is exactly ten digits that fit the North American Numbering Plan - the area code and the exchange code each starting with a digit from 2 to 9, and no leading zero - then assume +1. Most of this audience is in the United States and Canada, so that shape is far more likely to be North American than anything else.

4. If for some reason you are not able to determine the E164 format for a number OR the number does not look like a genuine phone number then return 'Unknown' for the country extension and base number. A number with a leading zero, or with a digit count that does not fit the North American plan, is not North American - do not assume +1 for it

You will return the E164 formatted phone number in json format like so

{"e164 country extension" : "{{e164 country extension}}", "e164 base number" : "{{e164 base number}}"}"""

# Sent back when a reply is malformed - a missing plus sign, a doubled country
# code. Never sent for "Unknown", which is an answer rather than a mistake.
#
# A schematic rather than a worked example: any sample number has to pick a
# country, and this is a formatting correction, not an invitation to
# reconsider which country the number belongs to.
CORRECTION = (
    "\n\nPrevious attempt returned: {returned}, which is not valid E.164. "
    "The format is +[country calling code][national number], with no spaces, "
    "punctuation or leading zero, and 10 to 15 digits after the plus sign. "
    "Keep the country you already identified and correct only the formatting."
)

E164_RE = re.compile(r"^\+[1-9]\d{9,14}$")

def is_e164(number: str) -> bool:
    """E.164 is a plus sign, a country code, then the national number."""
    return E164_RE.fullmatch(number or "") is not None

def _field(payload: dict, name: str) -> str:
    """A value from the reply, found without caring how the key was cased.

    Lower-casing the whole reply before parsing would make key lookup robust
    at the cost of turning the model's "Unknown" into "unknown" - one state
    stored under two spellings.
    """
    for key, value in payload.items():
        if str(key).strip().casefold() == name:
            return str(value).strip()
    return ""

def _is_known(value: str) -> bool:
    """Whether a value is a real answer rather than any spelling of Unknown."""
    text = str(value or "").strip()
    return bool(text) and text.casefold() != "unknown"

def parse_openai_response(response_content: str) -> tuple[str, str, str, str]:
    """-> (e164_phone, country_ext, base_number, raw_response)"""
    e164_phone = country_ext = base_number = UNKNOWN
    openai_response = ""

    try:
        payload = json.loads(response_content)
        country_ext = _field(payload, "e164 country extension")
        base_number = _field(payload, "e164 base number")

        if _is_known(country_ext) and _is_known(base_number):
            e164_phone = f"{country_ext}{base_number}"
        else:
            # Whatever spelling the model used for "I cannot tell", it is
            # stored as the one the rest of the service uses.
            country_ext = base_number = UNKNOWN

        openai_response = response_content

    except (json.JSONDecodeError, TypeError) as json_error:
        # Not JSON - pull the two values out of the text instead.
        text = (response_content or "").lower()
        country_match = re.search(r'["\']?e164 country extension["\']?\s*:\s*["\']?([^"\',}]+)', text)
        base_match    = re.search(r'["\']?e164 base number["\']?\s*:\s*["\']?([^"\',}]+)', text)

        # Compared without regard to case: the searched text is lower-cased,
        # so comparing against "Unknown" could never match and a reply of
        # "unknown" would pass as a real answer.
        if (country_match and base_match
                and _is_known(country_match.group(1))
                and _is_known(base_match.group(1))):
            country_ext = country_match.group(1).strip()
            base_number = base_match.group(1).strip()
            e164_phone = f"{country_ext}{base_number}"
            openai_response = response_content
        else:
            openai_response = f"JSON parsing failed: {json_error}. Raw response: {response_content}"

    return e164_phone, country_ext, base_number, openai_response

def format_one_number(phone: str, country: str) -> tuple[str, str, str, str]:
    """Ask the model for one number's E.164 form. -> (e164, ext, base, raw)

    "Unknown" ends the attempt rather than starting another: the model has
    said it cannot place the number, and asking again supplies it with
    nothing new to change its mind. Only a malformed answer is retried,
    because that is the case another attempt can actually fix.
    """
    base_message = f"Phone: {phone}"
    if country:
        base_message += f"\nCountry: {country}"
    message = base_message

    e164_phone = country_ext = base_number = UNKNOWN
    openai_response = ""

    for attempt in range(3):
        response_content = openai_functions.completions(message, SYSTEM_PROMPT) or ""
        e164_phone, country_ext, base_number, openai_response = parse_openai_response(response_content)

        if e164_phone == UNKNOWN:
            break
        if is_e164(e164_phone):
            break

        # Built from the original each time rather than appended to, so a
        # third attempt carries one correction instead of two.
        message = base_message + CORRECTION.format(returned=e164_phone)

    return e164_phone, country_ext, base_number, openai_response

def _write_rows_with_retry(rows: list[dict], sheet_name: str, ts: str,
                           max_attempts: int = 3) -> None:
    """Write rows to Sheets, retrying in smaller chunks before giving up.

    A thousand-lead batch is one large write, and a single transient failure
    would otherwise lose the lot. Each size is tried with backoff, then a
    smaller one; a final failure is recorded on the batch row so the loss is
    visible rather than silent.
    """
    if not rows:
        return

    last_err = None
    for size in (len(rows), 10, 1):
        try:
            for start in range(0, len(rows), size):
                chunk = rows[start:start + size]
                for attempt in range(max_attempts):
                    try:
                        googlesheets_functions.writeDF2Sheet(
                            pd.DataFrame(chunk), sheet_name, SPREADSHEET_ID)
                        break
                    except Exception as e:
                        last_err = e
                        if attempt == max_attempts - 1:
                            raise
                        time.sleep(2 ** (attempt + 1))      # 2s, 4s
            return
        except Exception as e:
            last_err = e                                     # try a smaller size

    try:
        googlesheets_functions.updateBatchRow(
            ts, {"error": f"Failed to log {len(rows)} rows to {sheet_name}: {last_err}"},
            SHEET_BATCHES, SPREADSHEET_ID)
    except Exception as gs_err:
        print("Could not record the Sheets failure on the batch row:", gs_err)

def _progress(ts: str, note: str) -> None:
    """Best-effort note on the batch row, so a long batch visibly moves."""
    try:
        googlesheets_functions.updateBatchRow(
            ts, {"cb_response": note}, SHEET_BATCHES, SPREADSHEET_ID)
    except Exception:
        pass

# ---------- BACKGROUND WORK ----------
def _format_lead(obj: dict) -> dict:
    """One lead through OpenAI. Neutrino happens later, for the whole batch."""
    ctx_lead = obj.get("objectContext", {})
    ctx_step = obj.get("flowStepContext", {})
    lead_id  = ctx_lead.get("id")
    phone    = ctx_step.get("phone", "")
    country  = ctx_step.get("country", "")

    result = {"lead_id": lead_id, "phone": phone, "country": country,
              "e164_phone": UNKNOWN, "country_ext": UNKNOWN,
              "base_number": UNKNOWN, "openai_response": "", "error": ""}
    try:
        (result["e164_phone"], result["country_ext"],
         result["base_number"], result["openai_response"]) = format_one_number(phone, country)
    except Exception as per_lead_err:
        result["error"] = f"{per_lead_err}\n{traceback.format_exc()}"
    return result

def _process_batch_async(data: dict, ts: str) -> None:
    """Do all the work, then call Marketo back. Runs off the request thread."""
    rows_leads: list[dict] = []
    callback_objects: list[dict] = []
    cb_response = ""

    try:
        object_data = data.get("objectData", [])
        total = len(object_data)

        # The batch row is written here, inside the thread, rather than before
        # the 202: it is a Sheets write with its own retries, and holding the
        # response open for it is what makes Marketo time the invocation out.
        batch_row = {"timestamp": ts, "error": "", "cb_response": f"0/{total} leads processed"}
        batch_row |= _split_long_text("request", json.dumps(data, ensure_ascii=False))
        _write_rows_with_retry([batch_row], SHEET_BATCHES, ts)

        # ---------- phase one: OpenAI, one call per lead ----------
        with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [pool.submit(_format_lead, obj) for obj in object_data]
            results = []
            for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                results.append(future.result())
                if done % 50 == 0 or done == total:
                    _progress(ts, f"{done}/{total} leads processed (OpenAI phase)")

        # ---------- phase two: Neutrino, one call for the batch ----------
        _progress(ts, f"{total}/{total} leads processed (validating with Neutrino)")
        neutrino = neutrino_functions.validate_phones_bulk(
            [r["e164_phone"] for r in results if r["e164_phone"] != UNKNOWN])

        # ---------- build the callback ----------
        for r in results:
            checked = neutrino.get(r["e164_phone"], {})
            phone_type    = checked.get("phone_type", UNKNOWN)
            phone_country = checked.get("phone_country", UNKNOWN)
            valid         = checked.get("valid", "false")
            neutrino_response = str(checked.get("full_response") or checked.get("error") or "")

            success = not r["error"] and r["e164_phone"] != UNKNOWN

            callback_objects.append({
                "leadData": {
                    "id": r["lead_id"],
                    "phone_e164": r["e164_phone"],
                },
                "activityData": {
                    "phone_value":   r["phone"],
                    "country":       r["country"],
                    "e164_phone":    r["e164_phone"],
                    "country_ext":   r["country_ext"],
                    "base_number":   r["base_number"],
                    "valid":         valid,
                    "phone_type":    phone_type,
                    "phone_country": phone_country,
                    "success":       success,
                    "e164_error":    r["error"],
                },
            })

            rows_leads.append({
                "timestamp": ts,
                "lead_id": f"{MARKETO_APP}/leadDatabase/loadLeadDetail?leadId={r['lead_id']}",
                "phone": r["phone"],
                "country": r["country"],
                "e164_phone": r["e164_phone"],
                "phone_type": phone_type,
                "phone_country": phone_country,
                "valid": valid,
                "openai_response": r["openai_response"],
                "neutrino_response": neutrino_response,
                "error": r["error"],
            })

        # ---------- single callback ----------
        _progress(ts, f"{total}/{total} leads processed (sending callback)")
        r = requests.post(
            data["callbackUrl"],
            headers={
                "x-api-key":        data["apiCallBackKey"],
                "x-callback-token": data["token"],
                "Content-Type":     "application/json",
            },
            json={"munchkinId": "123", "objectData": callback_objects},
            timeout=120,
        )
        cb_response = r.text

        _write_rows_with_retry(rows_leads, SHEET_LEADS, ts)

        try:
            googlesheets_functions.updateBatchRow(
                ts,
                {"error": "" if r.ok else f"Callback HTTP {r.status_code}",
                 "cb_response": cb_response},
                SHEET_BATCHES, SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets error while recording the callback:", gs_err)

        if not r.ok:
            print(f"Callback failed: HTTP {r.status_code} - {r.text}")

    except Exception as e:
        try:
            googlesheets_functions.updateBatchRow(
                ts, {"error": f"{e}\n{traceback.format_exc()}", "cb_response": cb_response},
                SHEET_BATCHES, SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets error while logging fatal failure:", gs_err)

        print("Fatal error in background processing:", e)

# ---------- SSFS ENDPOINTS ----------
@bp.route("/submitAsyncAction", methods=["POST"])
def submit_async_action():
    """Accept the invocation and hand the work to a background thread.

    Marketo is told 202 straight away, so the flow step is never sitting and
    waiting on this service. Nothing that can block goes before that - even
    logging the batch happens inside the thread, because a slow Sheets write
    is enough for Marketo to give up on the invocation.

    Because the HTTP response has already been sent by then, anything that
    goes wrong afterwards can only be logged and written to the batches
    sheet; it cannot be returned to Marketo. That is the trade-off for not
    holding the flow step open.
    """
    # Microseconds because this doubles as the batch's key in the sheet.
    # Marketo fires parallel invocations inside the same second, and at
    # second precision they would all update the same row.
    ts = datetime.now(pacific).strftime("%Y-%m-%d %H:%M:%S.%f")
    data = request.get_json(force=True)

    try:
        Thread(target=_process_batch_async, args=(data, ts), daemon=True).start()
        return "", 202

    except Exception as e:
        print("Failed to start background processing:", e)
        return jsonify({"error": "Failed to start processing"}), 500

@bp.route("/getServiceDefinition")
def get_service_definition():
    return jsonify({
        "apiName": "e164-formatting",
        "i18n": {
            "en_US": {
                "name": "E164 Formatting",
                "description": "Formats a phone number into E.164 and validates it with Neutrino",
                "triggerName": "Phone is Formatted",
                "filterName": "Phone was Formatted"
            }
        },
        "primaryAttribute": "phone",

        "invocationPayloadDef": {
            "flowAttributes": [
                {
                    "apiName": "phone",
                    "dataType": "string",
                    "description": "The phone number to format - use a token such as {{lead.Phone Number}}",
                    "i18n": {"en_US": {"name": "Phone"}}
                },
                {
                    "apiName": "country",
                    "dataType": "string",
                    "description": "The person's country, used when the number alone does not identify one",
                    "i18n": {"en_US": {"name": "Country"}}
                }
            ],
            "userDrivenMapping": False,
            "fields": []
        },

        "callbackPayloadDef": {
            "attributes": [
                {
                    "apiName": "phone_value",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Phone Value", "description": "The phone number that was submitted"}}
                },
                {
                    "apiName": "country",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Country", "description": "The country that was submitted"}}
                },
                {
                    "apiName": "e164_phone",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "E164 Phone", "description": "The number in E.164 format, or Unknown"}}
                },
                {
                    "apiName": "country_ext",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Country Extension", "description": "The country calling code"}}
                },
                {
                    "apiName": "base_number",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Base Number", "description": "The national number without the country code"}}
                },
                {
                    "apiName": "valid",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Valid", "description": "Whether Neutrino recognised the number"}}
                },
                {
                    "apiName": "phone_type",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Phone Type", "description": "Mobile, landline and so on, from Neutrino"}}
                },
                {
                    "apiName": "phone_country",
                    "dataType": "string",
                    "i18n": {"en_US": {"name": "Phone Country", "description": "The country Neutrino places the number in"}}
                },
                {
                    "apiName": "e164_error",
                    "dataType": "text",
                    "i18n": {"en_US": {"name": "E164 Error", "description": "Error from the E164 Formatting service"}}
                }
            ],
            "fields": [
                {
                    "serviceAttribute": "phone_e164",
                    "dataType": "string",
                    "isRequired": True,
                    "description": "The person field that receives the E.164 formatted number",
                    "i18n": {"en_US": {"name": "E164 Phone", "description": "The person field that receives the E.164 formatted number"}}
                }
            ],
            "userDrivenMapping": False
        }
    })

@bp.route("/status")
def status():
    return jsonify({"status": "ok"})

@bp.route("/install")
def serve_openapi():
    return send_from_directory(
        directory=os.path.dirname(__file__),
        path="swagger.json",
        mimetype="application/json"
    )
