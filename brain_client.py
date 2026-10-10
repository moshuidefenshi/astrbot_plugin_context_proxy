"""聪明模型（Brain）的调用封装。

这一层只做一件事：**调用绝不能把主流程弄坏**。
超时、抛异常、返回空、返回格式不对——统统返回 ``""``，由调用方原样透传。

刻意**不做重试**：钩子在会话锁内执行，重试只会让用户多等一截；AstrBot 内置的
LLM 压缩器也是同样的取舍（失败即降级）。

本模块不依赖 astrbot：调用函数由外部注入，因此可以直接单测。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

__all__ = ["BrainClient", "DEFAULT_BRAIN_TIMEOUT"]

DEFAULT_BRAIN_TIMEOUT = 8.0

GenerateFn = Callable[..., Awaitable[Any]]


class BrainClient:
    """按 provider_id 调用「聪明模型」，返回纯文本结果。

    Args:
        generate: 形如 ``context.llm_generate`` 的异步可调用对象。
            会被以 ``chat_provider_id=`` / ``prompt=`` / ``system_prompt=`` 调用。
        provider_id: 聪明模型的 provider id；为空时直接放弃（返回 ``""``）。
        timeout: 超时秒数；``None`` 或 <= 0 表示不设超时。
        logger: 可选，需支持 ``.warning(str)`` / ``.debug(str)``。为 ``None`` 时静默。
    """

    def __init__(
        self,
        generate: GenerateFn,
        provider_id: str,
        timeout: Optional[float] = DEFAULT_BRAIN_TIMEOUT,
        logger: Any = None,
    ) -> None:
        self._generate = generate
        self._provider_id = provider_id or ""
        self._timeout = timeout
        self._logger = logger

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def timeout(self) -> Optional[float]:
        return self._timeout

    def _log(self, level: str, message: str) -> None:
        if self._logger is None:
            return
        write = getattr(self._logger, level, None)
        if callable(write):
            write(message)

    async def digest(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
    ) -> str:
        """让聪明模型整理上下文。

        Returns:
            整理结果的纯文本；任何失败都返回空串，**绝不抛异常**。

        Raises:
            asyncio.CancelledError: 原样向上传播，不吞掉任务取消。
        """
        if not self._provider_id:
            self._log("debug", "未配置聪明模型（brain_provider_id 为空），跳过整理。")
            return ""

        try:
            coroutine = self._generate(
                chat_provider_id=self._provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
            if self._timeout is not None and self._timeout > 0:
                response = await asyncio.wait_for(coroutine, timeout=self._timeout)
            else:
                response = await coroutine
        except asyncio.CancelledError:
            # 任务取消必须继续往上抛，否则会破坏 asyncio 语义。
            raise
        except asyncio.TimeoutError:
            self._log(
                "warning",
                "聪明模型调用超时（{}s），本轮原样透传。".format(self._timeout),
            )
            return ""
        except Exception as exc:  # noqa: BLE001 - 这里就是要吞掉一切
            self._log(
                "warning",
                "聪明模型调用失败（{}: {}），本轮原样透传。".format(
                    type(exc).__name__, exc
                ),
            )
            return ""

        text = self._extract_text(response)
        if not text:
            self._log("warning", "聪明模型返回空内容，本轮原样透传。")
            return ""
        return text

    @staticmethod
    def _extract_text(response: Any) -> str:
        """从 LLMResponse / 字符串 / dict 里取出文本，取不到就返回空串。"""
        if response is None:
            return ""
        if isinstance(response, str):
            return response.strip()

        candidate = getattr(response, "completion_text", None)
        if candidate is None and isinstance(response, dict):
            candidate = response.get("completion_text")
        if candidate is None:
            return ""

        try:
            return str(candidate).strip()
        except Exception:  # noqa: BLE001
            return ""
