from fastapi import WebSocket
import base64
from enum import Enum
import json
import logging
from typing import Optional
from vocode import getenv
from vocode.streaming.agent.base_agent import BaseAgent
from vocode.streaming.agent.factory import AgentFactory
from vocode.streaming.models.agent import AgentConfig
from vocode.streaming.models.events import PhoneCallConnectedEvent, PhoneCallEndedEvent

from vocode.streaming.streaming_conversation import StreamingConversation
from vocode.streaming.models.telephony import CallConfig, TwilioConfig
from vocode.streaming.output_device.twilio_output_device import TwilioOutputDevice
from vocode.streaming.models.synthesizer import (
    AzureSynthesizerConfig,
    SynthesizerConfig,
)
from vocode.streaming.models.transcriber import (
    DeepgramTranscriberConfig,
    PunctuationEndpointingConfig,
    TranscriberConfig,
)
from vocode.streaming.synthesizer.azure_synthesizer import AzureSynthesizer
from vocode.streaming.synthesizer.base_synthesizer import BaseSynthesizer
from vocode.streaming.synthesizer.factory import SynthesizerFactory
from vocode.streaming.telephony.config_manager.base_config_manager import (
    BaseConfigManager,
)
from vocode.streaming.telephony.constants import DEFAULT_SAMPLING_RATE
from vocode.streaming.telephony.twilio import create_twilio_client, end_twilio_call
from vocode.streaming.models.audio_encoding import AudioEncoding
from vocode.streaming.streaming_conversation import StreamingConversation
from vocode.streaming.transcriber.base_transcriber import BaseTranscriber
from vocode.streaming.transcriber.deepgram_transcriber import DeepgramTranscriber
from vocode.streaming.transcriber.factory import TranscriberFactory
from vocode.streaming.utils.events_manager import EventsManager


class PhoneCallAction(Enum):
    CLOSE_WEBSOCKET = 1


class Call(StreamingConversation):
    def __init__(
        self,
        base_url: str,
        config_manager: BaseConfigManager,
        agent_config: AgentConfig,
        agent_factory: AgentFactory,
        synthesizer_config: SynthesizerConfig,
        synthesizer_factory: SynthesizerFactory,
        transcriber_config: TranscriberConfig,
        transcriber_factory: TranscriberFactory,
        conversation_id: str,
        twilio_config: Optional[TwilioConfig] = None,
        twilio_sid: Optional[str] = None,
        events_manager: Optional[EventsManager] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.base_url = base_url
        self.config_manager = config_manager
        # TODO(julien) This is not consistent the TwilioOutputDevice lives there but
        # the input device is here in handle_ws_message and attach_ws_and_start
        self.twilio_config = twilio_config or TwilioConfig(
            account_sid=getenv("TWILIO_ACCOUNT_SID"),
            auth_token=getenv("TWILIO_AUTH_TOKEN"),
        )
        self.twilio_client = create_twilio_client(twilio_config)
        super().__init__(
            output_device=TwilioOutputDevice(),
            conversation_id=conversation_id,
            agent_config=agent_config,
            agent_factory=agent_factory,
            transcriber_config=transcriber_config,
            transcriber_factory=transcriber_factory,
            synthesizer_config=synthesizer_config,
            synthesizer_factory=synthesizer_factory,
            per_chunk_allowance_seconds=0.01,
            events_manager=events_manager,
            logger=logger,
        )
        self.twilio_sid = twilio_sid
        self.latest_media_timestamp = 0

        # TODO(julien) Delete? This was just for logging
        self.last_message = None

    @staticmethod
    def from_call_config(
        base_url: str,
        call_config: CallConfig,
        config_manager: BaseConfigManager,
        conversation_id: str,
        logger: logging.Logger,
        transcriber_factory: TranscriberFactory = TranscriberFactory(),
        agent_factory: AgentFactory = AgentFactory(),
        synthesizer_factory: SynthesizerFactory = SynthesizerFactory(),
        events_manager: Optional[EventsManager] = None,
    ):
        return Call(
            base_url=base_url,
            logger=logger,
            config_manager=config_manager,
            agent_config=call_config.agent_config,
            conversation_id=conversation_id,
            agent_factory=agent_factory,
            transcriber_config=call_config.transcriber_config,
            transcriber_factory=transcriber_factory,
            synthesizer_config=call_config.synthesizer_config,
            synthesizer_factory=synthesizer_factory,
            twilio_config=call_config.twilio_config,
            twilio_sid=call_config.twilio_sid,
            events_manager=events_manager,
        )

    async def attach_ws_and_start(self, ws: WebSocket):
        self.logger.debug("Trying to attach WS to outbound call")
        self.output_device.ws = ws
        self.logger.debug("Attached WS to outbound call")

        twilio_call = self.twilio_client.calls(self.twilio_sid).fetch()

        if twilio_call.answered_by in ("machine_start", "fax"):
            self.logger.info(f"Call answered by {twilio_call.answered_by}")
            twilio_call.update(status="completed")
        else:
            await self.wait_for_twilio_start(ws)
            await super().start()
            self.events_manager.publish_event(
                PhoneCallConnectedEvent(conversation_id=self.id)
            )
            while self.active:
                message = await ws.receive_text()
                response = await self.handle_ws_message(message)
                if response == PhoneCallAction.CLOSE_WEBSOCKET:
                    break
        self.tear_down()

    async def wait_for_twilio_start(self, ws: WebSocket):
        while True:
            message = await ws.receive_text()
            if not message:
                continue
            data = json.loads(message)
            if data["event"] == "start":
                self.logger.debug(
                    f"Media WS: Received event '{data['event']}': {message}"
                )
                self.output_device.stream_sid = data["start"]["streamSid"]
                break

    async def handle_ws_message(self, message) -> PhoneCallAction:
        if message is None:
            return PhoneCallAction.CLOSE_WEBSOCKET

        last_message = self.last_message
        data = json.loads(message)
        self.last_message = data

        if data["event"] == "media":
            media = data["media"]
            chunk = base64.b64decode(media["payload"])
            # TODO(julien) What is this?
            # Check for existence otherwise the first message is always going to go through that
            if self.latest_media_timestamp and self.latest_media_timestamp + 20 < int(
                media["timestamp"]
            ):
                self.logger.error(
                    'self.latest_media_timestamp + 20 < int(media["timestamp"])\n'
                    "current_message: %s \n"
                    "last_message: %s,",
                    data,
                    last_message,
                )
                bytes_to_fill = 8 * (
                    int(media["timestamp"]) - (self.latest_media_timestamp + 20)
                )
                self.logger.debug(f"Filling {bytes_to_fill} bytes of silence")
                # TODO(julien) What is this?
                # NOTE: 0xff is silence for mulaw audio
                self.receive_audio(b"\xff" * bytes_to_fill)
            self.latest_media_timestamp = int(media["timestamp"])

            self.logger.debug("Twilio handle_ws_message")
            self.receive_audio(chunk)
        elif data["event"] == "stop":
            self.logger.debug(f"Media WS: Received event 'stop': {message}")
            self.logger.debug("Stopping...")
            return PhoneCallAction.CLOSE_WEBSOCKET

    def mark_terminated(self):
        super().mark_terminated()
        end_twilio_call(
            self.twilio_client,
            self.twilio_sid,
        )
        self.config_manager.delete_config(self.id)

    def tear_down(self):
        self.events_manager.publish_event(PhoneCallEndedEvent(conversation_id=self.id))
        self.terminate()
