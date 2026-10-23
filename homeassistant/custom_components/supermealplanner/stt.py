"""Speech to text with the SuperMealPlanner server's own transcriber (the one voice notes use)."""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterable

from homeassistant.components import stt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ChefConfigEntry
from .client import ChefError
from .conversation import device_info

# The transcriber takes any language; these are the ones Assist offers it for.
LANGUAGES = ["es", "es-ES", "en", "en-US", "en-GB", "ca", "eu", "gl", "pt", "fr", "de", "it"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ChefConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add this entry's speech to text."""
    async_add_entities([ChefSpeech(entry)])


class ChefSpeech(stt.SpeechToTextEntity):
    """Sends what the microphone heard to the server and returns the words."""

    _attr_has_entity_name = True
    _attr_name = "Speech to text"

    def __init__(self, entry: ChefConfigEntry) -> None:
        """Set up speech to text for one paired person."""
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}-stt"
        self._attr_device_info = device_info(entry)

    @property
    def supported_languages(self) -> list[str]:
        """Languages offered."""
        return LANGUAGES

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        """WAV only: the stream is raw PCM, wrapped here."""
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """PCM only."""
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """16-bit samples."""
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """16 kHz, what Assist records."""
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Mono."""
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        """Collect the whole utterance, wrap it as WAV and transcribe it on the server."""
        pcm = bytearray()
        async for chunk in stream:
            pcm.extend(chunk)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(metadata.channel.value)
            out.setsampwidth(metadata.bit_rate.value // 8)
            out.setframerate(metadata.sample_rate.value)
            out.writeframes(bytes(pcm))
        try:
            text = await self.entry.runtime_data.transcribe(buf.getvalue())
        except ChefError:
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        if not text:
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        return stt.SpeechResult(text, stt.SpeechResultState.SUCCESS)
