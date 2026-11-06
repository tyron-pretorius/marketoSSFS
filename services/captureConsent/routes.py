# services/captureConsent/routes.py
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory
from datetime import datetime
import os
import requests
import pandas as pd
import json
import traceback
import pytz
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict
from threading import Thread
import concurrent.futures
import threading
import time

import marketo_functions
from safe_errors import short_error
import googlesheets_functions
from . import consent_functions

base = "captureConsent"
bp = Blueprint(base, __name__, url_prefix=f"/{base}")

logger = logging.getLogger(__name__)

# ---------- CONFIG ----------
consent_sheet_leads = "Consent Leads"
consent_sheet_batches = "Consent Batches"
consent_spreadsheet_id = "xxx"  # change this
MARKETO_APP = "https://app-ab20.marketo.com"  # change this
pacific = pytz.timezone("America/Los_Angeles")
MAX_CELL = 50000          # Sheets' absolute limit
SAFE_SLICE = 48000        # leave UTF-8 head-room
BATCH_LOG_SIZE = 500      # Fallback chunk size for _write_rows_with_retry error recovery
THREADS = 10              # Marketo allows 10 concurrent API calls, so cap at 10

# Program member fields that receive the consent record. Both must already
# exist in Admin -> Program Member Fields; the API cannot create them.
#
# Program member fields have no text/textarea type, so the wording is split
# across two 255-character string fields and re-joined by concatenation.
#
# The time of consent is deliberately not written here - the smart campaign
# that calls this flow step sets it, which keeps one owner for that field
# rather than two writers racing on the same record.
CONSENT_TEXT_FIELD = "SMSOptInConsentText"
CONSENT_TEXT_OVERFLOW_FIELD = "SMSOptInConsentText2"

# ---------- AUTHENTICATION ----------
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
        if not auth or not auth.username or not auth.password or not _check_auth(auth.username, auth.password):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm=Workflow Pro Capture Consent'}
            )

# ---------- DATACLASSES ----------
@dataclass
class LeadData:
    """Data structure for lead information"""
    id: int

@dataclass
class ActivityData:
    """Data structure for activity information"""
    form_name_value: str
    form_id: str = ""
    program_name: str = ""
    consent_text: str = ""
    consent_timestamp: str = ""
    success: bool = True
    consent_error: str = ""

@dataclass
class CallbackObject:
    """Data structure for callback object"""
    leadData: LeadData
    activityData: ActivityData

# ---------- HELPER FUNCTIONS ----------
def _check_auth(username: str, password: str) -> bool:
    """Check authentication credentials"""
    return username == os.getenv("MARKETO_USER") and password == os.getenv("MARKETO_PASSWORD")

def _chunks(lst: List[Any], n: int):
    """Split list into chunks of size n"""
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def _write_rows_with_retry(rows: List[Dict[str, Any]],
                           sheet_name: str,
                           spreadsheet_id: str,
                           timestamp: str,
                           label_for_errors: str,
                           max_attempts: int = 3) -> None:
    """
    Try to write rows in decreasing chunk sizes with retries.
    On final failure, write the error to the batch row so it's visible.
    """
    if not rows:
        return

    sizes = [BATCH_LOG_SIZE, 10, 1] if len(rows) > BATCH_LOG_SIZE else [len(rows), 10, 1]

    last_err = None
    for size in sizes:
        try:
            for chunk in _chunks(rows, size):
                attempt = 0
                while attempt < max_attempts:
                    try:
                        googlesheets_functions.writeDF2Sheet(
                            pd.DataFrame(chunk), sheet_name, spreadsheet_id)
                        break  # chunk succeeded
                    except Exception as e:
                        last_err = e
                        attempt += 1
                        if attempt < max_attempts:
                            time.sleep(2 ** attempt)  # backoff: 2s, 4s, …
                        else:
                            raise
            return  # all chunks succeeded at this size
        except Exception as e:
            last_err = e
            # try next (smaller) size

    try:
        googlesheets_functions.updateBatchRow(
            timestamp,
            {"error": f"Failed to log {len(rows)} {label_for_errors} rows to '{sheet_name}': {last_err}"},
            consent_sheet_batches,
            consent_spreadsheet_id
        )
        logger.error(f"❌ Failed to log {len(rows)} {label_for_errors} rows after all retries: {last_err}")
    except Exception as e2:
        logger.error(f"💥 Failed to record Sheets logging error to batch row: {e2}")

