from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def getCompletion(system_msg: str, user_msg: str, model: str, temperature: float, max_tokens: int) -> str:

    resp = client.chat.completions.create( model=model,temperature=temperature,max_tokens=max_tokens,
        messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": user_msg}
                ])
    return resp.choices[0].message.content.strip()