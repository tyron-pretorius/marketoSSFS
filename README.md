# Marketo Self-Service Flow Steps

A working Flask template for building your own Marketo flow steps — the custom
actions that show up in the flow step dropdown next to "Change Data Value" and
"Send Email", alongside Marketo's own.

Five services are included. Each one is a complete, readable example you can
copy and change, rather than a framework to learn.

This is the companion repo to
[theworkflowpro.com/marketo-self-service-flow-steps](https://theworkflowpro.com/marketo-self-service-flow-steps).
**[`marketo_ssfs_guide.md`](marketo_ssfs_guide.md) is the authority on how
these services are shaped** — read it before building a new one.

---

## Why a flow step instead of a webhook

| | Webhook | Self-service flow step |
|---|---|---|
| Timeout | 30 seconds, hard | None |
| Leads per call | 1 | Up to 1000 in a batch campaign |
| Campaign types | Trigger only | Trigger, batch and executable |
| Flow waits for it | No | Yes |
| Writes back | One field at a time | Lead fields *and* a logged activity |
| Setup effort | Minutes | An afternoon |

The tradeoff is all in that last row. If a webhook does the job, use a webhook.

---

## The five services

| Service | What it does | Needs |
|---|---|---|
| `calcFormula` | Evaluates an Excel-style formula against lead values | — |
| `gptCompletion` | Sends a prompt to OpenAI and writes the answer back | `OPENAI_API_KEY` |
| `sendSMS` | Sends a text through Telnyx | `TELNYX_API_KEY` |
| `e164Formatting` | Turns a messy phone number into E.164 with OpenAI, then validates it with Neutrino | `OPENAI_API_KEY`, `NEUTRINO_USER_ID`, `NEUTRINO_API_KEY` |
| `captureConsent` | Reads the consent wording off the form the person filled out and records it on their program membership | `MARKETO_BASE_URL`, `MARKETO_CLIENT_ID`, `MARKETO_CLIENT_SECRET` |

The first three are deliberately simple. The last two are production-shaped and
show things the simple ones don't:

- **`e164Formatting`** — ten worker threads over a 1000-lead batch, retrying a
  malformed answer but *not* an "I don't know", and one bulk call to Neutrino
  at the end instead of one per lead.
- **`captureConsent`** — `triggerContext`, which hands you the form the person
  filled out so the marketer configures nothing, and calling Marketo's own REST
  API back for things the invocation payload doesn't carry.

---

## Repo layout

```
main.py                      Router. Registers each service's blueprint.
marketo_ssfs_guide.md        How all of this works. Start here.

services/<name>/
  routes.py                  Every endpoint for one service, plus its logic.
  swagger.json               How Marketo learns to talk to the service.
  *_functions.py             API clients used by that service only.

googlesheets_functions.py    Shared: logging inputs and outputs to a Sheet.
marketo_functions.py         Shared: Marketo REST API client.
rate_limiters.py             Shared: sliding window, so a thread pool
                             doesn't burst past a provider's ceiling.
safe_errors.py               Shared: failure text that doesn't swamp the log.
wfp_logo_pink.png            Served by /serviceIcon and /brandIcon.
```

A client used by one service lives in that service's folder. A client more than
one service could use lives at the root. That's the whole rule.

> **Never put `#` comments in `swagger.json`.** JSON has no comments, and
> Marketo answers with a vague error that is very hard to trace back.

---

## Every service exposes the same six endpoints

| Endpoint | Who calls it | What it's for |
|---|---|---|
| `/install` | You, once | Serves the OpenAPI spec Marketo installs from |
| `/getServiceDefinition` | Marketo | The flow step's name, inputs and outputs |
| `/submitAsyncAction` | Marketo | The actual invocation — up to 1000 leads |
| `/status` | Marketo | Health check |
| `/serviceIcon` | Marketo | 32×32 PNG on the flow step |
| `/brandIcon` | Marketo | 32×32 PNG in the admin listing |

`/submitAsyncAction` answers `202` immediately and does the work on a
background thread, then POSTs the results back to `callbackUrl`. Marketo's flow
waits for that callback, not for the HTTP response.

Because the response has already gone by then, anything that fails afterwards
can only be logged — it cannot be returned to Marketo. That's what the Google
Sheets logging is for.

---

## Setup

### 1. Install

```bash
poetry install
```

### 2. Environment

Create a `.env` (or use the Secrets pane on Replit). You only need the keys for
the services you actually intend to run — the app starts fine without the rest.

```bash
# Basic auth Marketo uses to call you. Required by every service.
MARKETO_USER=...
MARKETO_PASSWORD=...

# gptCompletion, e164Formatting
OPENAI_API_KEY=...

# sendSMS
TELNYX_API_KEY=...

# e164Formatting
NEUTRINO_USER_ID=...
NEUTRINO_API_KEY=...

# captureConsent — from Admin -> Web Services in your subscription
MARKETO_BASE_URL=https://123-ABC-456.mktorest.com
MARKETO_CLIENT_ID=...
MARKETO_CLIENT_SECRET=...
```

### 3. Google Sheets logging

Put your service account JSON in the repo root and point
`SERVICE_ACCOUNT_FILE` in `googlesheets_functions.py` at it. Then, for each
service you're running, replace the placeholder spreadsheet id at the top of
its `routes.py` and create the two tabs it writes to:

| Service | Tabs |
|---|---|
| `calcFormula` | `calcFormulaLeads`, `calcFormulaBatches` |
| `gptCompletion` | `gptCompletionLeads`, `gptCompletionBatches` |
| `sendSMS` | `sendSMSLeads`, `sendSMSBatches` |
| `e164Formatting` | `e164FormattingLeads`, `e164FormattingBatches` |
| `captureConsent` | `Consent Leads`, `Consent Batches` |

`e164Formatting` also has a `MARKETO_APP` constant to point at your own
instance, so the lead links in the log go somewhere useful.

**A missing tab fails silently.** The Sheets writes are wrapped in try/except so
a logging failure never costs you a lead — which also means you won't be told
the tab isn't there. If your logs are empty, check the tab names first.

Don't want the logging? Strip it out. Nothing depends on it.

### 4. Run

```bash
poetry run python main.py
```

Or on Replit, the Run button — `.replit` already has the gunicorn command.

---

## Installing a service in Marketo

1. Deploy somewhere Marketo can reach. Put that address in the `servers` URL in
   that service's `swagger.json`.
2. **Admin → Service Providers → New Service**, URL
   `https://your-host/<serviceName>/install`.
3. Enter the basic auth credentials you set as `MARKETO_USER` /
   `MARKETO_PASSWORD`.
4. Resolve any name collision — see the quirks section of the guide.
5. Map incoming and outgoing fields as required, then save. The step appears
   in the smart campaign flow dropdown.

Full walkthrough, including the parts that are easy to get wrong, in
[`marketo_ssfs_guide.md`](marketo_ssfs_guide.md).

---

## Adding your own service

```
services/yourService/
  __init__.py
  routes.py
  swagger.json
```

Copy the service closest to what you need — `calcFormula` if it's pure logic,
`sendSMS` if it calls one external API, `e164Formatting` if it calls several
and needs to be quick about it. Then register the blueprint in `main.py`.

Two things that will bite you:

- **Build API clients lazily.** `main.py` imports every blueprint, so anything
  that runs at import time runs for all of them. A client constructed at module
  level means a missing key for *your* service stops *everybody's* service from
  starting. See `services/gptCompletion/openai_functions.py` for the pattern.
- **The primary attribute always renders.** Marketo shows it on the flow step
  whether you want it or not, and it has to be one of your `flowAttributes`. A
  service that needs no input still has to declare one — `captureConsent` uses
  a `note` field it never reads.

---

## License

MIT. See [LICENSE](LICENSE).
