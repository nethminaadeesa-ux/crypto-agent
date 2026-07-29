"""
The brain socket.

The bot needs something to think with. This file decides what.
It checks which key you have set, in this order:

  GEMINI_API_KEY   -> Google Gemini, free tier          (recommended - costs nothing)
  GROQ_API_KEY     -> Groq, free tier                   (backup, also free)
  ANTHROPIC_API_KEY-> Claude, paid                      (best quality, costs money)

You only ever set one. Nothing else in the project changes.
"""

import json, os, time
import requests

GEMINI = os.environ.get("GEMINI_API_KEY")
GROQ = os.environ.get("GROQ_API_KEY")
CLAUDE = os.environ.get("ANTHROPIC_API_KEY")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
CLAUDE_MODEL = os.environ.get("MODEL", "claude-sonnet-5")


def provider():
    if GEMINI:
        return "gemini"
    if GROQ:
        return "groq"
    if CLAUDE:
        return "claude"
    raise RuntimeError(
        "No key found. Set one of GEMINI_API_KEY, GROQ_API_KEY or ANTHROPIC_API_KEY."
    )


def _extract_json(text):
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def _gemini(prompt, tokens, want_json):
    cfg = {"temperature": 0.3, "maxOutputTokens": tokens}
    if want_json:
        cfg["responseMimeType"] = "application/json"
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        params={"key": GEMINI},
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
        timeout=120)
    if r.status_code == 429:
        raise RuntimeError("rate limited - free tier allows a few requests a minute")
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _groq(prompt, tokens, want_json):
    body = {"model": GROQ_MODEL, "max_tokens": tokens, "temperature": 0.3,
            "messages": [{"role": "user", "content": prompt}]}
    if want_json:
        body["response_format"] = {"type": "json_object"}
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      headers={"Authorization": f"Bearer {GROQ}"}, json=body, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _claude(prompt, tokens, want_json):
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": CLAUDE, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": CLAUDE_MODEL, "max_tokens": tokens,
                            "messages": [{"role": "user", "content": prompt}]},
                      timeout=180)
    r.raise_for_status()
    return "\n".join(b["text"] for b in r.json()["content"] if b["type"] == "text")


def ask(prompt, want_json=True, tokens=1500):
    """Ask whichever brain is available. Retries once if the free tier throttles."""
    fn = {"gemini": _gemini, "groq": _groq, "claude": _claude}[provider()]
    last = None
    for attempt in range(3):
        try:
            text = fn(prompt, tokens, want_json)
            return _extract_json(text) if want_json else text.strip()
        except Exception as e:
            last = e
            wait = 20 * (attempt + 1)
            print(f"    brain busy ({e}) - waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"brain unavailable after 3 tries: {last}")