def _split_long_text(col_name: str, value: str) -> Dict[str, str]:
    """Return {col_name: value} unless it would overflow a Sheets cell.
       Long UTF-8 strings are sliced into N columns:  col_name, col_name_2 …"""
    if not value:
        return {col_name: ""}
    b = value.encode("utf-8")
    if len(b) <= MAX_CELL:
        return {col_name: value}

    chunks = [b[i:i+SAFE_SLICE].decode("utf-8", "ignore")
              for i in range(0, len(b), SAFE_SLICE)]
    return {f"{col_name}{'' if i == 0 else '_'+str(i+1)}": s
            for i, s in enumerate(chunks)}

def _update_progress(timestamp: str, text: str) -> None:
    """Best-effort progress note on the batch row"""
    try:
        googlesheets_functions.updateBatchRow(
            timestamp, {"cb_response": text}, consent_sheet_batches, consent_spreadsheet_id
        )
    except Exception:
        pass

def create_callback_object(lead_id: int, form_name: str, form_id: str = "",
                           program_name: str = "", consent_text: str = "",
                           consent_timestamp: str = "", success: bool = True,
                           error: str = "") -> CallbackObject:
    """Create a callback object with the given data.

    leadData carries only the id - the consent record lives on the program
    member, not on lead fields.
    """
    return CallbackObject(
        leadData=LeadData(id=lead_id),
        activityData=ActivityData(
            form_name_value=form_name,
            form_id=form_id,
            program_name=program_name,
            consent_text=consent_text,
            consent_timestamp=consent_timestamp,
            success=success,
            consent_error=error
        )
    )

def process_lead(obj: Dict[str, Any], batch_form_name: str,
                 setup_error: str) -> Dict[str, Any]:
    """Work out one person's consent record.

    The consent wording is the same for every lead in the batch - it is read
    from the form once - so this only resolves the per-person timestamp and
    checks the lead was invoked against the same form as the rest of the batch.
    """
    octx = obj.get("objectContext", {})
    lead_id = octx.get("id")

    # Everything comes from this person's own triggering context. There is no
    # timestamp in it, so the time of consent is the moment the flow step runs
    # - seconds after the fill on a trigger campaign.
    trigger = consent_functions.read_trigger_context(obj)
    form_name = trigger["form_name"]

    result = {
        "lead_id": lead_id,
        "form_name": form_name,
        "landing_page_url": trigger["landing_page_url"],
        "opt_in_value": "",
        "consent_timestamp": "",
        "error_msg": ""
    }

    try:
        if setup_error:
            raise RuntimeError(setup_error)

        # The batch reads the form once, so a lead pointed at a different form
        # would silently get the wrong consent wording.
        # The submission itself has to show the box was ticked. A smart list
        # can be built wrongly; what the person actually sent cannot.
        ticked, submitted = consent_functions.consent_was_ticked(
            trigger["form_fields"], consent_functions.CONSENT_CHECKBOX_FIELD
        )
        result["opt_in_value"] = submitted
        if not ticked:
            raise RuntimeError(
                f"The submission did not tick '{consent_functions.CONSENT_CHECKBOX_FIELD}' "
                f"(it sent {submitted!r}), so there is no consent to record."
            )

        if form_name and str(form_name).strip().casefold() != str(batch_form_name).strip().casefold():
            raise RuntimeError(
                f"This lead was triggered by form '{form_name}' but the batch "
                f"read '{batch_form_name}'. Every lead in one flow step run must "
                "use the same form."
            )

        # The "Fill Out Form" trigger carries no timestamp, so this is the
        # moment the flow step ran - seconds after the submission on a trigger
        # campaign.
        # Seconds precision: Marketo accepts a datetime carrying microseconds,
        # answers "updated", and stores midnight with the time thrown away.
        # The smart campaign owns this field now, but the value is logged and
        # reported, so it should be one Marketo would have accepted.
        iso_timestamp = datetime.now(pacific).isoformat(timespec="seconds")
        result["consent_timestamp"] = iso_timestamp
        logger.info(f"🖊️  Consent timestamp for lead {lead_id}: {iso_timestamp}")

    except Exception as e:
        result["error_msg"] = short_error(e)
        logger.error(f"❌ Consent capture failed for lead {lead_id}: {e}")

    return result

