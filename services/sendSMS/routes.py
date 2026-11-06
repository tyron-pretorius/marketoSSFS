# services/sendSMS/routes.py
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory
from datetime import datetime
import os, requests, pandas as pd, json, time, traceback, pytz
import concurrent.futures
from threading import Thread
import googlesheets_functions
from . import telnyx_functions

base = "sendSMS"
bp = Blueprint(base, __name__, url_prefix=f"/{base}")

# ---------- CONFIG ----------
SHEET_LEADS   = f"{base}Leads"
SHEET_BATCHES = f"{base}Batches"
SPREADSHEET_ID = "xxx" #change this
pacific        = pytz.timezone("America/Los_Angeles")
MAX_CELL  = 50_000
SAFE_SLICE = 48_000          # leave UTF-8 head-room
THREADS   = 50               # Sized for latency, not for rate: the limiter in
                             # telnyx_functions holds the account ceiling

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
                {"WWW-Authenticate": 'Basic realm=Workflow Pro Send SMS'}
            )

# ---------- SHEETS LOGGING ----------
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

def _short_error(exc: Exception, message: str = "") -> str:
    """A capped message plus the stack, for storing and reading.

    `message` is Telnyx's own wording where there is one; the exception's own
    text during an outage is a page of markup.
    """
    stack = "".join(traceback.format_tb(exc.__traceback__))
    return f"{(message or str(exc))[:telnyx_functions.MAX_ERROR_CHARS]}\n{stack}"

# ---------- STATIC ICONS ----------
@bp.route("/serviceIcon")
def service_icon():
    return send_file("wfp_logo_pink.png", mimetype="image/png")

@bp.route("/brandIcon")
def brand_icon():
    return send_file("wfp_logo_pink.png", mimetype="image/png")

# ---------- SSFS ENDPOINT ----------
def _send_one(obj: dict) -> dict:
    """Send one lead's SMS and report the outcome. Runs on a pool thread."""
    ctx_lead = obj.get("objectContext", {})
    ctx_step = obj.get("flowStepContext", {})

    result = {
        "lead_id":    ctx_lead.get("id"),
        "from_phone": ctx_step.get("from_phone", ""),
        "to_phone":   ctx_step.get("to_phone", ""),
        "message":    ctx_step.get("message", ""),
        "sms_response": "",
        "sms_delivery_error": "",
        "error": "",
    }

    try:
        result["sms_response"] = telnyx_functions.sendSMS(
            result["to_phone"], result["from_phone"], result["message"])
    except Exception as per_lead_err:
        # Telnyx's own code and wording go in their own column so the sheet can
        # be filtered by them; the stack stays in `error` for debugging. Both
        # are capped, because an outage answers with a page of markup.
        detail = telnyx_functions.describe_error(per_lead_err)
        result["sms_delivery_error"] = detail
        result["error"] = _short_error(per_lead_err, detail)

    return result

