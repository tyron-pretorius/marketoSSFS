"""Readable failure text that doesn't swamp the log or the callback.

A failing API answers with an error page, not JSON. Both `str(exc)` and
`traceback.format_exc()` repeat that page in full, and recording it once per
lead turns a thousand-lead batch into megabytes of markup in the Sheets log
and in the callback to Marketo - which then rejects the callback for being
too big, so one upstream outage costs you the whole batch twice over.
"""
import traceback

MAX_ERROR_CHARS = 500


def short_error(exc, limit=MAX_ERROR_CHARS, message=None):
    """A capped message plus the stack, meant to be stored and read.

    Pass `message` when the caller already worked out something clearer than
    the exception's own text, which during an outage is a page of markup.
    """
    stack = "".join(traceback.format_tb(exc.__traceback__))
    return "%s\n%s" % ((message or str(exc))[:limit], stack)
