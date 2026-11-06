# Marketo Self-Service Flow Steps — Implementation Guide

Reference notes transcribed from the Workflow Pro walkthrough of this Replit
template. This is the authority for how the services in this repo are shaped —
when in doubt, follow this file rather than inference.

---

## 1. What an SSFS actually is

Marketo sends lead data out to a server you host, the server processes it and
sends the results back, and Marketo updates the lead and logs an activity. The
"server" is just somewhere your Python listens for Marketo. Hosted code like
this is called a **service**.

### Why SSFS over a webhook

- No 30-second timeout.
- Batch campaigns send up to **1000 leads per call**; trigger campaigns send one.
- Works in trigger, batch and executable campaigns.
- The smart campaign flow **waits** for the step to finish before continuing to
  subsequent flow steps (same as executable campaigns).

The only tradeoff: far more setup than a webhook.

---

## 2. Repo structure

| Path | Job |
|---|---|
| `main.py` | Router only. Registers each service's blueprint so an incoming call reaches the right service. |
| `services/<name>/routes.py` | All endpoints for one service, plus the custom logic. |
| `services/<name>/swagger.json` | Lists the endpoints, the data types each expects, and the shape sent back. This is how Marketo learns to talk to the service. |
| `services/<name>/*_functions.py` | API clients used by that one service only — Telnyx for `sendSMS`, Neutrino for `e164Formatting`. |
| `googlesheets_functions.py` | Logging of inputs and outputs to a Google Sheet. |
| `marketo_functions.py` | Marketo's own REST API, for the things the invocation payload doesn't carry (§8). |
| `rate_limiters.py` | A sliding window, shared process-wide, so a thread pool doesn't burst past a provider's ceiling. |
| `safe_errors.py` | Failure text capped to something a log and a callback can carry. |
| service account JSON | Credentials for the Sheets API. |
| logo PNG | Served by `/serviceIcon` and `/brandIcon`. |

A client used by one service lives in that service's folder; one that more than
one service could use lives at the root. That's the whole rule.

`swagger.json` tells Marketo **what endpoints exist**; `routes.py` defines **how
the service responds** when Marketo calls them.

> **Do not put `#` comments in `swagger.json`.** JSON has no comments, and
> Marketo returns a vague, unhelpful error that is very hard to trace.

If Sheets logging is more trouble than it's worth, it can be stripped out — the
writes are wrapped in try/except, so a Sheets failure only prints an error and
the lead is still updated.

---

## 3. What Marketo sends to `/submitAsyncAction`

```json
{
  "token": "xxx",
  "apiCallBackKey": "123abc",
  "campaignId": 42504,
  "callbackUrl": "https://mkto-cfa.adobe.io/customflowaction/submitCustomFlowAction",
  "context": {"admin": {}},
  "objectData": [
    {
      "objectType": "lead",
      "objectContext": {"email": "x@y.com", "id": 7315607, "...": "mapped fields"},
      "flowStepContext": {"...": "values typed into the smart campaign flow step"}
    }
  ]
}
```

- `callbackUrl` + `apiCallBackKey` + `token` — how you send results back, and
  your proof you're allowed to.
- `objectData` — one entry per lead, up to 1000.
- `objectContext` — the lead fields being sent. **`id` is always present.**
- `flowStepContext` — the values the marketer configured in the flow step.

---

## 4. Endpoints

| Endpoint | Job |
|---|---|
| `/serviceIcon` | Returns the PNG shown in the flow step and admin listing. |
| `/brandIcon` | Returns the PNG shown in the admin listing. |
| `/status` | Pinged by Marketo every 24 hours. Just return ok. |
| `/install` | Points Marketo at `swagger.json`. This is the URL you paste into Marketo. |
| `/getPicklist` | **Optional.** Only needed if a flow attribute is a dropdown. |
| `/submitAsyncAction` | The work. Where all custom logic lives. |
| `/getServiceDefinition` | Declares what the service expects and returns. |

`/install`, `/serviceIcon` and `/brandIcon` are **unauthenticated**.
`/status` and `/submitAsyncAction` require basic auth — the username and password
Marketo prompts for at install time, checked against env vars.