def _process_batch_async(data: dict, ts: str) -> None:
    """Do all the work, then call Marketo back. Runs off the request thread."""
    rows_leads:   list[dict] = []
    callback_objects: list[dict] = []
    cb_response = ""

    try:
        object_data = data.get("objectData", [])
        total = len(object_data)

        # The batch row is written here, inside the thread, rather than before
        # the 202: it is a Sheets write with its own retries, and holding the
        # response open for it is what makes Marketo time the invocation out.
        # It has to exist before _progress can update it.
        batch_row = {"timestamp": ts, "error": "",
                     "cb_response": f"0/{total} leads sent"}
        batch_row |= _split_long_text("request", json.dumps(data, ensure_ascii=False))
        _write_rows_with_retry([batch_row], SHEET_BATCHES, ts)

        # ---------- send, in parallel ----------
        # Each send is a round-trip to Telnyx, so a batch sent one at a time is
        # a batch spent waiting. The rate limiter, not the pool size, is what
        # keeps this inside the account ceiling.
        results: list[dict] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [pool.submit(_send_one, obj) for obj in object_data]
            for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                try:
                    results.append(future.result())
                except Exception as worker_err:
                    # _send_one handles its own failures, so reaching here means
                    # the worker itself died. Report it rather than lose a lead.
                    results.append({
                        "lead_id": "", "from_phone": "", "to_phone": "",
                        "message": "", "sms_response": "",
                        "sms_delivery_error": "",
                        "error": f"Worker exception: {worker_err}",
                    })
                if done % 50 == 0 or done == total:
                    _progress(ts, f"{done}/{total} leads sent")

        # ---------- build the callback ----------
        for r in results:
            success = not r["error"]
            callback_objects.append({
                "leadData": {"id": r["lead_id"]},
                "activityData": {
                    "from_phone":     r["from_phone"],
                    "to_phone_value": r["to_phone"],
                    "message":        r["message"],
                    "sms_response":   r["sms_response"],
                    "sms_error":      r["sms_delivery_error"],
                    "success":        success,
                },
            })
            rows_leads.append({
                "timestamp": ts,
                "lead_id": (f"https://app-ab20.marketo.com/leadDatabase/"
                            f"loadLeadDetail?leadId={r['lead_id']}"),
                "from_phone":   r["from_phone"],
                "to_phone":     r["to_phone"],
                "message":      r["message"],
                "sms_response": r["sms_response"],
                "sms_delivery_error": r["sms_delivery_error"],
                "error":        r["error"],
            })

        # ---------- single callback ----------
        r = requests.post(
            data["callbackUrl"],
            headers={
                "x-api-key":        data["apiCallBackKey"],
                "x-callback-token": data["token"],
                "Content-Type":     "application/json",
            },
            json={
                "munchkinId": "123",
                "objectData": callback_objects
            },
            timeout=10,
        )

        cb_response = r.text

        # ---------- write to Sheets ----------
        _write_rows_with_retry(rows_leads, SHEET_LEADS, ts)

        try:
            googlesheets_functions.updateBatchRow(
                ts,
                {"error": "" if r.ok else f"Callback HTTP {r.status_code}",
                 "cb_response": cb_response},
                SHEET_BATCHES, SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets logging error:", gs_err)

        if not r.ok:
            print(f"Callback failed: HTTP {r.status_code} - {r.text}")

    except Exception as e:
        try:
            googlesheets_functions.updateBatchRow(
                ts,
                {"error": _short_error(e), "cb_response": cb_response},
                SHEET_BATCHES, SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets error while logging fatal failure:", gs_err)

        print("Fatal error in background processing:", e)


@bp.route("/submitAsyncAction", methods=["POST"])
def submit_async_action():
    """Accept the invocation and hand the work to a background thread.

    Marketo is told 202 straight away, so the flow step is never sitting and
    waiting on this service. All the real work - and the callback that
    actually applies the lead updates - then happens in _process_batch_async.

    Because the HTTP response has already been sent by then, anything that
    goes wrong afterwards can only be logged and written to the batches
    sheet; it cannot be returned to Marketo. That is the trade-off for not
    holding the flow step open.
    """
    ts = datetime.now(pacific).strftime("%Y-%m-%d %H:%M:%S")
    data = request.get_json(force=True)

    try:
        Thread(target=_process_batch_async, args=(data, ts), daemon=True).start()
        return "", 202

    except Exception as e:
        fail_row = {
            "timestamp": ts,
            "error": f"Failed to start background processing: {e}\n{traceback.format_exc()}",
        }
        try:
            googlesheets_functions.writeDF2Sheet(pd.DataFrame([fail_row]), SHEET_BATCHES, SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets error while logging thread start failure:", gs_err)

        return jsonify({"error": "Failed to start processing"}), 500

@bp.route("/getServiceDefinition")
def get_service_definition():
    return jsonify({
        "apiName": "send-sms",
        "i18n": {
            "en_US": {
                "name": "Send SMS",
                "description": "Uses the Telnyx SMS API to send an SMS message",
                "triggerName": "SMS is Sent",
                "filterName":  "SMS was Sent"
            }
        },
        "primaryAttribute": "to_phone",

        "invocationPayloadDef": {
            "flowAttributes": [
                {
                    "apiName":  "to_phone",
                    "dataType": "string",
                    "description": "The phone number that should receive the text",
                    "i18n": {
                        "en_US": {
                            "name": "To Phone"
                        }
                    }
                },
                {
                    "apiName":  "from_phone",
                    "dataType": "string",
                    "description": "The phone number that should send the text",
                    "i18n": {
                        "en_US": {
                            "name": "From Phone"
                        }
                    }
                },
                {
                    "apiName":  "message",
                    "dataType": "text",
                    "description": "The message to be sent",
                    "i18n": {
                        "en_US": {
                            "name": "Message"
                        }
                    }
                }
            ],
            "userDrivenMapping": False,
            "fields": []
        },

        "callbackPayloadDef": {
                "attributes": [                    
                    {
                        "apiName":  "to_phone_value",
                        "dataType": "string",
                        "i18n": {
                            "en_US": {
                                "name":        "To Phone Value",
                                "description": "The phone number value that should receive the text"
                            }
                        }
                    },
                    {
                        "apiName":  "from_phone",
                        "dataType": "string",
                        "i18n": {
                            "en_US": {
                                "name":        "From Phone",
                                "description": "The phone number that should send the text"
                            }
                        }
                    },
                    {
                        "apiName":  "message",
                        "dataType": "text",
                        "i18n": {
                            "en_US": {
                                "name":        "Message",
                                "description": "The message to be sent"
                            }
                        }
                    },
                    {
                          "apiName": "sms_response",
                          "dataType": "text",
                          "i18n": { "en_US": { "name": "SMS Response",
                                               "description": "Response from Telnyx SMS API" } }
                    },
                    {
                          "apiName": "sms_error",
                          "dataType": "text",
                          "i18n": { "en_US": { "name": "SMS Error",
                                               "description": "Error from Replit script" } }
                    }
                ],
                "fields": [ ],
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