def _log_initial_batch(data: Dict[str, Any], timestamp: str) -> None:
    """Record the invocation as it arrived, before any of it is processed.

    Runs inside the background thread rather than in the request, so a slow
    Sheets write delays the log rather than the 202 Marketo is waiting for.
    """
    total = len(data.get("objectData", []))

    request_data = {
        "token": data.get("token", ""),
        "campaignId": data.get("campaignId", ""),
        "leadCount": total,
        "callbackUrl": data.get("callbackUrl", ""),
        "context": data.get("context", {}),
        "apiCallBackKey": data.get("apiCallBackKey", ""),
        "objectData": data.get("objectData", [])
    }

    initial_batch_row = {
        "timestamp": timestamp,
        "error": "",
        "cb_response": f"0/{total} leads processed"
    }
    initial_batch_row.update(_split_long_text("request", json.dumps(request_data, ensure_ascii=False)))

    _write_rows_with_retry(
        [initial_batch_row], consent_sheet_batches, consent_spreadsheet_id, timestamp,
        label_for_errors="initial-batch"
    )
    logger.info(f"📝 Initial batch logged for timestamp: {timestamp}")


def _process_batch_async(data: Dict[str, Any], timestamp: str) -> None:
    """Process batch asynchronously in background thread"""
    # The batch row has to exist before anything tries to update it.
    try:
        _log_initial_batch(data, timestamp)
    except Exception as log_err:
        logger.error(f"❌ Could not log the initial batch row: {log_err}")

    try:
        object_data = data.get("objectData", [])
        total_leads = len(object_data)
        overall_success = True
        processed_count = 0

        logger.info(f"🚀 Background processing started for {total_leads} leads at {timestamp}")

        # ----- resolve the form and read its consent wording once per batch -----
        # Marketo only sends campaignId, so the program is looked up from that,
        # then the form comes straight off the trigger. A failure here hits
        # every lead, so it is recorded and reported per lead rather than
        # dropping the batch silently.
        program_id: Optional[int] = None
        program_name = ""
        program_url = ""
        form_id: Optional[int] = None
        batch_form_name = ""
        consent: Dict[str, str] = {}
        setup_error = ""

        try:
            first = object_data[0] if object_data else {}
            trigger = consent_functions.read_trigger_context(first)
            batch_form_name = trigger["form_name"]
            form_id = trigger["form_id"]

            if not trigger["present"]:
                raise RuntimeError(
                    "No triggering context arrived, so there is no form to read "
                    "consent wording from. Capture Consent has to run from a "
                    "'Fills Out Form' trigger. The lead's payload held: "
                    + consent_functions.describe_context(first)
                )

            if not form_id:
                raise RuntimeError(
                    f"The trigger was '{trigger['activity_type'] or 'unknown'}', which "
                    "carries no form. Capture Consent has to run from a "
                    "'Fills Out Form' trigger. Trigger held: "
                    + consent_functions.describe_context(first.get("triggeringContext"))
                )

            program_id, program_name = marketo_functions.get_program_for_campaign(
                data.get("campaignId")
            )
            logger.info(f"🏷️  Calling program resolved: {program_name} ({program_id})")
            # Marketo hands out the program's own link; building one would get
            # the fragment wrong for program types other than this one.
            try:
                program_url = marketo_functions.get_program(program_id).get("url", "") or ""
            except Exception as url_err:
                logger.warning(f"⚠️  Could not read the program's url: {url_err}")

            # No lookup by name: the trigger named the form by id, so cloned
            # programs sharing a form name are no longer ambiguous.
            logger.info(f"📄 Form from trigger: {batch_form_name} ({form_id})")

            form_fields = marketo_functions.get_form_fields(form_id)
            consent = consent_functions.build_consent_values(form_fields)

            _update_progress(
                timestamp,
                f"0/{total_leads} leads processed (program {program_name}, form {form_id})"
            )
        except Exception as setup_err:
            setup_error = f"{setup_err}"
            overall_success = False
            logger.error(f"💥 Batch setup failed: {setup_error}")

        results: List[Dict[str, Any]] = []
        results_lock = threading.Lock()
        all_lead_log_rows: List[Dict[str, Any]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as executor:
            futures = [executor.submit(process_lead, obj, batch_form_name, setup_error)
                       for obj in object_data]

            pending = set(futures)
            while pending:
                done, pending = concurrent.futures.wait(
                    pending, timeout=0.1,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    try:
                        with results_lock:
                            results.append(fut.result())
                    except Exception as lead_err:
                        overall_success = False
                        with results_lock:
                            results.append({
                                "lead_id": "",
                                "form_name": "",
                                "landing_page_url": "",
                                "opt_in_value": "",
                                "consent_timestamp": "",
                                "error_msg": f"Worker exception: {lead_err}"
                            })
                    processed_count += 1

                if processed_count % 50 == 0 or processed_count == total_leads:
                    _update_progress(timestamp, f"{processed_count}/{total_leads} leads processed (consent phase)")

        logger.info(f"📋 Consent phase complete for {len(results)} leads")

        # ----- write the program member fields -----
        # Batched at 300 per call. This endpoint updates existing members only,
        # so anyone who is not a member of the program comes back rejected and
        # is marked as a failed lead.
        field_errors: Dict[int, Optional[str]] = {}
        pending_writes: List[Tuple[int, Dict[str, Any]]] = [
            (int(r["lead_id"]), {
                CONSENT_TEXT_FIELD: consent.get("consent_text_1", ""),
                CONSENT_TEXT_OVERFLOW_FIELD: consent.get("consent_text_2", ""),
            })
            for r in results
            if r["consent_timestamp"] and not r["error_msg"] and r["lead_id"] not in ("", None)
        ]

        if pending_writes and program_id:
            _update_progress(timestamp, f"{len(results)}/{total_leads} leads processed (writing consent text)")
            try:
                field_errors = marketo_functions.sync_program_member_fields(
                    program_id, pending_writes
                )
            except Exception as sync_err:
                logger.error(f"💥 Program member write failed wholesale: {sync_err}")
                field_errors = {lid: f"{sync_err}" for lid, _ in pending_writes}

        # ----- build callback objects and lead log rows -----
        callback_objects_all: List[CallbackObject] = []
        for result in results:
            error_msg = result["error_msg"]

            # A rejected program member write means nothing was recorded against
            # the person, so the lead has to count as failed.
            try:
                field_err = field_errors.get(int(result["lead_id"]))
            except (TypeError, ValueError):
                field_err = None
            if field_err and not error_msg:
                error_msg = field_err

            success = not error_msg
            if not success:
                overall_success = False

            callback_object = create_callback_object(
                lead_id=result["lead_id"],
                form_name=result["form_name"] or batch_form_name,
                form_id=str(form_id or ""),
                program_name=program_name,
                consent_text=consent.get("consent_text", "") if success else "",
                consent_timestamp=result["consent_timestamp"],
                success=success,
                error=error_msg
            )
            callback_objects_all.append(callback_object)

            all_lead_log_rows.append({
                # The moment recorded against the person in Marketo, not the
                # moment the batch happened to start - this row is a consent
                # record, so its timestamp should be the one that was stored.
                "timestamp": result["consent_timestamp"] or timestamp,
                "lead_id": f"{MARKETO_APP}/leadDatabase/loadLeadDetail?leadId={result['lead_id']}",
                "program_id": program_url or (program_id or ""),
                "program_name": program_name,
                "form_id": form_id or "",
                "form_name": result["form_name"] or batch_form_name,
                "landing_page_url": result["landing_page_url"],
                "opt_in_value": result["opt_in_value"],
                "consent_text_1": consent.get("consent_text_1", ""),
                "consent_text_2": consent.get("consent_text_2", ""),
                "error": error_msg,
                "callback_object": str(asdict(callback_object))
            })

        # Single batch write to leads sheet (Marketo never sends >1000 leads)
        logger.info(f"📊 Writing all {len(all_lead_log_rows)} lead logs to Google Sheets in single batch...")
        _update_progress(timestamp, f"{len(results)}/{total_leads} leads processed (Writing to Google Sheets)")

        if all_lead_log_rows:
            try:
                _write_rows_with_retry(
                    all_lead_log_rows, consent_sheet_leads, consent_spreadsheet_id, timestamp,
                    label_for_errors=f"all-leads-batch (size={len(all_lead_log_rows)})"
                )
                logger.info(f"✅ Successfully wrote {len(all_lead_log_rows)} lead logs to Google Sheets")
            except Exception as gs_err:
                logger.error(f"❌ Failed to write lead logs to Google Sheets: {gs_err}")

        _update_progress(timestamp, f"{len(results)}/{total_leads} leads processed (Logged to Sheets, sending callback)")

        # Send single callback to Marketo with all leads (token is one-time use)
        all_dict_objects = [
            {"leadData": asdict(o.leadData), "activityData": asdict(o.activityData)}
            for o in callback_objects_all
        ]

        logger.info(f"🚀 Sending single callback with {len(all_dict_objects)} leads to Marketo")
        callback_response_text = ""

        try:
            start_time = time.time()
            r = requests.post(
                data["callbackUrl"],
                headers={
                    "x-api-key": data["apiCallBackKey"],
                    "x-callback-token": data["token"],
                    "Content-Type": "application/json"
                },
                json={"munchkinId": "123", "objectData": all_dict_objects},
                timeout=120
            )
            callback_response_text = r.text
            logger.info(f"⏱️  Callback response time: {time.time() - start_time:.2f} seconds")
            logger.info(f"📋 HTTP Status: {r.status_code}")

            if r.ok:
                logger.info(f"✅ Callback SUCCESSFUL for all {len(all_dict_objects)} leads")
            else:
                overall_success = False
                logger.error(f"❌ Callback FAILED: HTTP {r.status_code} - {callback_response_text}")

        except requests.exceptions.Timeout as timeout_err:
            overall_success = False
            callback_response_text = f"Timeout after 120s: {timeout_err}"
            logger.error("⏰ TIMEOUT for callback")
        except requests.exceptions.RequestException as req_err:
            overall_success = False
            callback_response_text = f"Request error: {req_err}"
            logger.error("🌐 REQUEST ERROR for callback")
        except Exception as callback_err:
            overall_success = False
            callback_response_text = f"Error: {callback_err}"
            logger.error(f"💥 GENERAL ERROR for callback: {traceback.format_exc()}")

        batch_updates = {
            "error": "" if overall_success else "One or more leads failed",
            "cb_response": callback_response_text
        }
        batch_updates.update(_split_long_text(
            "callback_object",
            json.dumps({"munchkinId": "123", "objectData": all_dict_objects}, ensure_ascii=False)
        ))

        try:
            googlesheets_functions.updateBatchRow(timestamp, batch_updates, consent_sheet_batches, consent_spreadsheet_id)
            logger.info(f"✅ Batch status updated for timestamp: {timestamp}")
        except Exception as gs_err:
            logger.error(f"❌ Failed to update batch status: {gs_err}")

        logger.info(f"🎉 Background processing completed for timestamp: {timestamp}")

    except Exception as e:
        error_msg = "Background processing error: " + short_error(e)
        logger.error(f"💥 FATAL ERROR in background processing: {error_msg}")

        try:
            if 'all_lead_log_rows' in locals() and all_lead_log_rows:
                _write_rows_with_retry(
                    all_lead_log_rows, consent_sheet_leads, consent_spreadsheet_id, timestamp,
                    label_for_errors=f"fatal-error-recovery (size={len(all_lead_log_rows)})"
                )
        except Exception as save_err:
            logger.error(f"❌ Failed to save lead logs during fatal error: {save_err}")

        try:
            googlesheets_functions.updateBatchRow(
                timestamp, {"error": error_msg, "cb_response": ""},
                consent_sheet_batches, consent_spreadsheet_id
            )
        except Exception as gs_err:
            logger.error(f"Failed to log batch error: {gs_err}")

# ---------- STATIC ICONS ----------
@bp.route("/serviceIcon")
def service_icon() -> Response:
    """Returns 32×32 PNG for the Flow Step bar"""
    return send_file("wfp_logo_pink.png", mimetype="image/png")

@bp.route("/brandIcon")
def brand_icon() -> Response:
    """Returns 32×32 PNG for the admin listing"""
    return send_file("wfp_logo_pink.png", mimetype="image/png")

# ---------- SSFS ENDPOINTS ----------
@bp.route("/submitAsyncAction", methods=["POST"])
def submit_async_action():
    """Accept the invocation and hand the work to a background thread.

    Marketo is told 202 straight away, so the flow step is never sitting and
    waiting on this service. Everything else - reading the form, writing the
    program member fields, and the callback that applies the lead updates -
    happens in _process_batch_async.
    """
    # Microseconds included because this doubles as the batch key. Marketo
    # fires parallel invocations within the same second, and at second
    # resolution they all key to the same row: the first batch collects
    # everyone's progress updates and the rest sit at 0 forever.
    timestamp = datetime.now(pacific).strftime("%Y-%m-%d %H:%M:%S.%f")
    data = request.get_json(force=True)

    try:
        Thread(target=_process_batch_async, args=(data, timestamp), daemon=True).start()
        return "", 202

    except Exception as e:
        logger.error(f"💥 Failed to start background processing: {e}")
        return jsonify({"error": "Failed to start processing"}), 500

@bp.route("/getServiceDefinition")
def get_service_definition() -> Response:
    """Returns the SSFS service definition"""
    return jsonify({
        "apiName": "capture-consent",
        "i18n": {
            "en_US": {
                "name": "Capture Consent",
                "description": "Reads the consent wording off the form the person filled out and records it, with the time they consented, on their program membership",
                "triggerName": "Consent is Captured",
                "filterName": "Consent was Captured"
            }
        },
        # Marketo always renders the primary attribute on the flow step and it
        # has to be one of flowAttributes, so a service that needs no input
        # still shows one field. This one is a note for whoever opens the
        # campaign; its value is never read.
        "primaryAttribute": "note",

        "invocationPayloadDef": {
            "flowAttributes": [
                {
                    "apiName": "note",
                    "dataType": "string",
                    "description": "Optional note for whoever opens this campaign. The form and the time of consent come from the trigger, so nothing needs filling in",
                    "i18n": {
                        "en_US": {
                            "name": "Note"
                        }
                    }
                }
            ],
            # The form that was filled out, straight from the "Fills Out Form"
            # trigger, so a marketer types nothing and a cloned program needs
            # no edit. Marketo nests what it sends back under
            # "triggeringContext" inside each objectData entry - not at the
            # top level, and not under the "triggerContext" name used here to
            # request it.
            "triggerContext": True,
            "userDrivenMapping": False,
            "fields": []
        },

        "callbackPayloadDef": {
            "attributes": [
                {
                    "apiName": "form_name_value",
                    "dataType": "string",
                    "i18n": {
                        "en_US": {
                            "name": "Form Name Value",
                            "description": "The name of the form the consent wording was read from"
                        }
                    }
                },
                {
                    "apiName": "form_id",
                    "dataType": "string",
                    "i18n": {
                        "en_US": {
                            "name": "Form Id",
                            "description": "The id of the form inside the calling program"
                        }
                    }
                },
                {
                    "apiName": "program_name",
                    "dataType": "string",
                    "i18n": {
                        "en_US": {
                            "name": "Program Name",
                            "description": "The program that owns the form and the membership record"
                        }
                    }
                },
                {
                    "apiName": "consent_text",
                    "dataType": "text",
                    "i18n": {
                        "en_US": {
                            "name": "Consent Text",
                            "description": "The consent wording that was shown on the form"
                        }
                    }
                },
                {
                    "apiName": "consent_timestamp",
                    "dataType": "string",
                    "i18n": {
                        "en_US": {
                            "name": "Consent Timestamp",
                            "description": "When the person gave consent"
                        }
                    }
                },
                {
                    "apiName": "consent_error",
                    "dataType": "text",
                    "i18n": {
                        "en_US": {
                            "name": "Consent Error",
                            "description": "Error from the Capture Consent service"
                        }
                    }
                }
            ],
            "fields": [],
            "userDrivenMapping": False
        }
    })

@bp.route("/status")
def status() -> Response:
    """Service health check"""
    return jsonify({"status": "ok"})

@bp.route("/install")
def serve_openapi() -> Response:
    """Serve the OpenAPI specification"""
    return send_from_directory(
        directory=os.path.dirname(__file__),
        path="swagger.json",
        mimetype="application/json"
    )
