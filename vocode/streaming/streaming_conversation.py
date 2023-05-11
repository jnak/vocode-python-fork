import asyncio
import queue
from typing import AsyncGenerator, Awaitable, Callable, Optional, Tuple
import logging
import threading
import time
import random

from vocode.streaming.agent.bot_sentiment_analyser import (
    BotSentimentAnalyser,
)
from vocode.streaming.agent.factory import AgentFactory
from vocode.streaming.models.events import TranscriptCompleteEvent
from vocode.streaming.models.message import BaseMessage
from vocode.streaming.models.transcriber import TranscriberConfig
from vocode.streaming.output_device.base_output_device import BaseOutputDevice
from vocode.streaming.synthesizer.factory import SynthesizerFactory
from vocode.streaming.transcriber.factory import TranscriberFactory
from vocode.streaming.utils.events_manager import EventsManager
from vocode.streaming.utils.goodbye_model import GoodbyeModel
from vocode.streaming.utils.transcript import Transcript

from vocode.streaming.models.agent import (
    AgentConfig,
    FillerAudioConfig,
    FILLER_AUDIO_DEFAULT_SILENCE_THRESHOLD_SECONDS,
)
from vocode.streaming.models.synthesizer import (
    SentimentConfig,
    SynthesizerConfig,
)
from vocode.streaming.constants import (
    TEXT_TO_SPEECH_CHUNK_SIZE_SECONDS,
    PER_CHUNK_ALLOWANCE_SECONDS,
    ALLOWED_IDLE_TIME,
)
from vocode.streaming.agent.base_agent import BaseAgent
from vocode.streaming.synthesizer.base_synthesizer import (
    BaseSynthesizer,
    SynthesisResult,
    FillerAudio,
)
from vocode.streaming.utils import (
    create_conversation_id,
    create_loop_in_thread,
    get_chunk_size_per_second,
)
from vocode.streaming.transcriber.base_transcriber import (
    Transcription,
    BaseTranscriber,
)


