# services/hearAbout/routes.py
from flask import Blueprint, request, jsonify, Response, send_file, send_from_directory
from datetime import datetime
import os, requests, pandas as pd, json, traceback, pytz
import googlesheets_functions
from . import openai_functions

bp = Blueprint("gptCompletion", __name__, url_prefix="/gptCompletion")

# ---------- CONFIG ----------
base = "gptCompletion"
GC_SHEET_LEADS   = f"{base}Leads"
GC_SHEET_BATCHES = f"{base}Batches"
GC_SPREADSHEET_ID = "1DqUYub7vrnhEw2N5LOWRAWAZwYAzE3P-E-RPXqRhkws"
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
@bp.route("/submitAsyncAction", methods=["POST"])
def submit_async_action():
    timestamp = datetime.now(pacific).strftime("%Y-%m-%d %H:%M:%S")
    rows_leads: list[dict] = []        # (optional) logging
    cb_response = ""
    data = request.get_json(force=True)
    
    try:
        
        callback_objects: list[dict] = []
        for obj in data.get("objectData", []):
            octx          = obj.get("objectContext", {})
            lead_id      = octx.get("id")

            # ── flow-step inputs (all are strings except the two numerics) ──
            ctx          = obj.get("flowStepContext", {})
            sys_prompt   = ctx.get("system", "")          # mapped in UI
            usr_prompt   = ctx.get("user",   "")
            model_name   = ctx.get("model",  "gpt-4o-mini")
            temp         = float(ctx.get("temperature", 0.5))
            max_tokens   = int(float(ctx.get("output-tokens", 256)))
            resp_field   = ctx.get("field")      # **API-name** only!
            answer = ""
            error = ""

            try:
                answer = openai_functions.getCompletion(sys_prompt, usr_prompt, model_name, temp, max_tokens)

                single_cb = {
                    "leadData": { "id": lead_id , resp_field: answer},
                    "activityData": {
                        "system": sys_prompt,
                        "user":    usr_prompt,
                        "model": model_name,
                        "temperature": temp,
                        "output-tokens": max_tokens,
                        "field": resp_field,
                        "gpt-response": answer,
                        "success":   True
                    }
                }

            except Exception as e:
                # still send a callback entry so the step doesn’t stall
                error = f"{e}\n{traceback.format_exc()}",
                single_cb = {
                    "leadData": { "id": lead_id },
                    "activityData": {
                        "gpt-error": error,
                        "success":   False
                    }
                }

            callback_objects.append(single_cb)
            
            # ---- (optional) log one row per lead -----------
            rows_leads.append({
                "timestamp":    timestamp,
                "lead_id":      f"https://app-ab20.marketo.com/leadDatabase/loadLeadDetail?leadId={lead_id}",
                "system":       sys_prompt,
                "user":          usr_prompt,
                "model":        model_name,
                "temperature":  temp,
                "max_tokens":   max_tokens,
                "response_field": resp_field,
                "gpt_response": answer,
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
            googlesheets_functions.writeDF2Sheet(df_leads,   GC_SHEET_LEADS,   GC_SPREADSHEET_ID)
            googlesheets_functions.writeDF2Sheet(df_batches, GC_SHEET_BATCHES, GC_SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets logging error:", gs_err)

        return ("", 202) if r.ok else (jsonify({"error": r.text}), 500)

    except Exception as e:
        fail_row = {
            "timestamp": timestamp,
            "error": f"{e}\n{traceback.format_exc()}",
            "cb_response": cb_response
        }

        fail_row |= _split_long_text("request", json.dumps(data, ensure_ascii=False))
        
        try:
            googlesheets_functions.writeDF2Sheet(pd.DataFrame([fail_row]), GC_SHEET_BATCHES, GC_SPREADSHEET_ID)
        except Exception as gs_err:
            print("Sheets error while logging fatal failure:", gs_err)
    
        return jsonify({"error": "server error"}), 500

@bp.route("/getServiceDefinition")
def get_service_definition():
    return jsonify({
        "apiName": "gpt-completion",
        "i18n": {
            "en_US": {
                "name": "GPT Completion",
                "description": "Makes a request to the OpenAI completion endpoint",
                "triggerName": "GPT Completion is Made",
                "filterName":  "GPT Completion was Made"
            }
        },
        "primaryAttribute": "user",

        "invocationPayloadDef": {
            "flowAttributes": [
                {
                    "apiName":  "user",
                    "dataType": "text",
                    "description": "Open‑text field containing the user message",
                    "i18n": {
                        "en_US": {
                            "name": "User"
                        }
                    }
                },
                {
                    "apiName":  "system",
                    "dataType": "text",
                    "description": "Open‑text field containing the system message",
                    "i18n": {
                        "en_US": {
                            "name": "System"
                        }
                    }
                },
                {
                    "apiName":  "model",
                    "dataType": "string",
                    "description": "OpenAI model",
                    "i18n": {
                        "en_US": {
                            "name": "Model"
                        }
                    }
                },
                {
                    "apiName":  "field",
                    "dataType": "string",
                    "description": "Field to store the GPT response",
                    "i18n": {
                        "en_US": {
                            "name": "Response Field"
                        }
                    }
                },
                {
                    "apiName":  "output-tokens",
                    "dataType": "integer",
                    "description": "Number of output tokens to restrict the response",
                    "i18n": {
                        "en_US": {
                            "name": "Output Tokens"
                        }
                    }
                },
                {
                    "apiName":  "temperature",
                    "dataType": "float",
                    "description": "Temperature of the completion",
                    "i18n": {
                        "en_US": {
                            "name": "Temperature"
                        }
                    }
                }
            ],
            "userDrivenMapping": False, #causes the outgoing mapping to appear when installing in the UI
            "fields": []
        },

        "callbackPayloadDef": {
                "attributes": [                    
                    # {
                    #     "apiName":  "user",
                    #     "dataType": "text",
                    #     "i18n": {
                    #         "en_US": {
                    #             "name":        "User message",
                    #             "description": "User message"
                    #         }
                    #     }
                    # },
                    {
                        "apiName":  "system",
                        "dataType": "text",
                        "i18n": {
                            "en_US": {
                                "name":        "System message",
                                "description": "System message"
                            }
                        }
                    },
                    {
                        "apiName":  "model",
                        "dataType": "string",
                        "i18n": {
                            "en_US": {
                                "name":        "Model",
                                "description": "Model"
                            }
                        }
                    },
                    {
                          "apiName": "field",
                          "dataType": "string",
                          "i18n": { "en_US": { "name": "Response Field",
                                               "description": "Field to store the GPT response" } }
                        },
                    {
                          "apiName": "temperature",
                          "dataType": "float",
                          "i18n": { "en_US": { "name": "Temperature",
                                               "description": "Temperature" } }
                    },
                    {     "apiName": "output-tokens",        
                          "dataType": "integer",
                          "i18n": { "en_US": { "name": "Output Tokens",                                                                                          
                                              "description": "Output token constraint" } }
                    }
                    ,
                    {     "apiName": "gpt-response",        
                          "dataType": "text",
                          "i18n": { "en_US": { "name": "GPT Response",                                                                                          
                                              "description": "GPT Response" } }
                    },
                    {     "apiName": "gpt-error",        
                          "dataType": "text",
                          "i18n": { "en_US": { "name": "GPT Error",                                                                                          
                                              "description": "GPT Error" } }
                    },

                ],
                "fields": [],
                "userDrivenMapping": True #causes the incoming mapping to appear when installing in the UI, need Admin to whitelist fields whose APIName can then be given in the flow step config so that they can be updated via API
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