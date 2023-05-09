import asyncio
from langchain.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
)
from langchain.chains import ConversationChain
from langchain.chat_models import ChatOpenAI
from langchain.llms import OpenAIChat
from langchain.memory import ConversationBufferMemory
from langchain.schema import ChatMessage, AIMessage
import openai
from typing import AsyncGenerator, Optional, Tuple

import logging
from vocode import getenv

from vocode.streaming.agent.base_agent import BaseAgent
from vocode.streaming.models.agent import ChatGPTAgentConfig
from vocode.streaming.agent.utils import stream_openai_response_async
from vocode.streaming.models.worker import QueueWorker
from vocode.streaming.transcriber.base_transcriber import Transcription


class ChatGPTAgent(BaseAgent, QueueWorker):
    def __init__(
        self,
        agent_config: ChatGPTAgentConfig,
        # TODO(julien) We want to make this a final transcription
        input_queue: asyncio.Queue[Transcription],
        # TODO(julien) We probably want to do this
        output_queue: asyncio.Queue[str],
        logger: logging.Logger | None = None,
        openai_api_key: str | None = None,
    ):
        super().__init__(agent_config, input_queue, output_queue)
        openai.api_key = openai_api_key or getenv("OPENAI_API_KEY")
        if not openai.api_key:
            raise ValueError("OPENAI_API_KEY must be set in environment or passed in")
        self.agent_config = agent_config
        self.logger = logger or logging.getLogger(__name__)
        self.logger.setLevel(logging.DEBUG)
        self.prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessagePromptTemplate.from_template(agent_config.prompt_preamble),
                MessagesPlaceholder(variable_name="history"),
                HumanMessagePromptTemplate.from_template("{input}"),
            ]
        )
        self.memory = ConversationBufferMemory(return_messages=True)
        if agent_config.initial_message:
            if (
                agent_config.generate_responses
            ):  # we use ChatMessages for memory when we generate responses
                self.memory.chat_memory.messages.append(
                    ChatMessage(
                        content=agent_config.initial_message.text, role="assistant"
                    )
                )
            else:
                self.memory.chat_memory.add_ai_message(
                    agent_config.initial_message.text
                )
        self.llm = ChatOpenAI(
            model_name=self.agent_config.model_name,
            temperature=self.agent_config.temperature,
            max_tokens=self.agent_config.max_tokens,
            openai_api_key=openai.api_key,
        )
        self.conversation = ConversationChain(
            memory=self.memory, prompt=self.prompt, llm=self.llm
        )

        # TODO(julien) Move this out of the init function so create_first_response
        # can be made async and awaited
        self.first_response = (
            self.create_first_response(agent_config.expected_first_prompt)
            if agent_config.expected_first_prompt
            else None
        )
        self.is_first_response = True

    def create_first_response(self, first_prompt):
        # TODO(julien) This should be async.
        self.logger.debug("LLM First message: %s", first_prompt)
        return self.conversation.predict(input=first_prompt)

    async def process(transcription):
        self.memory.chat_memory.messages.append(
            ChatMessage(role="user", content=transcription.)
        )
        # # TODO(julien) This probably does not happen because transcriptions are discarded if they are not final
        # if is_interrupt and self.agent_config.cut_off_response:
        #     # This is for a feature where the AI Will say something when it's being interrupted
        #     # TODO(julien) Let's not worry about it for now. Especially since it probably does not work
        #     cut_off_response = self.get_cut_off_response()
        #     self.memory.chat_memory.messages.append(
        #         ChatMessage(role="assistant", content=cut_off_response)
        #     )
        #     self.logger.debug("LLM Interupted: %s", cut_off_response)
        #     yield cut_off_response
        #     return

        prompt_messages = [
            ChatMessage(role="system", content=self.agent_config.prompt_preamble)
        ] + self.memory.chat_memory.messages
        stream = await openai.ChatCompletion.acreate(
            model=self.agent_config.model_name,
            messages=[
                prompt_message.dict(include={"content": True, "role": True})
                for prompt_message in prompt_messages
            ],
            max_tokens=self.agent_config.max_tokens,
            temperature=self.agent_config.temperature,
            stream=True,
        )

        bot_memory_message = ChatMessage(role="assistant", content="")
        self.memory.chat_memory.messages.append(bot_memory_message)
        async for message in stream_openai_response_async(
            stream,
            get_text=lambda choice: choice.get("delta", {}).get("content"),
        ):
            bot_memory_message.content = (
                f"{bot_memory_message.content} {message}"
                if bot_memory_message.content
                else message
            )
            self.logger.debug("LLM Sentence: %s", message)
            self.output_queue.put_nowait(message)

    def update_last_bot_message_on_cut_off(self, message: str):
        """Get the last message from the agent and delete"""
        # TODO(julien) It would probably make more sense to add messages only once they've been played back
        # There is no use for having in the history until then
        for memory_message in self.memory.chat_memory.messages[::-1]:
            if (
                isinstance(memory_message, ChatMessage)
                and memory_message.role == "assistant"
            ) or isinstance(memory_message, AIMessage):
                memory_message.content = message
                return

    def terminate(self):
        self.logger.debug("Full chat transcript: %s", self.memory.chat_memory.messages)
