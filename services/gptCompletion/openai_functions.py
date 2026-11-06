from openai import OpenAI
import os

# Built on first use, not at import.
#
# main.py imports every service's blueprint, so anything that runs at import
# time runs for all of them. Constructing the client here used to mean a
# missing OPENAI_API_KEY stopped the whole app from starting - including the
# services that have nothing to do with OpenAI.
#
# Deferring it to the first call means you only need the keys for the
# services you actually use, and a missing one shows up as a clear error on
# that service's own request instead of a stack trace at boot.
_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set, so the GPT Completion service "
                "cannot run. Add it to .env locally or to the Secrets pane "
                "on Replit."
            )
        _client = OpenAI(api_key=api_key)
    return _client


def getCompletion(system_msg: str, user_msg: str, model: str, temperature: float, max_tokens: int) -> str:

    resp = _get_client().chat.completions.create( model=model,temperature=temperature,max_tokens=max_tokens,
        messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": user_msg}
                ])
    return resp.choices[0].message.content.strip()
