# services/hearAbout/routes.py
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory
from datetime import datetime
import os, requests, pandas as pd, json, traceback, pytz
from threading import Thread
import googlesheets_functions
from . import formula_functions

base = "calcFormula"
bp = Blueprint(base, __name__, url_prefix=f"/{base}")

# ---------- CONFIG ----------
SHEET_LEADS   = f"{base}Leads"
SHEET_BATCHES = f"{base}Batches"
SPREADSHEET_ID = "xxx" #change this
pacific        = pytz.timezone("America/Los_Angeles")
MAX_CELL = 50000          # Sheets’ absolute limit
SAFE_SLICE = 48000        # leave UTF-8 head-room

# ---------- AUTH ----------
def _check_auth(username, password):
    return username == os.getenv("MARKETO_USER") and password == os.getenv("MARKETO_PASSWORD")

def _split_long_text(col_name: str, value: str | None) -> dict[str, str]:
    """Return {col_name: value} unless it would overflow a Sheets cell.
       Long UTF-8 strings are sliced into N columns:  request, request_2 …"""
    if not value:
        return {col_name: ""}
    b = value.encode("utf-8")
    if len(b) <= MAX_CELL:
        return {col_name: value}

    chunks = [b[i:i+SAFE_SLICE].decode("utf-8", "ignore")
              for i in range(0, len(b), SAFE_SLICE)]
    return {f"{col_name}{'' if i == 0 else '_'+str(i+1)}": s
            for i, s in enumerate(chunks)}

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
                {"WWW-Authenticate": 'Basic realm=Workflow Pro GPT Completion'}
            )

# ---------- STATIC ICONS ----------
@bp.route("/serviceIcon")
def service_icon():
    return send_file("wfp_logo_pink.png", mimetype="image/png")

@bp.route("/brandIcon")
def brand_icon():
    return send_file("wfp_logo_pink.png", mimetype="image/png")