### `/getPicklist`

Receives the field name Marketo wants dropdown values for, and returns the
choices for it:

```python
if field_name == "data_type":
    return [...]        # int, bool, string, float
elif field_name == "some_other_field":
    return [...]
```

A flow attribute that needs a dropdown declares:

```python
"hasPicklist": True,
"enforcePicklistSelect": True    # blocks the marketer from typing a free value
```

---

## 5. `/submitAsyncAction` structure

Split across two functions in every service: a thin route handler that answers
Marketo instantly, and a background worker that does everything else.

### The route handler — `submit_async_action()`

1. `data = request.get_json(force=True)` — everything Marketo sent.
2. Stamp a timestamp (it becomes the log row's id).
3. Hand both to a daemon `Thread` running `_process_batch_async`.
4. `return "", 202` immediately.

That's the whole handler. Marketo gets its 202 in milliseconds and stops
waiting on the service.

### The background worker — `_process_batch_async(data, timestamp)`

1. Loop over `data["objectData"]` — one iteration per lead, up to 1000.
2. Pull what you need from `flowStepContext` (marketer config) and
   `objectContext` (lead fields; `id` always).
3. Run the custom logic.
4. Build a **callback object** for that lead and append it to a list.
5. Append a log row for that lead.
6. **After the loop**, make ONE callback request to Marketo containing every
   callback object. **This callback is what actually applies the updates** —
   the 202 told Marketo the job was accepted, nothing more.
7. Write the lead rows and one batch summary row to Sheets.

### Why bother, when SSFS already has no 30-second timeout

The flow step **waits** for the callback before moving to the next step, but
that is Marketo waiting on the callback — not on the HTTP response. Answering
202 up front separates the two, which buys three things:

- The HTTP request isn't held open for the length of the work, so a slow
  batch can't be killed by a proxy, load balancer or worker timeout.
- The web worker is freed immediately, so concurrent invocations don't queue
  behind each other.
- Long work (per-lead API calls, image generation, uploads) becomes safe to do
  at 1000-lead scale.

**The trade-off:** once the 202 is sent, there is no HTTP response left to
fail with. Anything that goes wrong afterwards can only be `print`ed and
written to the batches sheet — so the Sheets log stops being optional
convenience and becomes the only place a background failure is visible.

### The callback object

Two parts:

```python
{
  "leadData": {
      "id": lead_id,              # always - tells Marketo which lead
      "<field>": value            # the fields to update
  },
  "activityData": {
      "<attribute>": value,       # what shows in the activity detail
      "success": True
  }
}
```

**Every key in `activityData` must also be declared in
`callbackPayloadDef.attributes`, or it will not appear in the activity detail.**

### The callback request

```python
requests.post(
    data["callbackUrl"],
    headers={"x-api-key": data["apiCallBackKey"],
             "x-callback-token": data["token"],
             "Content-Type": "application/json"},
    json={"munchkinId": "123", "objectData": callback_objects})
```

`munchkinId` **does not matter** — Marketo doesn't look at it. A dummy value is
correct and intentional.

### Error handling

An inner try/except per lead (so one bad lead doesn't stall the step) and an
outer try/except for anything that goes wrong outside the loop. The outer failure
is logged as a row in the batches sheet.

### Sheets 50,000 character cell limit

A 1000-lead request easily exceeds it, so a long value is split across
`request`, `request_2`, `request_3` … columns. Without this, the row fails to log.

---

## 6. `/getServiceDefinition`

Declares the API name, the trigger and filter names, the primary attribute, and
the two payload definitions.

### `primaryAttribute`

- It is **one of `invocationPayloadDef.flowAttributes`**.
- It **always** appears in the flow step configuration. There is no way to have
  no primary attribute — if the service doesn't need one, declare a dummy flow
  attribute and ignore its value.
- It **always** appears in the activity detail automatically.
- ⚠️ **Declaring it in `callbackPayloadDef.attributes` produces an error.** Don't.
  It shows up regardless.
- ⚠️ Its **token does not resolve** in the activity detail — you see `{{lead.X}}`
  rather than the value. Standard workaround: declare a second attribute holding
  the resolved value (`to_phone` → `to_phone_value`, `formula` → `formula_value`)
  and populate that in `activityData`.

### The `fields` object shape

Confirmed against the Map Outgoing Fields screen — the External Field column
renders `serviceAttribute`:

```python
{
    "serviceAttribute": "hear_about_us",   # the external name; NOT "apiName"
    "dataType": "string",
    "isRequired": True,                    # else the mapping is optional
    "description": "...",                  # top level; validator rejects without it
    "i18n": {"en_US": {"name": "...", "description": "..."}}
}
```

`attributes` entries use `apiName` instead. Getting these two confused produces
`fields #1 missing serviceAttribute` at install.

Without `isRequired`, the install screen counts the mapping as optional
("0 of 0 required mapping completed / 0 of 1 optional"), so an admin can finish
the install without mapping it at all and the service receives nothing.

> **Open question:** the install screen's Description column renders blank even
> with `description` set at the top level *and* inside `i18n.en_US`. Source of
> the displayed description not yet identified.

### `invocationPayloadDef` — what Marketo sends you

Three parts:

- **`flowAttributes`** — the inputs the marketer fills in on the flow step. Each
  one you declare here becomes a field in the smart campaign flow step UI.
- **`fields`** — lead fields you want Marketo to send with every call.
- **`userDrivenMapping`** — changes how `fields` behaves entirely (below).

Global attributes, my tokens, subscription, program member, trigger tokens and
campaign tokens can also be brought in here.

### `triggerContext` — what set the campaign off

A fourth part, and the one that saves the most configuration. Set it and
Marketo sends the triggering activity along with each lead:

```python
"invocationPayloadDef": {
    "flowAttributes": [...],
    "triggerContext": True,
    "userDrivenMapping": False,
    "fields": []
}
```

The marketer then types nothing, and a cloned program needs no edit, because
the service reads the form (or email, or list) out of the trigger rather than
out of a field somebody filled in.

⚠️ **It comes back under a different name, in a different place.** You ask for
`triggerContext` at the top of `invocationPayloadDef`; Marketo returns
`triggeringContext`, nested inside **each `objectData` entry** — not at the top
level of the payload. Confirmed against a real "Fill Out Form" invocation:

```json
"objectData": [{
  "objectContext": {"id": 7315607},
  "triggeringContext": {
    "activityTypeName": "Fill Out Form",
    "primaryAttributeValue": "<program name>.<form name>",
    "primaryAttributeValueId": 7048,
    "attributes": {"Webform ID": "7048", "Form Fields": "<php>", "Referrer URL": "..."}
  }
}]
```

Three things worth knowing about what's in there:

- **`primaryAttributeValue` is qualified** — `<program name>.<form name>`. The
  form name is after the *last* dot, because a program name may contain dots of
  its own.
- **`primaryAttributeValueId` is the form id.** Use it rather than looking the
  form up by name: cloned programs reuse form names, so the name alone isn't
  unique across the instance.
- **`Form Fields` is PHP-serialised**, not JSON —
  `a:28:{s:6:"module";s:11:"leadCapture";…}`, where each number is a *byte*
  count. Parse it by walking those lengths, not by splitting on quotes: a
  submitted value can contain quotes and semicolons of its own. See
  `parse_php_serialized_map` in `services/captureConsent/consent_functions.py`.

**There is no timestamp anywhere in it.** If you need the time the activity
happened, the closest you can get is the moment your flow step ran — seconds
later on a trigger campaign. Say so in the log rather than implying precision
you don't have.

Because the trigger arrives per lead, a batch can in principle contain leads
triggered by different forms. If your service reads something once per batch
(the form's fields, say), check each lead's trigger matches the one you read
and fail the rest rather than recording the wrong thing against them.

### `callbackPayloadDef` — what you send back

- **`attributes`** — everything you want visible in the activity detail. Must
  match the keys you put in `activityData`. Do not include the primary attribute.
- **`fields`** — the lead fields you want Marketo to update. A field must be
  declared here for Marketo to accept it in `leadData`.
- **`userDrivenMapping`** — same two behaviours as below.

---

## 7. `userDrivenMapping` — the part that's easy to get wrong

### `False`

- The fields you declare in `fields` appear in the **Map Outgoing Fields**
  (invocation) or **Map Incoming Fields** (callback) section at install time.
- The admin **activates** each field they want and **maps** it to the
  corresponding Marketo field.
- In your code you reference the **external field name** — the name as declared
  in `getServiceDefinition`:

  ```python
  title = context.get("person_job_title", "")     # external name
  ```

- **If `fields` is empty and `userDrivenMapping` is False, the mapping section is
  skipped entirely** — it just appears blank.

### `True`

- **Whatever is in `fields` is ignored.** No point filling it in.
- The mapping section still appears, but the admin manually adds every field
  they want sent (invocation) or want writable (callback).
- In your code you reference the **Marketo API name** of the field:

  ```python
  phone = context.get("Form_Phone__c", "")        # Marketo API name
  ```

The same logic applies in both directions: on the callback side, `False` means
you write to the external names you declared; `True` means you write to Marketo
API names, and the admin whitelists which fields the step may overwrite.

---

## 8. Calling Marketo's REST API

Most services never need this. Marketo sends you everything in the invocation
payload, and you answer in the callback. But some things simply aren't in the
payload — the fields on a form, a program membership — and then you have to ask
Marketo directly. `marketo_functions.py` is that client, and `captureConsent`
is the service that uses it.

This is a **different API with different credentials** from the flow step
mechanism. From Admin → Web Services:

```bash
MARKETO_BASE_URL=https://123-ABC-456.mktorest.com
MARKETO_CLIENT_ID=...
MARKETO_CLIENT_SECRET=...
```

### Things that catch people out

**The token goes in a header.** `Authorization: Bearer <token>`. Passing it as
a query parameter instead returns `603 Access denied`, which reads like a
permissions problem and is not one — you will go and check the API user's role
before you think to check this.

**Cache the token.** It lasts an hour, and fetching one counts against the same
API limit as everything else, so a token per request halves your allowance.

**HTTP 200 doesn't mean it worked.** Marketo answers failures with 200 and
`"success": false`. Check the body, not the status code:

```python
def _check(body, what):
    if not body.get("success", False):
        raise RuntimeError(f"{what} failed: {json.dumps(body.get('errors', body))}")
    return body
```

**The limits are 100 calls per 20 seconds and 10 concurrent.** Those are the
platform's, identical in every subscription and sandbox — raising a constant in
your code doesn't raise them, it just moves the failure to Marketo, which
starts answering `606`. A service calling Marketo once per batch is nowhere
near. One calling it *per lead* needs a rate limiter before it ever meets a
real batch.

### Working out which program called you

The payload carries `campaignId` and nothing else about where it came from. The
smart campaign record has a `folder`, which is either the program itself (type
`Program`) or a campaign folder nested inside one — so walk upwards until a
Program turns up. `get_program_for_campaign` does this, with a bounded loop so
a malformed folder tree can't spin forever.

Take the program's link from its own record rather than building one. The URL
fragment differs by program type (`#ME` for a marketing event, and so on), so a
hand-built link is wrong for some programs.

### Writing program member fields

`POST /rest/v1/programs/{id}/members.json`, 300 records per call.

⚠️ **It updates existing members only — it will not enroll anyone.** A lead who
isn't a member of the program comes back rejected, and that is by far the most
common reason this kind of service reports a failure. It's a campaign design
problem, not a bug: the flow step has to run inside the program whose
membership it's writing to.

Match the response back to your input by `leadId` first, then `seq`, then input
order — and treat `updated`, `created` and `added` as success, anything else as
a per-lead failure with Marketo's own reason attached.

⚠️ **Program member fields have no text or textarea type.** Every string field
is capped at 255 characters. Longer content has to be split across two fields
and re-joined, which is why `captureConsent` writes `…ConsentText` and
`…ConsentText2`. Decide what happens when it won't fit in both — a consent
record cut short is worse than no record at all.

---

## 9. Adding a new service to this repo

1. Create `services/<name>/`.
2. Copy the closest existing `routes.py`, its swagger, and its functions module.
3. Update `base` at the top of `routes.py` (drives the URL prefix and sheet names).
4. Update the imports to the new functions module.
5. Write `getServiceDefinition` **first** — the flow attributes and callback
   attributes you declare there are the names you then use in
   `submitAsyncAction`.
6. Update `_process_batch_async` to read those names and build the callback
   object. Leave `submitAsyncAction` alone — it only starts the thread.
7. Add `/getPicklist` if any flow attribute is a dropdown.
8. In `swagger.json`: title, version, description, provider domain, support
   contact, the `servers` URL (base + `/<name>`), the tags, the `apiName` and the
   `primaryAttribute` examples.
9. Register the blueprint in `main.py`.
10. Deploy, then install in Marketo Admin → Service Providers with
    `https://<host>/<name>/install`.
11. Create the log sheet tabs.

⚠️ **Build API clients on first use, not at import.** `main.py` imports every
blueprint, so anything at module level runs for all of them — and a client
constructed there means a key missing for *your* service stops *everybody's*
service from starting:

```python
_client = None

def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("SOME_API_KEY")
        if not api_key:
            raise RuntimeError("SOME_API_KEY is not set, so ... cannot run.")
        _client = SomeClient(api_key=api_key)
    return _client
```

The app then boots with no keys at all, you only hold credentials for the
services you actually run, and a missing key arrives as that service's own
error rather than a stack trace at startup.

---

## 10. Install and test in Marketo

1. Deploy, then copy the primary domain.
2. Admin → Service Providers → New Service, URL =
   `https://<host>/<serviceName>/install`.
3. Enter the basic auth username and password from your secrets.
4. Resolve any name collision (see quirks).
5. Map incoming/outgoing fields as required.
6. Save. The step is now available in smart campaign flows.

---

## 11. Known quirks and bugs

| Quirk | Consequence |
|---|---|
| **No token picker** on SSFS flow attributes | No autosuggest and no hint text. Workaround: add a Change Data Value step, build the token there, copy it across. |
| **Primary attribute tokens don't resolve** in the activity detail | Declare a separate `<name>_value` attribute and populate it yourself. |
| **Deleted names persist** | API names, trigger names and filter names stay reserved after you delete a service. You must increment or invent a new name on reinstall. |
| **No refresh** | Any change to `getServiceDefinition` or the endpoints requires **uninstall and reinstall** in Marketo. Redeploying alone does nothing. |
| **Primary attribute can't be empty** | Declare a dummy flow attribute if you don't need one. |
| **Activity detail order is fixed** | Native Marketo fields (step ID, source) are interspersed with yours and can't be reordered. |
| **Dropdown that isn't a dropdown** | A flow attribute without `hasPicklist` still renders a dropdown arrow, but is a free text field. |
| `#` comments in `swagger.json` | Vague Marketo error on install. |
| **`triggerContext` comes back as `triggeringContext`** | And nested inside each `objectData` entry, not at the top level. Looking where you asked finds nothing. |
| **No timestamp in the trigger** | The closest you can record is when your flow step ran, which on a trigger campaign is seconds after the activity. |
| **`Form Fields` is PHP-serialised** | Not JSON. Lengths are byte counts, and values can contain quotes and semicolons, so splitting on punctuation corrupts them. |
| **Program member writes don't enroll** | A lead who isn't already a member comes back rejected. The flow step must run inside the program it writes to. |
| **Program member fields cap at 255** | There is no text or textarea type, so longer content needs two fields and a join. |
| **Marketo answers failures with HTTP 200** | Check `"success"` in the body; the status code tells you nothing. |
| **REST token as a query parameter** | Returns `603 Access denied`, which looks like a permissions problem. Use the `Authorization: Bearer` header. |

---

## 12. Hosting notes (Replit)

- Deploying needs the Replit Core plan ($20/month). Usage on top is small —
  two services for two weeks cost under $1.
- Secrets are set in the Replit UI and read with `os.getenv(...)`.
- After creating a deployment, copy the primary domain into each service's
  `swagger.json` `servers` URL.
- `Run` first to install packages, then Deploy.
- Deleting a service folder means also removing its import and
  `register_blueprint` line from `main.py`, or the deploy fails.
