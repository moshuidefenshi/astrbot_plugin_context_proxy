"""brain_client 单元测试：验证「任何失败都降级为空串」。

不依赖 astrbot，可直接运行::

    cd astrbot_plugin_context_proxy
    python -m unittest discover -s tests -v

兼容 Python 3.8+（用到 unittest.IsolatedAsyncioTestCase，3.8 起可用）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from brain_client import BrainClient  # noqa: E402


class FakeResponse:
    def __init__(self, text):
        self.completion_text = text


class RecordingLogger:
    def __init__(self):
        self.entries = []

    def _record(self, level, message):
        self.entries.append((level, message))

    def debug(self, message):
        self._record("debug", message)

    def info(self, message):
        self._record("info", message)

    def warning(self, message):
        self._record("warning", message)

    def error(self, message):
        self._record("error", message)

    def levels(self):
        return [level for level, _ in self.entries]


class Recorder:
    """记录调用参数的假 generate。"""

    def __init__(self, result=None, exc=None, delay=0.0):
        self.calls = []
        self._result = result
        self._exc = exc
        self._delay = delay

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return self._result


class DigestSuccessTest(unittest.IsolatedAsyncioTestCase):
    async def test_object_response(self):
        generate = Recorder(FakeResponse("  简报内容  "))
        client = BrainClient(generate, "brain-1")

        result = await client.digest("材料", "整理指令")

        self.assertEqual(result, "简报内容")
        self.assertEqual(len(generate.calls), 1)
        self.assertEqual(generate.calls[0]["chat_provider_id"], "brain-1")
        self.assertEqual(generate.calls[0]["prompt"], "材料")
        self.assertEqual(generate.calls[0]["system_prompt"], "整理指令")

    async def test_string_response(self):
        client = BrainClient(Recorder("纯文本简报"), "brain-1")
        self.assertEqual(await client.digest("p"), "纯文本简报")

    async def test_dict_response(self):
        client = BrainClient(
            Recorder({"completion_text": "字典里的简报"}), "brain-1"
        )
        self.assertEqual(await client.digest("p"), "字典里的简报")

    async def test_system_prompt_optional(self):
        generate = Recorder("ok")
        client = BrainClient(generate, "brain-1")
        await client.digest("p")
        self.assertIsNone(generate.calls[0]["system_prompt"])

    async def test_properties(self):
        client = BrainClient(Recorder("ok"), "brain-1", timeout=3.5)
        self.assertEqual(client.provider_id, "brain-1")
        self.assertEqual(client.timeout, 3.5)


class DigestDegradeTest(unittest.IsolatedAsyncioTestCase):
    """每一个降级分支都必须返回空串且不抛异常。"""

    async def test_blank_provider_id_skips_call(self):
        generate = Recorder("不该被调用")
        logger = RecordingLogger()
        for provider_id in ("", None):
            client = BrainClient(generate, provider_id, logger=logger)
            self.assertEqual(await client.digest("p"), "")
        self.assertEqual(generate.calls, [])
        self.assertEqual(logger.levels(), ["debug", "debug"])

    async def test_timeout(self):
        generate = Recorder("太慢了", delay=5.0)
        logger = RecordingLogger()
        client = BrainClient(generate, "brain-1", timeout=0.02, logger=logger)

        started = time.monotonic()
        result = await client.digest("p")
        elapsed = time.monotonic() - started

        self.assertEqual(result, "")
        self.assertLess(elapsed, 2.0, "超时后应立即返回，而不是等聪明模型跑完")
        self.assertIn("warning", logger.levels())

    async def test_exception_is_swallowed(self):
        logger = RecordingLogger()
        client = BrainClient(
            Recorder(exc=RuntimeError("接口 500")), "brain-1", logger=logger
        )
        self.assertEqual(await client.digest("p"), "")
        self.assertIn("warning", logger.levels())
        self.assertIn("RuntimeError", logger.entries[-1][1])

    async def test_synchronous_exception_is_swallowed(self):
        def broken(**kwargs):
            raise ValueError("连请求都没发出去")

        client = BrainClient(broken, "brain-1")
        self.assertEqual(await client.digest("p"), "")

    async def test_non_awaitable_return_is_swallowed(self):
        def not_a_coroutine(**kwargs):
            return "我不是 awaitable"

        client = BrainClient(not_a_coroutine, "brain-1")
        self.assertEqual(await client.digest("p"), "")

    async def test_none_response(self):
        client = BrainClient(Recorder(None), "brain-1")
        self.assertEqual(await client.digest("p"), "")

    async def test_empty_string_response(self):
        for value in ("", "   ", "\n\t "):
            client = BrainClient(Recorder(FakeResponse(value)), "brain-1")
            self.assertEqual(await client.digest("p"), "", repr(value))

    async def test_response_without_completion_text(self):
        class Opaque:
            pass

        client = BrainClient(Recorder(Opaque()), "brain-1")
        self.assertEqual(await client.digest("p"), "")

    async def test_completion_text_none(self):
        client = BrainClient(Recorder(FakeResponse(None)), "brain-1")
        self.assertEqual(await client.digest("p"), "")

    async def test_cancellation_propagates(self):
        """任务取消不能被当成普通异常吞掉。"""
        client = BrainClient(
            Recorder(exc=asyncio.CancelledError()), "brain-1"
        )
        with self.assertRaises(asyncio.CancelledError):
            await client.digest("p")

    async def test_logger_is_optional(self):
        """logger=None 时静默降级，不应因缺 logger 而崩。"""
        client = BrainClient(Recorder(exc=RuntimeError("x")), "brain-1", logger=None)
        self.assertEqual(await client.digest("p"), "")


class TimeoutConfigurationTest(unittest.IsolatedAsyncioTestCase):
    async def test_none_timeout_disables_wait_for(self):
        client = BrainClient(Recorder("ok", delay=0.05), "brain-1", timeout=None)
        self.assertEqual(await client.digest("p"), "ok")

    async def test_zero_timeout_disables_wait_for(self):
        client = BrainClient(Recorder("ok", delay=0.05), "brain-1", timeout=0)
        self.assertEqual(await client.digest("p"), "ok")

    async def test_generous_timeout_does_not_truncate(self):
        client = BrainClient(Recorder("ok", delay=0.05), "brain-1", timeout=5.0)
        self.assertEqual(await client.digest("p"), "ok")

    async def test_default_timeout(self):
        client = BrainClient(Recorder("ok"), "brain-1")
        self.assertGreater(client.timeout, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
