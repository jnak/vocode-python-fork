import asyncio
from typing import Callable, Optional, Awaitable
from vocode.streaming.models.worker import AsyncWorker

from vocode.streaming.utils import convert_wav
from vocode.streaming.models.transcriber import EndpointingConfig, TranscriberConfig


class Transcription:
    def __init__(
        self,
        message: str,
        confidence: float,
        is_final: bool,
        is_interrupt: bool = False,
    ):
        self.message = message
        self.confidence = confidence
        self.is_final = is_final
        self.is_interrupt = is_interrupt

    def __str__(self):
        return f"Transcription({self.message}, {self.confidence}, {self.is_final})"


# class BaseTranscriber(AsyncWorker):
#     def __init__(
#         self,
#         transcriber_config: TranscriberConfig,
#         # TODO(julien) Should we make this an audio queue?
#         # In that case, we would need 2 worker tasks...
#         audio_queue: asyncio.Queue[bytes],
#         # TODO(julien) We probably want to do this
#         transcription_queue: asyncio.Queue[Transcription],
#     ):
#         super().__init__(audio_queue, transcription_queue)
#         self.transcriber_config = transcriber_config
#         self.audio_queue = audio_queue
#         self.transcription_queue = transcription_queue

#     # TODO(julien) This is probably un-necessary
#     def get_transcriber_config(self) -> TranscriberConfig:
#         return self.transcriber_config


class BaseTranscriber:
    WORKER = AsyncWorker()

    def __init__(
        self,
        transcriber_config: TranscriberConfig,
    ):
        super().__init__(audio_queue, transcription_queue)
        self.transcriber_config = transcriber_config

    # TODO(julien) This is probably un-necessary
    def get_transcriber_config(self) -> TranscriberConfig:
        return self.transcriber_config
    
    def worker(...,transcriber=...):
        self.WORKER(
            ...
            process=self.send_audio,
        )