class StreamingConversation:
    def __init__(
        self,
        agent_config: AgentConfig,
        agent_factory: AgentFactory,
        synthesizer_config: SynthesizerConfig,
        synthesizer_factory: SynthesizerFactory,
        transcriber_config: TranscriberConfig,
        transcriber_factory: TranscriberFactory,
        # TODO(julien) Change oupput device to make it consistant with input and output
        output_device: BaseOutputDevice,
        conversation_id: str = None,
        per_chunk_allowance_seconds: float = PER_CHUNK_ALLOWANCE_SECONDS,
        events_manager: Optional[EventsManager] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.id = conversation_id or create_conversation_id()
        self.logger = logger or logging.getLogger(__name__)
        self.output_device = output_device

        self.input_audio_queue = asyncio.Queue
        self.transcription_queue = asyncio.Queue
        self.transcriber = transcriber_factory.create_transcriber(
            transcriber_config, self.input_audio_queue, self.transcription_queue
        )

        # hande_transcription_task consumes self.transcription_queue and produce on this queue
        self.final_transcription_queue = asyncio.Queue

        self.agent_message_queue = asyncio.Queue
        self.agent = agent_factory.create_agent(
            agent_config, self.final_transcription_queue, self.agent_message_queue
        )

        self.synthesized_audio_queue = asyncio.Queue
        self.synthesizer = synthesizer_factory.create_synthesizer(
            synthesizer_config, self.agent_message_queue, self.synthesized_audio_queue
        )
        # TODO(julien) Move this logic into the synthesizer class and abstract it away
        self.synthesizer_event_loop = asyncio.new_event_loop()
        # self.synthesizer_thread = threading.Thread(
        #     name="synthesizer",
        #     target=create_loop_in_thread,
        #     args=(self.synthesizer_event_loop,),
        # )

        # TODO(julien) Let's try to get this coupled for now
        self.output_audio_queue = asyncio.Queue

        self.events_manager = events_manager or EventsManager()
        self.events_task = None
        self.per_chunk_allowance_seconds = per_chunk_allowance_seconds
        self.transcript = Transcript()
        self.bot_sentiment = None
        if self.agent.get_agent_config().track_bot_sentiment:
            self.sentiment_config = (
                self.synthesizer.get_synthesizer_config().sentiment_config
            )
            if not self.sentiment_config:
                self.sentiment_config = SentimentConfig()
            self.bot_sentiment_analyser = BotSentimentAnalyser(
                emotions=self.sentiment_config.emotions
            )
        if self.agent.get_agent_config().end_conversation_on_goodbye:
            self.goodbye_model = GoodbyeModel()

        self.is_human_speaking = False
        self.active = False
        self.current_synthesis_task = None
        self.is_current_synthesis_interruptable = False
        self.stop_events: queue.Queue[threading.Event] = queue.Queue()
        self.last_action_timestamp = time.time()
        self.check_for_idle_task = None
        self.track_bot_sentiment_task = None
        self.should_wait_for_filler_audio_done_event = False
        self.current_filler_audio_done_event: Optional[threading.Event] = None
        self.current_filler_seconds_per_chunk: int = 0
        self.current_transcription_is_interrupt: bool = False

        self.output_audio_chunk_size = self.compute_chunk_output_audio_size()

        if not self.agent.get_agent_config().generate_responses:
            # TODO(julien) Delete this branch
            raise NotImplementedError

    def compute_chunk_output_audio_size(self):
        seconds_per_chunk = TEXT_TO_SPEECH_CHUNK_SIZE_SECONDS
        return (
            get_chunk_size_per_second(
                self.synthesizer.get_synthesizer_config().audio_encoding,
                self.synthesizer.get_synthesizer_config().sampling_rate,
            )
            * seconds_per_chunk
        )

    async def start(self, mark_ready: Optional[Callable[[], Awaitable[None]]] = None):
        self.synthesizer_thread.start()
        self.agent.start()
        self.handle_transcription_task = asyncio.create_task(
            self.handle_transcription()
        )
        self.transcriber.start()

        # julien - Ignore all this
        if self.agent.get_agent_config().send_filler_audio:
            filler_audio_config = (
                self.agent.get_agent_config().send_filler_audio
                if isinstance(
                    self.agent.get_agent_config().send_filler_audio, FillerAudioConfig
                )
                else FillerAudioConfig()
            )
            self.synthesizer.set_filler_audios(filler_audio_config)

        if mark_ready:
            await mark_ready()
        if self.agent.get_agent_config().initial_message:
            self.transcript.add_bot_message(
                text=self.agent.get_agent_config().initial_message.text,
                events_manager=self.events_manager,
                conversation_id=self.id,
            )
        if self.synthesizer.get_synthesizer_config().sentiment_config:
            self.update_bot_sentiment()
        if self.agent.get_agent_config().initial_message:
            self.send_message_to_stream_nonblocking(
                self.agent.get_agent_config().initial_message, False
            )
        self.active = True
        if self.synthesizer.get_synthesizer_config().sentiment_config:
            self.track_bot_sentiment_task = asyncio.create_task(
                self.track_bot_sentiment()
            )
        self.check_for_idle_task = asyncio.create_task(self.check_for_idle())
        if len(self.events_manager.subscriptions) > 0:
            self.events_task = asyncio.create_task(self.events_manager.start())

    async def check_for_idle(self):
        while self.is_active():
            if time.time() - self.last_action_timestamp > (
                self.agent.get_agent_config().allowed_idle_time_seconds
                or ALLOWED_IDLE_TIME
            ):
                self.logger.debug("Conversation idle for too long, terminating")
                self.mark_terminated()
                return
            await asyncio.sleep(15)

    async def track_bot_sentiment(self):
        prev_transcript = None
        while self.is_active():
            await asyncio.sleep(1)
            if self.transcript.to_string() != prev_transcript:
                self.update_bot_sentiment()
                prev_transcript = self.transcript.to_string()

    def update_bot_sentiment(self):
        new_bot_sentiment = self.bot_sentiment_analyser.analyse(
            self.transcript.to_string()
        )
        if new_bot_sentiment.emotion:
            self.logger.debug("Bot sentiment: %s", new_bot_sentiment)
            self.bot_sentiment = new_bot_sentiment

    def receive_audio(self, chunk: bytes):
        self.input_audio_queue.put_nowait(chunk)

    async def handle_agent_message(self):
        while self.active:
            agent_response: Tuple[
                BaseMessage, bool, asyncio.Event
            ] = await self.agent_message_queue.get()
            (
                message,
                is_interruptible,
                stop_event,
            ) = agent_response
            if is_interruptible and stop_event.is_set():
                continue

            # TODO(julien) This is dangerous
            self.is_current_synthesis_interruptable = is_interruptible
            self.logger.debug("Synthesizing speech for message")

            # self.current_synthesis_span = tracer.start_span(
            #     SYNTHESIS_TRACE_NAME,
            #     {"synthesizer": str(self.synthesizer.get_synthesizer_config().type)},
            # )

            # TODO(julien) This should not be awaited otherwise you can't deal

            # TODODODOOD WORK ON THIS

            loop = asyncio.get_event_loop()
            synthesis_result = await loop.run_in_executor(
                None,
                lambda: self.synthesizer.create_speech(
                    message, 
                    self.output_audio_chunk_size, 
                    bot_sentiment=self.bot_sentiment
                )
            )
            
            message_sent, cut_off = await loop self.send_speech_to_output(
                message.text,
                synthesis_result,
                stop_event,
                seconds_per_chunk,
            )
            self.logger.debug("Message sent: {}".format(message_sent))
            if cut_off:
                self.agent.update_last_bot_message_on_cut_off(message_sent)
            self.transcript.add_bot_message(
                text=message_sent,
                events_manager=self.events_manager,
                conversation_id=self.id,
            )

    def warmup_synthesizer(self):
        self.synthesizer.ready_synthesizer()

    async def send_speech_to_output(
        self,
        message,
        synthesis_result: SynthesisResult,
        stop_event: threading.Event,
        seconds_per_chunk: int,
        is_filler_audio: bool = False,
    ):
        """
        Returns an estimate of what was sent up to, and a flag if the message was cut off.
        This process limit the throughput so the audio is sent at the same speed as it is played back.
        """
        message_sent = message
        cut_off = False
        chunk_size = seconds_per_chunk * get_chunk_size_per_second(
            self.synthesizer.get_synthesizer_config().audio_encoding,
            self.synthesizer.get_synthesizer_config().sampling_rate,
        )
        for i, chunk_result in enumerate(synthesis_result.chunk_generator):
            start_time = time.time()
            speech_length_seconds = seconds_per_chunk * (
                len(chunk_result.chunk) / chunk_size
            )
            if stop_event.is_set():
                seconds = i * seconds_per_chunk
                self.logger.debug(
                    "Interrupted, stopping text to speech after {} chunks".format(i)
                )
                message_sent = f"{synthesis_result.get_message_up_to(seconds)}-"
                cut_off = True
                break
            if i == 0:
                if is_filler_audio:
                    self.should_wait_for_filler_audio_done_event = True
            await self.output_device.send_async(chunk_result.chunk)
            end_time = time.time()
            await asyncio.sleep(
                max(
                    speech_length_seconds
                    - (end_time - start_time)
                    - self.per_chunk_allowance_seconds,
                    0,
                )
            )
            self.logger.debug(
                "Sent chunk {} with size {}".format(i, len(chunk_result.chunk))
            )
            self.last_action_timestamp = time.time()
        # clears it off the stop events queue
        if not stop_event.is_set():
            stop_event.set()
        return message_sent, cut_off

    async def handle_transcription(self):
        """
        This is run as a task
        """
        while self.active:
            # TODO(julien) Monitor queue depth
            transcription = await self.transcription_queue.get()

            self.last_action_timestamp = time.time()
            if transcription.is_final:
                self.logger.debug(
                    "Got transcription: {}, confidence: {}".format(
                        transcription.message, transcription.confidence
                    )
                )
            if not self.is_human_speaking and transcription.confidence > (
                self.transcriber.get_transcriber_config().min_interrupt_confidence or 0
            ):
                # send interrupt
                self.current_transcription_is_interrupt = False
                if self.is_current_synthesis_interruptable:
                    self.logger.debug("Sending interrupt")
                    # TODO(julien) We want to interrupt the LLM, the synthesis, and the playback
                    self.current_transcription_is_interrupt = (
                        self.interrupt_all_synthesis()
                    )
                self.logger.debug("Human started speaking")

            transcription.is_interrupt = self.current_transcription_is_interrupt
            self.is_human_speaking = not transcription.is_final

            if not transcription.is_final:
                return

            self.transcript.add_human_message(
                text=transcription.message,
                events_manager=self.events_manager,
                conversation_id=self.id,
            )

            # TODO(julien) There was some weird interaction with this and the llm
            # Ignore for now
            # goodbye_detected_task = None
            # if self.agent.get_agent_config().end_conversation_on_goodbye:
            #     goodbye_detected_task = asyncio.create_task(
            #         self.goodbye_model.is_goodbye(transcription.message)
            #     )

            # TODO(julien) This is weird. Why would you want to do this each a new final transcription is received
            # Ignore that for now
            # if self.agent.get_agent_config().send_filler_audio:
            #     self.logger.debug("Sending filler audio")
            #     if self.synthesizer.filler_audios:
            #         filler_audio = random.choice(self.synthesizer.filler_audios)
            #         self.logger.debug(f"Chose {filler_audio.message.text}")
            #         self.current_filler_audio_done_event = threading.Event()
            #         self.current_filler_seconds_per_chunk = (
            #             filler_audio.seconds_per_chunk
            #         )
            #         stop_event = self.enqueue_stop_event()
            #         asyncio.run_coroutine_threadsafe(
            #             self.send_filler_audio_to_output(
            #                 filler_audio,
            #                 stop_event,
            #                 done_event=self.current_filler_audio_done_event,
            #             ),
            #             self.synthesizer_event_loop,
            #         )
            #     else:
            #         self.logger.debug("No filler audio available for synthesizer")
            # self.logger.debug("Generating response for transcription")

            stop_event = self.enqueue_stop_event()
            self.final_transcription_queue.put_nowait((transcription, stop_event))

            # if goodbye_detected_task:
            #     try:
            #         goodbye_detected = await asyncio.wait_for(
            #             goodbye_detected_task, 0.1
            #         )
            #         if goodbye_detected:
            #             self.logger.debug("Goodbye detected, ending conversation")
            #             self.mark_terminated()
            #             return
            #     except asyncio.TimeoutError:
            #         self.logger.debug("Goodbye detection timed out")

    def enqueue_stop_event(self):
        stop_event = threading.Event()
        self.stop_events.put_nowait(stop_event)
        return stop_event

    def interrupt_all_synthesis(self):
        """Returns true if any synthesis was interrupted"""
        num_interrupts = 0
        self.logger.debug("Synthesizer: About to interrupt")
        while True:
            try:
                stop_event = self.stop_events.get_nowait()
                if not stop_event.is_set():
                    self.logger.debug("Synthesizer: Interrupting")
                    stop_event.set()
                    num_interrupts += 1
            except queue.Empty:
                break
        return num_interrupts > 0

    # NOTE(julien) The fact this function can be called from anywhere introduce
    # potential concurrency issues because is_current_synthesis_interruptable is set
    async def send_filler_audio_to_output(
        self,
        filler_audio: FillerAudio,
        stop_event: threading.Event,
        done_event: threading.Event,
    ):
        filler_synthesis_result = filler_audio.create_synthesis_result()
        self.is_current_synthesis_interruptable = filler_audio.is_interruptable
        if isinstance(
            self.agent.get_agent_config().send_filler_audio, FillerAudioConfig
        ):
            silence_threshold = (
                self.agent.get_agent_config().send_filler_audio.silence_threshold_seconds
            )
        else:
            silence_threshold = FILLER_AUDIO_DEFAULT_SILENCE_THRESHOLD_SECONDS
        await asyncio.sleep(silence_threshold)
        self.logger.debug("Sending filler audio to output")
        await self.send_speech_to_output(
            filler_audio.message.text,
            filler_synthesis_result,
            stop_event,
            filler_audio.seconds_per_chunk,
            is_filler_audio=True,
        )
        done_event.set()

    def wait_for_filler_audio_to_finish(self):
        if not self.should_wait_for_filler_audio_done_event:
            self.logger.debug(
                "Not waiting for filler audio to finish since we didn't send any chunks"
            )
            return
        self.should_wait_for_filler_audio_done_event = False
        if (
            self.current_filler_audio_done_event
            and not self.current_filler_audio_done_event.is_set()
        ):
            self.logger.debug("Waiting for filler audio to finish")
            # this should guarantee that filler audio finishes, since it has to be on its last chunk
            if not self.current_filler_audio_done_event.wait(
                self.current_filler_seconds_per_chunk
            ):
                self.logger.debug("Filler audio did not finish")

    def mark_terminated(self):
        self.active = False

    # must be called from the main thread
    def terminate(self):
        self.mark_terminated()
        self.events_manager.publish_event(
            TranscriptCompleteEvent(
                conversation_id=self.id, transcript=self.transcript.to_string()
            )
        )
        if self.check_for_idle_task:
            self.logger.debug("Terminating check_for_idle Task")
            self.check_for_idle_task.cancel()
        if self.track_bot_sentiment_task:
            self.logger.debug("Terminating track_bot_sentiment Task")
            self.track_bot_sentiment_task.cancel()
        if self.events_manager and self.events_task:
            self.logger.debug("Terminating events Task")
            self.events_manager.end()

        self.logger.debug("Terminating: transcriber")
        self.transcriber.terminate()

        self.logger.debug("Terminating agent")
        self.agent.terminate()

        self.logger.debug("Terminating synthesizer event loop")
        self.synthesizer.termininate()
        self.logger.debug("Terminating synthesizer thread")
        if self.synthesizer_thread.is_alive():
            self.synthesizer_thread.join()

        self.logger.debug("Terminating transcriber task")

    def is_active(self):
        return self.active
