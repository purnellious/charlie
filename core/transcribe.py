"""Voice message transcription via Groq Whisper API."""

import logging
import os

from groq import AsyncGroq

log = logging.getLogger(__name__)

MODEL = "whisper-large-v3-turbo"


async def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe audio bytes using Groq Whisper. Returns transcribed text."""
    client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))
    transcription = await client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=MODEL,
        response_format="text",
    )
    return transcription.strip()
