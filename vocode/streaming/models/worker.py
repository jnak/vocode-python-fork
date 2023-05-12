import asyncio
import threading
import collections
import janus
import inspect


class AsyncWorker:
    _TASK_NAME = '_run_loop'

    def __init__(self, input_queue: asyncio.Queue, output_queue: asyncio.Queue) -> None:
        self.worker_task: None | asyncio.Task = None
        self.input_queue = input_queue
        self.output_queue = output_queue

    def set_task(self, instance):
        task_func = instance.worker_task if hasattr(instance, 'WORKER_TASK') else instance
        if not inspect.iscoroutinefunction(task_func):
            raise TypeError('Should be an async function')
        setattr(self, self._TASK_NAME, task_func)
        return self

    def start(self) -> asyncio.Task:
        self.worker_task = asyncio.create_task(self._run_loop(self.input_queue, self.output_queue))
        return self.worker_task

    def send_nonblocking(self, item):
        self.input_queue.put_nowait(item)

    async def _run_loop(self, input_queue, output_queue):
        raise NotImplementedError

    # TODO(julien) Should this be async?
    def terminate(self):
        if self.worker_task:
            return self.worker_task.cancel()

        return False
    


class InterruptibleEvent:
    def __init__(
        self,
        is_interruptible,
        payload=None,
    ):
        self.interruption_event = threading.Event()
        self.is_interruptible = is_interruptible
        self.payload = payload

    def interrupt(self):
        return False if self.is_interruptible else self.interruption_event.set()

    def is_interrupted(self):
        return self.is_interruptible and self.interruption_event.is_set()


class IterruptipleWorker(AsyncWorker):
    _TASK_NAME = '_process_item'

    def __init__(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
        max_concurrency=2,
    ) -> None:
        super().__init__(input_queue, output_queue)
        self.max_concurrency = max_concurrency
        self.task_queue = collections.deque()

    def set_task(process_item):
        if not inspect.iscoroutinefunction(process_item):
            raise TypeError('Should be an async function')
        self.process_item = process_item

    async def _run_loop(self):
        # TODO(julien) Implement concurrency with max_nb_of_thread
        try:
            while True:
                item = await self.input_queue.get()
                if isinstance(item, InterruptibleEvent) and item.is_interrupted():
                    continue
        # TODO (julien) Finishi this
            self._run_task ...
        except Exception:
            pass

    async def _run_task(self, item):
        # TODO(julien) Finish this
        interuptible_event = item
        self.current_task = asyncio.create_task(
            self.process_item(item, self.output_queue)
        )
        await self.current_task
        self.current_task = None
        except asyncio.CancelledError:
            pass

    async def _process_item(self, item, output_queue):
        """
        Publish results onto output queue.
        Calls to async function / task should be able to handle asyncio.CancelledError gracefully:
        """
        raise NotImplementedError

    def cancel_current_task(self):
        """Free up the resources. That's useful so implementors do not have to implement this but:
        - threads tasks won't be able to be interrupted. Hopefully not too much of a big deal
            Threads will also get a reference to the interuptible event
        - asyncio tasks will still have to handle CancelledError and clean up resources
        """
        # TODO(julien) Finish this
        if self.current_task and self.interuptible_event.is_interruptible:
            return self.current_task.cancel()

        return False


class ThreadAsync:
    """
    This would be the synthesizer
    """

    _EOQ = object()

    async def process(self, item, output_queue):
        output_janus_queue = janus.Queue
        thread_task = asyncio.to_thread(
            self.blocking_task, output_janus_queue.sync_q, *item
        )
        forward_task = asyncio.create_task(
            self._forward_from_thead(output_janus_queue, output_queue)
        )
        await thread_task
        output_janus_queue.async_q.put_nowait(self._EOQ)
        await forward_task

    async def _forward_from_thead(self, output_janus_queue, output_queue):
        while True:
            thread_item = await output_janus_queue.async_q.get()
            if self._EOQ:
                return
            output_queue.put_nowait(thread_item)

    def blocking_task(self, output_queue, *args):
        raise NotImplementedError

"""
Here is an example of how that would work

class BaseSynth:
    Worker = None
    WORKER_TASK = 'create_speech'


class MySynth(BaseSynth):
    Worker = IterruptipleWorker

    def create_speech(item, output_queue):
        ...

MySynthesizer.Worker(
    ..., 
).set_task(
    MySynthesizer(
       ...
    )
)

class BaseTranscriber:
    Worker = AsyncWorker()
    WORKER_TASK = 'run_loop'

class MyTranscriber(BaseTranscriber):

    def run_loop(self, input_queue, output_queue):
        ...

MyTranscriber.Worker(
    ...
).set_task(
   MyTranscriber(...)
)

"""