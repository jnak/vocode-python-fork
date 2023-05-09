import asyncio
import logging
import random
from typing import Optional
from vocode.streaming.models.agent import (
    AgentConfig,
    ChatGPTAgentConfig,
    LLMAgentConfig,
)
from vocode.streaming.models.worker import SimpleAsyncWorker
from vocode.streaming.transcriber.base_transcriber import Transcription


class BaseAgent(SimpleAsyncWorker):
    def __init__(
        self,
        agent_config: AgentConfig,
        # TODO(julien) We want to make this a final transcription
        transcription_queue: asyncio.Queue[Transcription],
        # TODO(julien) We probably want to do this
        message_queue: asyncio.Queue[str],
    ):
        self.agent_config = agent_config
        super().__init__(transcription_queue, message_queue)

    # TODO(julien) This is only used by StreamingConversation. It feels like the conversation should keep a reference around...
    def get_agent_config(self) -> AgentConfig:
        return self.agent_config

    # TODO(julien) It would be more correct / easier to have a function that sets the words / sentences once they have been said
    def update_last_bot_message_on_cut_off(self, message: str):
        """Updates the last bot message in the conversation history when the human cuts off the bot's response."""
        pass

    def get_cut_off_response(self) -> Optional[str]:
        # TODO(julien) This is meh to have the bass class check for subclasses
        assert isinstance(self.agent_config, LLMAgentConfig) or isinstance(
            self.agent_config, ChatGPTAgentConfig
        )
        on_cut_off_messages = self.agent_config.cut_off_response.messages
        if on_cut_off_messages:
            return random.choice(on_cut_off_messages).text
