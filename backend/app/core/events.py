"""进程内的 SSE 事件总线（单机部署足够，无需引入 Redis/MQ）。

每个任务维护一条自增 id 的事件流：新订阅者先按 `Last-Event-ID` 回放历史事件，
再接上实时队列。因此前端刷新页面或断线重连后不会丢进度，也不会重复消费。
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

# 单任务保留的历史事件上限，防止长任务把内存撑爆
MAX_HISTORY = 4000
# 订阅者队列上限；消费不过来时丢弃最老事件，避免拖垮生产端
QUEUE_MAXSIZE = 1000


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._counter = itertools.count(1)

    def publish(self, task_id: str, event: str, data: dict[str, Any]) -> None:
        """同步发布（可从任意协程调用，不阻塞）。"""
        payload = {"id": next(self._counter), "event": event, "data": data}
        history = self._history[task_id]
        history.append(payload)
        if len(history) > MAX_HISTORY:
            del history[: len(history) - MAX_HISTORY]

        for queue in list(self._subscribers.get(task_id, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 慢消费者：丢最老的一条，保证新进度能推进
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    logger.warning("SSE 队列拥塞，丢弃事件 task=%s", task_id)

    def subscribe(self, task_id: str, last_event_id: int = 0) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        # 先灌历史再注册，避免回放与实时事件乱序
        for payload in self._history.get(task_id, ()):
            if payload["id"] > last_event_id:
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:  # pragma: no cover
                    break
        self._subscribers[task_id].add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        self._subscribers.get(task_id, set()).discard(queue)

    def history(self, task_id: str) -> list[dict[str, Any]]:
        return list(self._history.get(task_id, ()))

    def clear(self, task_id: str) -> None:
        self._history.pop(task_id, None)


bus = EventBus()
