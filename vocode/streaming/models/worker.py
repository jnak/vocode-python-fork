import asyncio
import threading
import janus


class AsyncWorker:
    def __init__(self, input_queue: asyncio.Queue, output_queue: asyncio.Queue) -> None:
        self.worker_task: None | asyncio.Task = None
        self.input_queue = input_queue
        self.output_queue = output_queue

    def start(self) -> asyncio.Task:
        self.worker_task = asyncio.create_task(self._run_loop())
        return self.worker_task

    def send_nonblocking(self, item):
        self.input_queue.put_nowait(item)

    async def _run_loop(self):
        raise NotImplementedError

    # TODO(julien) Should this be async?
    def terminate(self):
        if self.worker_task:
            return self.worker_task.cancel()

        return False


# class InterruptibleAsyncWorker(AsyncWorker):


class ThreadAsyncWorker(AsyncWorker):
    """
    This would be the synthesizer
    """

    _EOQ = object()

    def __init__(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
        blocking_task: function,
        max_nb_of_thread=2,
    ) -> None:
        super().__init__(input_queue, output_queue)
        self.max_nb_of_thread = max_nb_of_thread
        self.blocking_task = blocking_task
        self.output_janus_queue = janus.Queue

    def start(self) -> asyncio.Task:
        self.current_task = asyncio.create_task(self.run_loop())

    async def _run_loop(self):
        # TODO(julien) Implement concurrency
        while True:
            item = await self.input_queue.get()
            self.current_task = asyncio.to_thread(
                self.blocking_task, self.output_janus_queue.sync_q, *item
            )
            self.forward_task = asyncio.create_task(
                self._forward_from_thead(self.janus_output_queue)
            )
            await self.current_task
            self.output_janus_queue.async_q.put_nowait(self._EOQ)
            await self.forward_task

            try:
                item = await self.current_task
            except asyncio.CancelledError:
                pass

    async def _forward_from_thead(self, output_janus_queue):
        while True:
            thread_item = await output_janus_queue.async_q.get()
            if self._EOQ:
                return
            self.output_queue.put_nowait(thread_item)


class SimpleQueueWorker(AsyncWorker):
    def __init__(self, input_queue: asyncio.Queue, output_queue: asyncio.Queue) -> None:
        super().__init__(input_queue, output_queue)
        self.current_task: None | asyncio.Task = None

    async def run_loop(self):
        while True:
            item = await self.input_queue.get()
            self.current_task = asyncio.create_task(self.process(item))
            try:
                await self.current_task
            except asyncio.CancelledError:
                # This is a good place to do something if needed
                # Ex: update the cut_off message
                pass

    async def process(self, item):
        """
        Publish results onto output queue.
        Calls to async function / task should be able to handle
        asyncio.CancelledError gracefully:
            - Ideally, you would want to make sure that async tasks await-ed
              down the call stack are also cancelled
              and are also handling CancelledError gracefully.
            - When making async aiohttp calls, things should be ok, but it's
              always best to add tests for that.
        TODO(julien) Add function to recursively cancel and patterns to test
            cancellation
        TODO(julien) Do we want to abstract away the output queue so it only process stuff?
            Let's see once we implement a couple
        """
        raise NotImplementedError

    def cancel_current_task(self):
        if self.current_task and not self.current_task.done():
            return self.current_task.cancel()

        return False

    async def _cancel_recursively(self, task: asyncio.Task):
        # TODO(julien) Do we want to use this or something like hiarchechical cancellation such any.IO?
        # https://stackoverflow.com/questions/70596654/asyncio-automatic-cancellation-of-subtasks
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if hasattr(task, "_fut_waiter") and not task._fut_waiter.done():
            await self._cancel_recursively(task._fut_waiter)
