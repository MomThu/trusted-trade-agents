"""Voice for the BUYER agent (ElevenLabs): speech-to-text in, text-to-speech out.

This belongs to the buyer's side. The chef speaks to their own agent; the agent still
talks to Valio only in JSON over MCP. Valio's interface does not change.

  speech_to_text(audio)  -> the buyer's words (any language; Finnish works)
  text_to_speech(text)   -> MP3 of the agent's answer, shortened for listening
"""
from __future__ import annotations

import os
import re

import httpx

API = "https://api.elevenlabs.io/v1"


class VoiceError(RuntimeError):
    pass


def _key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY", "")
    if not key:
        raise VoiceError("ELEVENLABS_API_KEY is not set in .env")
    return key


def enabled() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY"))


async def speech_to_text(audio: bytes, filename: str = "speech.webm", content_type: str = "audio/webm") -> dict:
    """Transcribe the buyer's recording. Returns {"text", "language"}."""
    model = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{API}/speech-to-text", headers={"xi-api-key": _key()},
                              data={"model_id": model}, files={"file": (filename, audio, content_type)})
    if r.status_code != 200:
        raise VoiceError(f"ElevenLabs speech-to-text {r.status_code}: {r.text[:300]}")
    data = r.json()
    return {"text": (data.get("text") or "").strip(), "language": data.get("language_code")}


async def realtime_token() -> str:
    """Single-use token (15 min) so the BROWSER can stream to Scribe realtime without our API key."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{API}/single-use-token/realtime_scribe", headers={"xi-api-key": _key()})
    if r.status_code != 200:
        raise VoiceError(f"ElevenLabs realtime token {r.status_code}: {r.text[:300]}")
    return r.json()["token"]


def spoken_version(text: str, max_chars: int = 600) -> str:
    """Make a written agent report pleasant to hear: no markdown, no long codes, short."""
    t = re.sub(r"`([^`]*)`", r"\1", text)
    t = re.sub(r"\*\*|__|#+ |^- ", "", t, flags=re.M)
    t = re.sub(r"\s*\(\s*ord_\w+\s*\)", "", t)               # "(ord_1114_c55012)": for reading, not listening
    t = re.sub(r"\b([Yy]our\s+|[Tt]he\s+)?([Oo])rder\s+ord_\w+",
               lambda m: (m.group(1) or ("The " if m.group(2) == "O" else "the ")) + "order", t)
    t = re.sub(r"\bord_\w+", "the order", t)
    t = re.sub(r"\s*\((?:product code |code |tuotekoodi |produktkod )?\d{6}\)", "", t)  # (464306), fi, sv
    t = re.sub(r"\bEUR\s*([\d,.]+)", r"\1 euros", t)
    t = re.sub(r"€\s*([\d,.]+)", r"\1 euros", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([,.;:])", r"\1", t)
    if len(t) > max_chars:
        cut = t[:max_chars]
        t = cut[: cut.rfind(".") + 1] if "." in cut else cut
    return t


async def text_to_speech(text: str) -> bytes:
    """MP3 audio of the agent's answer."""
    voice = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    model = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{API}/text-to-speech/{voice}", params={"output_format": "mp3_44100_128"},
                              headers={"xi-api-key": _key(), "Accept": "audio/mpeg"},
                              json={"text": spoken_version(text), "model_id": model})
    if r.status_code != 200:
        raise VoiceError(f"ElevenLabs text-to-speech {r.status_code}: {r.text[:300]}")
    return r.content
