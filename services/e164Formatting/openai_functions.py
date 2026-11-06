##### OpenAI #####
import os
import time
from typing import Optional

from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# gpt-6-luna is a reasoning model, and that changes how it has to be called:
#
#   * `max_completion_tokens`, never the legacy `max_tokens`, which it
#     rejects outright. The budget covers the model's thinking as well as its
#     answer, so a ceiling sized for the answer alone comes back empty - which
#     parses as a failure and looks like a model that cannot read a phone
#     number. Observed usage is around 60 tokens; the rest is head-room, and
#     only what is used is billed.
#
#   * No `temperature`. A reasoning model rejects the parameter rather than
#     ignoring it, so it is not sent at all.
MODEL = "gpt-6-luna"
MAX_COMPLETION_TOKENS = 2000


def completions(user_message: str, system_prompt: str,
                model: str = MODEL, max_retries: int = 3) -> Optional[str]:
    """One JSON completion, or None once the retries are spent."""
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
            result = response.choices[0].message.content
            return result.strip() if result else None

        except Exception as e:
            print(f"OpenAI attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                return None
            time.sleep(2 ** attempt)          # 1s, 2s, 4s
