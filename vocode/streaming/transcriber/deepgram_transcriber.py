import asyncio
import json
import logging
import websockets
from collections import deque
from websockets.client import WebSocketClientProtocol
import audioop
from urllib.parse import urlencode
from vocode import getenv

from vocode.streaming.transcriber.base_transcriber import (
    BaseTranscriber,
    Transcription,
)
from vocode.streaming.models.transcriber import (
    DeepgramTranscriberConfig,
    EndpointingConfig,
    EndpointingType,
)
from vocode.streaming.models.audio_encoding import AudioEncoding


PUNCTUATION_TERMINATORS = [".", "!", "?"]
NUM_RESTARTS = 5


class DeepgramTranscriber(BaseTranscriber):
    def __init__(
        self,
        transcriber_config: DeepgramTranscriberConfig,
        logger: logging.Logger = None,
        api_key: str = None,
    ):
        super().__init__(transcriber_config)
        self.api_key = api_key or getenv("DEEPGRAM_API_KEY")
        if not self.api_key:
            raise Exception(
                "Please set DEEPGRAM_API_KEY environment variable or pass it as a parameter"
            )
        self.transcriber_config = transcriber_config
        self._ended = False
        self.is_ready = False
        self.logger = logger or logging.getLogger(__name__)
        self.audio_queue = asyncio.Queue()
        self.msg_deque = deque()

    async def run(self):
        restarts = 0
        while not self._ended and restarts < NUM_RESTARTS:
            await self.process()
            restarts += 1
            self.logger.debug(
                "Deepgram connection died, restarting, num_restarts: %s", restarts
            )

    async def send_audio(self, chunk):
        if (
            self.transcriber_config.downsampling
            and self.transcriber_config.audio_encoding == AudioEncoding.LINEAR16
        ):
            self.logger.error(
                "Deepgram: potentially expensive audio conversion in send_audio"
            )
            chunk, _ = audioop.ratecv(
                chunk,
                2,
                1,
                self.transcriber_config.sampling_rate
                * self.transcriber_config.downsampling,
                self.transcriber_config.sampling_rate,
                None,
            )
        await self.audio_queue.put(chunk)

    def terminate(self):
        terminate_msg = json.dumps({"type": "CloseStream"})
        self.audio_queue.put_nowait(terminate_msg)
        self._ended = True

    def get_deepgram_url(self):
        if self.transcriber_config.audio_encoding == AudioEncoding.LINEAR16:
            encoding = "linear16"
        elif self.transcriber_config.audio_encoding == AudioEncoding.MULAW:
            encoding = "mulaw"
        url_params = {
            "encoding": encoding,
            "sample_rate": self.transcriber_config.sampling_rate,
            "channels": 1,
            "interim_results": "true",
        }
        extra_params = {}
        if self.transcriber_config.language:
            extra_params["language"] = self.transcriber_config.language
        if self.transcriber_config.model:
            extra_params["model"] = self.transcriber_config.model
        if self.transcriber_config.tier:
            extra_params["tier"] = self.transcriber_config.tier
        if self.transcriber_config.version:
            extra_params["version"] = self.transcriber_config.version
        if self.transcriber_config.keywords:
            extra_params["keywords"] = self.transcriber_config.keywords
        if (
            self.transcriber_config.endpointing_config
            and self.transcriber_config.endpointing_config.type
            == EndpointingType.PUNCTUATION_BASED
        ):
            extra_params["punctuate"] = "true"
        url_params.update(extra_params)
        return f"wss://api.deepgram.com/v1/listen?{urlencode(url_params)}"

    def is_speech_final(
        self,
        current_buffer: str,
        current_response: dict,
        last_response: dict,
        time_silent: float,
    ):
        if (
            last_response
            and top_choice(last_response)["transcript"]
            and confidence(last_response) > 0
        ):
            return False

        transcript = top_choice(current_response)["transcript"]

        # if it is not time based, then return true if speech is final and there is a transcript
        if not self.transcriber_config.endpointing_config:
            return transcript and current_response["speech_final"]
        elif (
            self.transcriber_config.endpointing_config.type
            == EndpointingType.TIME_BASED
        ):
            # if it is time based, then return true if there is no transcript
            # and there is some speech to send
            # and the time_silent is greater than the cutoff
            return (
                not transcript
                and current_buffer
                and (time_silent + current_response["duration"])
                > self.transcriber_config.endpointing_config.time_cutoff_seconds
            )
        elif (
            self.transcriber_config.endpointing_config.type
            == EndpointingType.PUNCTUATION_BASED
        ):
            return (
                transcript
                and current_response["speech_final"]
                and transcript.strip()[-1] in PUNCTUATION_TERMINATORS
            ) or (
                not transcript
                and current_buffer
                and (time_silent + current_response["duration"])
                > self.transcriber_config.endpointing_config.time_cutoff_seconds
            )
        raise Exception("Endpointing config not supported")

    def calculate_time_silent(self, data: dict):
        end = data["start"] + data["duration"]
        words = data["channel"]["alternatives"][0]["words"]
        if words:
            return end - words[-1]["end"]
        return data["duration"]

    async def send_audio_to_ws(
        self, ws: WebSocketClientProtocol
    ):  # sends audio to websocket
        self.logger.debug("Starting Deepgram transcriber sender")
        while not self._ended:
            try:
                data = await asyncio.wait_for(self.audio_queue.get(), 1)
            except asyncio.exceptions.TimeoutError:
                self.logger.exception("Deepgram transcriber sender: TimeoutError")
                break
            await ws.send(data)
        self.logger.debug("Terminating Deepgram transcriber sender")

    async def receive_msg_from_ws(self, ws: WebSocketClientProtocol):
        self.logger.debug("Starting Deepgram transcriber receiver")
        # TODO(julien) andles websocket disconnection gracefully
        self.msg_deque = deque()

        while not self._ended:
            try:
                msg = await ws.recv()
                self.logger.debug(
                    "Deepgram: received message. Queue size: %i", len(self.msg_deque)
                )
                data = json.loads(msg)
                self.msg_deque.appendleft(data)
            except Exception as e:
                self.logger.exception("Error in Deepgram transcriber receiver")
                self.logger.debug(f"Got error {e} in Deepgram receiver")
                break

        self.logger.debug("Terminating Deepgram transcriber receiver")

    async def process_msg_from_queue(self):
        try:
            buffer = ""
            time_silent = 0

            while True:
                if not len(self.msg_deque):
                    await asyncio.sleep(0.0001)
                    continue

                self.logger.debug(
                    "Deepgram: about to process new message. Queue size: %i",
                    len(self.msg_deque),
                )
                data = self.msg_deque.pop()

                if len(self.msg_deque):
                    last_data = self.msg_deque[0]
                    # TODO(julien) Keep this temporarily to see if that can actually happen
                    # If not, this is not necessary to have a queue
                    self.logger.error(
                        "Deepgram transriber receiver: multiple messages in msg_deque. %s",
                        str(len(self.msg_deque) + 1),
                    )
                else:
                    last_data = None

                # TODO What to do with buffered message if any?
                # means we've finished receiving transcriptions
                if "is_final" not in data or (
                    last_data and "is_final" not in last_data
                ):
                    break

                speech_final = self.is_speech_final(
                    buffer, data, last_data, time_silent
                )
                data_is_final = is_final(data)
                data_top_choice = top_choice(data)
                data_confidence = confidence(data)

                if (
                    data_top_choice["transcript"]
                    and data_confidence > 0.0
                    and data_is_final
                ):
                    # do not a whitespace in between when the buffer is empty
                    # make it a one liner
                    buffer = (
                        data_top_choice["transcript"]
                        if not buffer
                        else f"{buffer} {data_top_choice['transcript']}"
                    )

                if speech_final:
                    # TODO(julien) Is that ok to use the confidence of the last message only?
                    await self.on_response(Transcription(buffer, data_confidence, True))
                    buffer = ""
                    time_silent = 0
                elif data_top_choice["transcript"] and data_confidence > 0.0:
                    await self.on_response(
                        Transcription(buffer, data_confidence, False)
                    )
                    time_silent = self.calculate_time_silent(data)
                else:
                    # TODO(julien) What's point of this? Can't we just rely on Deepgram for this?
                    time_silent += data["duration"]
        except Exception as e:
            self.logger.exception("Deepgram transcriber: process_msg_from_queue error")

    async def process(self):
        extra_headers = {"Authorization": f"Token {self.api_key}"}

        async with websockets.connect(
            self.get_deepgram_url(), extra_headers=extra_headers
        ) as ws:
            await asyncio.gather(
                self.send_audio_to_ws(ws),
                self.receive_msg_from_ws(ws),
                self.process_msg_from_queue(),
            )


def top_choice(resp):
    return resp["channel"]["alternatives"][0]


def confidence(resp):
    confidence = top_choice(resp)["confidence"]
    if confidence == 0:
        print(top_choice(resp))
    return confidence


def is_final(resp):
    return resp["is_final"]