# ---------- SSFS ENDPOINTS ----------
@bp.route("/getPicklist", methods=["POST"])
def get_picklist():

    try:
        payload = request.get_json(force=True) or {}
        field_name = (payload.get("name") or "").strip()

        if not field_name:
            return jsonify({"error": "Missing 'name' in request body"}), 400

        # ───────────── pick‑list definitions ─────────────
        if field_name == "data_type":
            choices = [
                {"displayValue": {"en_US": "int"},   "submittedValue": "int"},
                {"displayValue": {"en_US": "str"},   "submittedValue": "str"},
                {"displayValue": {"en_US": "bool"},  "submittedValue": "bool"},
                {"displayValue": {"en_US": "float"}, "submittedValue": "float"},
            ]
        else:
            return jsonify({"error": f"Unknown field name '{field_name}'"}), 400

        return jsonify({"choices": choices}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
def _process_batch_async(data: dict, timestamp: str) -> None:
    """Do all the work, then call Marketo back. Runs off the request thread."""
    rows_leads: list[dict] = []        # (optional) logging
    cb_response = ""
    
    try:
        
        callback_objects: list[dict] = []
        for obj in data.get("objectData", []):
            octx          = obj.get("objectContext", {})
            lead_id      = octx.get("id")

            # ── flow-step inputs (all are strings except the two numerics) ──
            ctx          = obj.get("flowStepContext", {})
            formula   = ctx.get("formula", "")          
            data_type   = ctx.get("data_type", "str")
            resp_field   = ctx.get("field")      # **API-name** only!
            answer = ""
            error = ""

            try:
                answer = formula_functions.compute_formula(formula,data_type)

                single_cb = {
                    "leadData": { "id": lead_id , resp_field: answer},
                    "activityData": {
                        "formula_value": formula,
                        "data_type":  data_type,
                        "field": resp_field,
                        "answer": answer,
                        "success":   True
                    }
                }

            except Exception as e:
                # still send a callback entry so the step doesn’t stall
                error = f"{e}\n{traceback.format_exc()}",
                single_cb = {
                    "leadData": { "id": lead_id },
                    "activityData": {
                        "formula_error": error,
                        "success":   False
                    }
                }

            callback_objects.append(single_cb)
            
            # ---- (optional) log one row per lead -----------
            rows_leads.append({
                "timestamp":    timestamp,
                "lead_id":      f"https://app-ab20.marketo.com/leadDatabase/loadLeadDetail?leadId={lead_id}",
                "formula":       formula,
                "data_type":          data_type,
                "response_field": resp_field,
                "answer": answer,
                "error": error,
                "callback_objects": str(single_cb)
            })

        # ─────────── ONE callback back to Marketo ────────────
        r = requests.post(
            data["callbackUrl"],
            headers={
                "x-api-key":        data["apiCallBackKey"],
                "x-callback-token": data["token"],
                "Content-Type":     "application/json"
            },
            json={ "munchkinId": "123", "objectData": callback_objects },
            timeout=10
        )

        cb_response = r.text

        # -------- 3️⃣  one batch-summary row (no width-matching) -------------
        batch_row = {
            "timestamp":   timestamp,
            "error":       "" if r.ok else f"Callback HTTP {r.status_code}",
            "cb_response": cb_response
        }

        # request JSON can be huge → fan it out so every cell stays <50 kB
        batch_row.update(_split_long_text("request", json.dumps(data)))

        # ------------ (optional) write logs -----------------
        try:
            df_leads   = pd.DataFrame(rows_leads)
            df_batches = pd.DataFrame([batch_row])
            googlesheets_functions.writeDF2Sheet(df_leads,   SHEET_LEADS,   SPREADSHEET_ID)
            googlesheets_functions.writeDF2Sheet(df_batches, SHEET_BATCHES, SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets logging error:", gs_err)

        if not r.ok:
            print(f"Callback failed: HTTP {r.status_code} - {r.text}")

    except Exception as e:
        fail_row = {
            "timestamp": timestamp,
            "error": f"{e}\n{traceback.format_exc()}",
            "cb_response": cb_response
        }

        fail_row |= _split_long_text("request", json.dumps(data, ensure_ascii=False))
        
        try:
            googlesheets_functions.writeDF2Sheet(pd.DataFrame([fail_row]), SHEET_BATCHES, SPREADSHEET_ID)
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
    timestamp = datetime.now(pacific).strftime("%Y-%m-%d %H:%M:%S")
    data = request.get_json(force=True)

    try:
        Thread(target=_process_batch_async, args=(data, timestamp), daemon=True).start()
        return "", 202

    except Exception as e:
        fail_row = {
            "timestamp": timestamp,
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
        "apiName": "calc-formula",
        "i18n": {
            "en_US": {
                "name": "Calculate Formula",
                "description": "Calculate an Excel formula",
                "triggerName": "Formula is Calculated",
                "filterName":  "Formula was Calculated"
            }
        },
        "primaryAttribute": "formula",

        "invocationPayloadDef": {
            "flowAttributes": [
                {
                    "apiName":  "formula",
                    "dataType": "text",
                    "description": "Open‑text field containing the formula to be calculated",
                    "i18n": {
                        "en_US": {
                            "name": "Formula"
                        }
                    }
                },
                {
                    "apiName":  "data_type",
                    "dataType": "string",
                    "description": "Data type for the output",
                    "i18n": {"en_US": {"name": "Response Data Type"}},
                    "hasPicklist": True,
                    "enforcePicklistSelect": True
                    
                },
                {
                    "apiName":  "field",
                    "dataType": "string",
                    "description": "Field to store the result",
                    "i18n": {
                        "en_US": {
                            "name": "Response Field"
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
                        "apiName":  "data_type",
                        "dataType": "string",
                        "i18n": {
                            "en_US": {
                                "name":        "Output Data Type",
                                "description": "Output Data Type"
                            }
                        }
                    },
                    {
                          "apiName": "field",
                          "dataType": "string",
                          "i18n": { "en_US": { "name": "Response Field",
                                               "description": "Field to store the GPT response" } }
                        },
                    
                    {     "apiName": "answer",        
                          "dataType": "text",
                          "i18n": { "en_US": { "name": "Formula result",                                                                                          
                                              "description": "Formula result" } }
                    },

                    {     "apiName": "formula_value",        
                          "dataType": "text",
                          "i18n": { "en_US": { "name": "Formula Value",                                                                                          
                                              "description": "Formula Value" } }
                    },
                    {     "apiName": "formula_error",        
                          "dataType": "text",
                          "i18n": { "en_US": { "name": "Formula Error",                                                                                          
                                              "description": "Formula Error" } }
                    }

                ],
                "fields": [],
                "userDrivenMapping": True
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
