"""Reconnecting Redis pub/sub subscribers (spec 5.2, 7.2).

Two variants with identical semantics:
  * AsyncChannelSubscriber   - redis.asyncio, for the FastAPI lifespan.
  * ThreadedChannelSubscriber - daemon thread, for the synchronous pipeline.

Semantics: connect, subscribe, dispatch each JSON-decoded payload to
on_message. Any connection error triggers reconnect with exponential backoff
(0.1s doubling, cap 5s, reset after a successfully dispatched message).
on_reconnect fires each time a subscription is re-established after the
first. Malformed JSON and on_message exceptions are logged and skipped.
A fresh connection is created per attempt, so a server-side kill is always
observed as a disconnect and a resubscription.
"""
import asyncio
import inspect
import json
import logging
import threading

import redis
import redis.asyncio as aredis

log = logging.getLogger(__name__)

BACKOFF_START = 0.1
BACKOFF_CAP = 5.0
POLL_TIMEOUT = 0.5
HEALTH_CHECK_INTERVAL = 15


def _decode(channel: str, raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        log.warning("malformed message on %s skipped: %s", channel, exc)
        return None


class AsyncChannelSubscriber:
    def __init__(self, url, channel, on_message, on_reconnect=None):
        self.url = url
        self.channel = channel
        self.on_message = on_message
        self.on_reconnect = on_reconnect
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._subscriptions = 0

    def stop(self) -> None:
        self._stop.set()

    async def wait_ready(self) -> None:
        """Returns once a subscription is confirmed (first or later)."""
        await self._ready.wait()

    async def _call(self, fn, *args, what: str):
        try:
            res = fn(*args)
            if inspect.isawaitable(res):
                await res
            return True
        except Exception:  # noqa: BLE001
            log.exception("%s callback failed on %s", what, self.channel)
            return False

    async def _sleep(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), delay)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            client = pubsub = None
            try:
                client = aredis.Redis.from_url(
                    self.url, socket_connect_timeout=2, health_check_interval=HEALTH_CHECK_INTERVAL
                )
                pubsub = client.pubsub()
                await pubsub.subscribe(self.channel)
                while not self._stop.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=False, timeout=POLL_TIMEOUT)
                    if msg is None:
                        continue
                    if msg["type"] == "subscribe":
                        self._subscriptions += 1
                        self._ready.set()
                        backoff = BACKOFF_START  # a confirmed subscription is a healthy connection
                        if self._subscriptions > 1 and self.on_reconnect:
                            await self._call(self.on_reconnect, what="on_reconnect")
                    elif msg["type"] == "message":
                        payload = _decode(self.channel, msg["data"])
                        if payload is not None:
                            await self._call(self.on_message, payload, what="on_message")
                            backoff = BACKOFF_START
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                log.warning("subscriber %s disconnected (%s); retry in %.1fs", self.channel, exc, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_CAP)
            finally:
                for closer in (pubsub and pubsub.aclose, client and client.aclose):
                    if closer:
                        try:
                            await closer()
                        except Exception:  # noqa: BLE001
                            pass


class ThreadedChannelSubscriber:
    def __init__(self, url, channel, on_message, on_reconnect=None):
        self.url = url
        self.channel = channel
        self.on_message = on_message
        self.on_reconnect = on_reconnect
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._subscriptions = 0
        self._thread = threading.Thread(target=self._run, name=f"sub-{channel}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def _call(self, fn, *args, what: str) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("%s callback failed on %s", what, self.channel)

    def _run(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            client = pubsub = None
            try:
                client = redis.Redis.from_url(
                    self.url, socket_connect_timeout=2, health_check_interval=HEALTH_CHECK_INTERVAL
                )
                pubsub = client.pubsub()
                pubsub.subscribe(self.channel)
                while not self._stop.is_set():
                    msg = pubsub.get_message(ignore_subscribe_messages=False, timeout=POLL_TIMEOUT)
                    if msg is None:
                        continue
                    if msg["type"] == "subscribe":
                        self._subscriptions += 1
                        self._ready.set()
                        backoff = BACKOFF_START  # a confirmed subscription is a healthy connection
                        if self._subscriptions > 1 and self.on_reconnect:
                            self._call(self.on_reconnect, what="on_reconnect")
                    elif msg["type"] == "message":
                        payload = _decode(self.channel, msg["data"])
                        if payload is not None:
                            self._call(self.on_message, payload, what="on_message")
                            backoff = BACKOFF_START
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                log.warning("subscriber %s disconnected (%s); retry in %.1fs", self.channel, exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_CAP)
            finally:
                for obj in (pubsub, client):
                    if obj is not None:
                        try:
                            obj.close()
                        except Exception:  # noqa: BLE001
                            pass
