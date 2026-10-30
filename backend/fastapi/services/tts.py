"""ElevenLabs Text-to-Speech helper for Operation Shadow voice narration.

Streams ULTRON's mission rationale through the operator's speakers using the
eleven_multilingual_v2 model. Runs as a non-blocking background task so
physical hardware commands are dispatched immediately and never delayed by TTS.
"""

from __future__ import annotations

import os

from core.logging import get_logger

logger = get_logger("services.tts")

# Voice presets — George (cinematic, authoritative)
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
DEFAULT_MODEL_ID = "eleven_multilingual_v2"


def _get_client():
    """Lazily initialise the ElevenLabs client (requires ELEVENLABS_API_KEY)."""
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        logger.warning(
            "ELEVENLABS_API_KEY not set — TTS narration is disabled. "
            "Set the key in .env to enable voice output."
        )
        return None

    try:
        from elevenlabs.client import ElevenLabs
        return ElevenLabs(api_key=api_key)
    except Exception as exc:
        logger.error(f"Failed to initialise ElevenLabs client: {exc}")
        return None


def speak_rationale(
    text: str,
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    model_id: str = DEFAULT_MODEL_ID,
) -> None:
    """Stream TTS audio of the rationale through the local speakers.

    Designed to be called via ``BackgroundTasks.add_task`` so the HTTP
    response is returned immediately and the audio plays asynchronously.
    """
    if not text or not text.strip():
        return

    client = _get_client()
    if client is None:
        return

    try:
        from elevenlabs import stream as elevenlabs_stream

        audio_stream = client.text_to_speech.stream(
            text=text,
            voice_id=voice_id,
            model_id=model_id,
        )
        elevenlabs_stream(audio_stream)
        logger.info("TTS narration completed for rationale.")
    except Exception as exc:
        logger.error(f"TTS playback failed (non-fatal, mission unaffected): {exc}")
