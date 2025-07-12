# services/hearAbout/routes.py
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory
from datetime import datetime
import os, requests, pandas as pd, json, traceback, pytz
import googlesheets_functions
from . import telnyx_functions

bp = Blueprint("sendSMS", __name__, url_prefix="/sendSMS")

# ---------- CONFIG ----------
base = "sendSMS"
SMS_SHEET_LEADS   = f"{base}Leads"
SMS_SHEET_BATCHES = f"{base}Batches"
SMS_SPREADSHEET_ID = "1DqUYub7vrnhEw2N5LOWRAWAZwYAzE3P-E-RPXqRhkws"
pacific        = pytz.timezone("America/Los_Angeles")
MAX_CELL  = 50_000
SAFE_SLICE = 48_000          # leave UTF-8 head-room

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

# ---------- STATIC ICONS ----------
@bp.route("/serviceIcon")
def service_icon():
    return send_file("wfp_logo_pink.png", mimetype="image/png")

@bp.route("/brandIcon")
def brand_icon():
    return send_file("wfp_logo_pink.png", mimetype="image/png")

# ---------- SSFS ENDPOINT ----------
@bp.route("/submitAsyncAction", methods=["POST"])
def submit_async_action():
    ts  = datetime.now(pacific).strftime("%Y-%m-%d %H:%M:%S")
    rows_leads:   list[dict] = []
    callback_objects: list[dict] = []
    cb_response = ""
    data = request.get_json(force=True)

    try:

        for obj in data.get("objectData", []):
            ctx_lead   = obj.get("objectContext", {})
            ctx_step   = obj.get("flowStepContext", {})
            from_phone   = ctx_step.get("from_phone", "")
            to_phone   = ctx_step.get("to_phone", "")
            message    = ctx_step.get("message", "")
            lead_id    = ctx_lead.get("id")

            row = {
                "timestamp": ts,
                "lead_id": (f"https://app-ab20.marketo.com/leadDatabase/"
                            f"loadLeadDetail?leadId={lead_id}"),
                "from_phone":from_phone,
                "to_phone":to_phone,
                "message":message,
                "sms_response": "",
                "error": ""
            }

            try:
                response = telnyx_functions.sendSMS(to_phone,from_phone, message)

                callback_objects.append({
                    "leadData": {
                        "id":         lead_id
                    },
                    "activityData": {
                        "from_phone": from_phone,
                        "to_phone_value":  to_phone,
                        "message":  message,
                        "sms_response": response,
                        "success":           True
                    }
                })

                row["sms_response"] = response

            except Exception as per_lead_err:
                # still return an entry so the batch keeps going
                callback_objects.append({
                    "leadData": { "id": lead_id },
                    "activityData": {
                        "from_phone": from_phone,
                        "to_phone_value":  to_phone,
                        "message":  message,
                        "sms_error": str(per_lead_err),
                        "success": False,
                    }
                })
                row["error"] = f"{per_lead_err}\n{traceback.format_exc()}"

            rows_leads.append(row)

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

        cb_response =r.text

        # ---------- batch summary ----------
        batch_row = {
            "timestamp": ts,
            "error": "" if r.ok else f"Callback HTTP {r.status_code}",
            "cb_response": cb_response
        }
        # split the potentially huge *request* JSON
        batch_row |= _split_long_text("request", json.dumps(data, ensure_ascii=False))

        # ---------- write to Sheets once ----------
        try:
            if rows_leads:
                googlesheets_functions.writeDF2Sheet(pd.DataFrame(rows_leads), SMS_SHEET_LEADS, SMS_SPREADSHEET_ID)

            googlesheets_functions.writeDF2Sheet(pd.DataFrame([batch_row]), SMS_SHEET_BATCHES, SMS_SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets logging error:", gs_err)

        return ("", 202) if r.ok else (jsonify({"error": r.text}), 500)

    except Exception as e:
        fail_row = {
            "timestamp": ts,
            "error": f"{e}\n{traceback.format_exc()}",
            "cb_response": cb_response,
        }

        fail_row |= _split_long_text("request", json.dumps(data, ensure_ascii=False))

        try:
            googlesheets_functions.writeDF2Sheet(pd.DataFrame([fail_row]), SMS_SHEET_BATCHES, SMS_SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets error while logging fatal failure:", gs_err)

        return jsonify({"error": "server error"}), 500

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