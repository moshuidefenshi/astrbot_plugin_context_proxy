"""AstrBot 上下文代理插件。

把「已经整理过的历史」交给聪明模型重新整理成一份结构化简报，再把简报 + 最近
几轮原文交给当前会话原本的执行模型（笨模型），让弱模型不被又长又杂的上下文干扰。

⚠️ 重要机制说明（决定了本插件的形态）
--------------------------------------------------------------------------
AstrBot 在 ``astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py``
里把 ``agent_runner.run_context.messages`` **整体写回**会话历史
（``_save_to_history`` → ``conversation_manager.update_conversation``），
而 ``run_context.messages`` 正是 ``reset()`` 从 ``req.contexts`` 拍出来的列表。
也就是说：**模型看到的历史** 和 **被持久化的历史** 是同一份。

插件没有任何「只给模型看、不落库」的通道：``extra_user_content_parts`` 只能
往本轮用户消息后面**追加**，无法删减历史；``system_prompt`` 虽不落库，但同样
无法删减历史。因此本插件采用与 AstrBot 内置 ``llm_compress`` 策略相同的语义：
**持久化压缩** —— 简报会被写进会话历史，并在下一轮被识别为「已整理过的历史」，
从而实现增量摘要、自我重复不会发生。

用户可见的后果：被折叠掉的早期原文**不会**回到会话历史里（与内置压缩策略一致）。
README 里有醒目说明。
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from astrbot.api import AstrBotConfig, logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from . import context_broker as cb
from .brain_client import BrainClient

__all__ = ["ContextBrokerPlugin"]

KV_SESSION_PREFIX = "session:"
"""会话级开关的 KV key 前缀；值是 bool，键不存在表示「跟随默认」。"""

_TRUE_STRINGS = {"1", "true", "yes", "y", "on", "是", "开启", "开"}

_SHOW_MAX_CHARS = 2000
"""``/上下文代理 查看`` 单条消息展示的简报上限。"""


# --------------------------------------------------------------------------- #
# 配置值容错（用户可能手改 JSON，类型不一定对）
# --------------------------------------------------------------------------- #

def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return default


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


@dataclass
class _Settings:
    """一轮请求用到的配置快照。"""

    enable: bool = True
    brain_provider_id: str = ""
    mode: str = "digest"
    keep_recent_turns: int = 4
    min_history_messages: int = 4
    trigger_min_tokens: int = 0
    brain_timeout: float = 8.0
    brain_input_max_chars: int = 24000
    max_digest_chars: int = 1500
    carry_forward_brief: bool = True
    include_standalone: bool = True
    instruction: str = cb.DEFAULT_BRIEF_INSTRUCTION
    exclude_sessions: List[str] = field(default_factory=list)
    debug_log: bool = False
    preserve_leading_system: bool = True
    system_context_mode: str = "keep"
    sanitize_output: bool = True
    per_message_max_chars: int = cb.DEFAULT_PER_MESSAGE_MAX_CHARS


class ContextBrokerPlugin(Star):
    """用聪明模型整理上下文，再把干净的上下文交给执行模型。"""

    def __init__(
        self,
        context: Context,
        config: Optional[AstrBotConfig] = None,
    ) -> None:
        super().__init__(context)
        self.config: Any = config if config is not None else {}
        self._brain: Optional[BrainClient] = None
        self._brain_signature: Optional[Tuple[Any, ...]] = None

    # ----------------------------------------------------------------- #
    # 小工具
    # ----------------------------------------------------------------- #

    @property
    def _logger(self) -> Any:
        """插件专用 logger；极端情况下回退到全局 logger。"""
        return getattr(self, "logger", None) or astrbot_logger

    def _debug(self, settings: _Settings, message: str) -> None:
        text = "[上下文代理] " + message
        if settings.debug_log:
            self._logger.info(text)
        else:
            self._logger.debug(text)

    def _read_settings(self) -> _Settings:
        cfg = self.config if hasattr(self.config, "get") else {}

        def raw(key: str, default: Any = None) -> Any:
            try:
                value = cfg.get(key, default)
            except Exception:  # noqa: BLE001 - 配置异常不该弄坏请求
                return default
            return default if value is None else value

        mode = str(raw("mode", "digest")).strip().lower()
        if mode not in ("digest", "rewrite", "shadow"):
            mode = "digest"

        return _Settings(
            enable=_as_bool(raw("enable", True), True),
            brain_provider_id=str(raw("brain_provider_id", "") or "").strip(),
            mode=mode,
            keep_recent_turns=max(0, _as_int(raw("keep_recent_turns", 4), 4)),
            min_history_messages=max(0, _as_int(raw("min_history_messages", 4), 4)),
            trigger_min_tokens=max(0, _as_int(raw("trigger_min_tokens", 0), 0)),
            brain_timeout=max(0.0, _as_float(raw("brain_timeout_sec", 8.0), 8.0)),
            brain_input_max_chars=max(
                0, _as_int(raw("brain_input_max_chars", 24000), 24000)
            ),
            max_digest_chars=max(0, _as_int(raw("max_digest_chars", 1500), 1500)),
            carry_forward_brief=_as_bool(raw("carry_forward_brief", True), True),
            include_standalone=_as_bool(
                raw("include_standalone_question", True), True
            ),
            instruction=cb.resolve_instruction(raw("brief_instruction", "")),
            exclude_sessions=_as_str_list(raw("exclude_sessions", [])),
            debug_log=_as_bool(raw("debug_log", False), False),
            preserve_leading_system=_as_bool(
                raw("preserve_leading_system", True), True
            ),
            system_context_mode=cb.coerce_system_context_mode(
                raw("system_context_mode", cb.DEFAULT_SYSTEM_CONTEXT_MODE)
            ),
            sanitize_output=_as_bool(raw("sanitize_output", True), True),
        )

    @staticmethod
    def _is_excluded(umo: str, prefixes: Sequence[str]) -> bool:
        if not umo:
            return False
        return any(umo.startswith(prefix) for prefix in prefixes)

    @staticmethod
    def _normalise_history(contexts: Any) -> Optional[List[Dict[str, Any]]]:
        """把 ``req.contexts`` 规整成 ``list[dict]``，不可用时返回 None。"""
        if isinstance(contexts, str):
            try:
                contexts = json.loads(contexts)
            except Exception:  # noqa: BLE001
                return None
        if not isinstance(contexts, list) or not contexts:
            return None
        messages = [item for item in contexts if isinstance(item, dict)]
        return messages or None

    @staticmethod
    def _strip_system(
        messages: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """去掉 role=system 的消息（digest/drop 下执行模型不该再看到它们）。"""
        return [
            message
            for message in messages
            if not (isinstance(message, dict) and message.get("role") == "system")
        ]

    def _apply_system_context_mode(
        self,
        settings: _Settings,
        req: ProviderRequest,
        brief: str,
        system_material: str,
    ) -> bool:
        """按配置决定是否从执行模型侧移除系统上下文。

        Returns:
            是否真的移除了。返回 ``False`` 时调用方**不得**再过滤 system 消息，
            否则会丢掉没有交给聪明模型备份的内容。
        """
        mode = settings.system_context_mode
        if mode == "keep":
            return False

        if (
            mode == "digest"
            and system_material
            and cb.SYSTEM_CONTEXT_HEADING not in brief
        ):
            self._logger.warning(
                "[上下文代理] digest 模式下简报里没有「{}」一节，"
                "为避免丢失系统设定，本轮不清空 system_prompt。"
                "（自定义了 brief_instruction 时请在输出格式里保留该小节）".format(
                    cb.SYSTEM_CONTEXT_HEADING
                )
            )
            return False

        req.system_prompt = ""
        self._debug(
            settings,
            "{} 模式：已移除请求里的系统上下文（原文 {} 字符）。".format(
                mode, len(system_material)
            ),
        )
        return True

    def _brain_client(self, settings: _Settings) -> BrainClient:
        """按配置签名缓存 BrainClient，配置改了自动重建。"""
        signature = (settings.brain_provider_id, settings.brain_timeout)
        if self._brain is None or self._brain_signature != signature:
            self._brain = BrainClient(
                self.context.llm_generate,
                settings.brain_provider_id,
                timeout=settings.brain_timeout,
                logger=self._logger,
            )
            self._brain_signature = signature
        return self._brain

    def _brain_model_label(self, provider_id: str) -> str:
        try:
            provider = self.context.get_provider_by_id(provider_id)
            model = provider.get_model() if provider is not None else None
            if model:
                return "{} ({})".format(model, provider_id)
        except Exception:  # noqa: BLE001
            pass
        return provider_id or "（未配置）"

    # ----------------------------------------------------------------- #
    # 会话开关（KV，三态）
    # ----------------------------------------------------------------- #

    async def _get_session_override(self, umo: str) -> Optional[bool]:
        try:
            value = await self.get_kv_data(KV_SESSION_PREFIX + umo, None)
        except Exception:  # noqa: BLE001
            self._logger.warning(
                "[上下文代理] 读取会话开关失败，按「跟随默认」处理。\n"
                + traceback.format_exc()
            )
            return None
        if value is True or value is False:
            return value
        return None

    async def _set_session_override(self, umo: str, value: Optional[bool]) -> None:
        key = KV_SESSION_PREFIX + umo
        if value is None:
            await self.delete_kv_data(key)
        else:
            await self.put_kv_data(key, value)

    async def _load_conversation(
        self, umo: str
    ) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
        """读取当前会话的对话 id 与已持久化的历史。"""
        try:
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return None, None
            conversation = await manager.get_conversation(umo, cid)
            if conversation is None:
                return cid, None
            history = json.loads(conversation.history or "[]")
            if not isinstance(history, list):
                return cid, None
            return cid, history
        except Exception:  # noqa: BLE001
            self._logger.warning(
                "[上下文代理] 读取会话历史失败。\n" + traceback.format_exc()
            )
            return None, None

    # ----------------------------------------------------------------- #
    # 生命周期
    # ----------------------------------------------------------------- #

    async def initialize(self) -> None:
        settings = self._read_settings()

        if not settings.brain_provider_id:
            self._logger.warning(
                "[上下文代理] 尚未配置「聪明模型」(brain_provider_id)，插件不会生效。"
                "请在 WebUI 的插件配置里选择一个模型。"
            )
        else:
            provider = None
            try:
                provider = self.context.get_provider_by_id(settings.brain_provider_id)
            except Exception:  # noqa: BLE001
                provider = None
            if provider is None:
                self._logger.error(
                    "[上下文代理] 找不到 provider id = {!r} 的模型；"
                    "请检查 WebUI 的模型配置，或重新在插件配置里选择。".format(
                        settings.brain_provider_id
                    )
                )
            else:
                self._logger.info(
                    "[上下文代理] 已启用；聪明模型 = {}；模式 = {}；最近保留 {} 轮原文。".format(
                        self._brain_model_label(settings.brain_provider_id),
                        settings.mode,
                        settings.keep_recent_turns,
                    )
                )

        self._warn_if_builtin_llm_compression_enabled()

    def _warn_if_builtin_llm_compression_enabled(self) -> None:
        """内置 llm_compress 与本插件都会改写会话历史，同时开启会互相覆盖。"""
        try:
            config = self.context.get_config()
            runner = config.get("agent_runner", {}) or {}
            compression = (runner.get("config", {}) or {}).get("compression", {}) or {}
            strategy = str(compression.get("context_limit_reached_strategy", "") or "")
        except Exception:  # noqa: BLE001
            return

        if strategy == "llm_compress":
            self._logger.warning(
                "[上下文代理] 检测到 AstrBot 内置的上下文压缩策略也是 llm_compress。"
                "两者都会改写会话历史，可能互相覆盖并多花一次模型调用。建议到 "
                "WebUI「AI 配置 → 高级 → 上下文管理策略」把它关掉，"
                "只由本插件管理上下文。"
            )

    # ----------------------------------------------------------------- #
    # 核心钩子
    # ----------------------------------------------------------------- #

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """在请求发出前，把已整理的历史替换成新鲜简报 + 最近若干轮原文。"""
        try:
            await self._process_request(event, req)
        except asyncio.CancelledError:
            # 任务取消不能吞，否则会破坏 asyncio 语义。
            raise
        except BaseException:  # noqa: BLE001 - 任何问题都只允许「少优化」
            self._logger.error(
                "[上下文代理] 处理失败，本轮已原样透传，不影响正常回复。\n"
                + traceback.format_exc()
            )

    async def _process_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        settings = self._read_settings()
        umo = event.unified_msg_origin

        excluded = self._is_excluded(umo, settings.exclude_sessions)
        override = await self._get_session_override(umo)
        enabled, reason = cb.resolve_effective_state(
            global_enable=settings.enable,
            excluded=excluded,
            session_override=override,
        )
        if not enabled:
            self._debug(settings, "本会话未启用（{}），跳过。".format(reason))
            return

        history = self._normalise_history(req.contexts)
        if not history:
            self._debug(settings, "没有可用的会话历史，跳过。")
            return

        # 识别历史里是否已经有我们上一轮注入的简报。
        previous_brief, brief_index = cb.extract_existing_brief(history)
        if brief_index >= 0:
            # 简报之前的内容（前导 system、人格开场白等）原样保留，但丢掉旧简报本身。
            preserved = list(history[:brief_index])
            delta = list(history[brief_index + 2 :])
        else:
            previous_brief = None
            preserved = cb.split_leading_system(history)[0]
            # 前导 system 已归入 preserved，必须从 delta 里剔除，否则会重复出现。
            delta = list(history[len(preserved) :])

        # 系统上下文：keep 下原样保留；digest/drop 下由本插件接管。
        # 夹在历史中间的 system 消息也要算进来——稍后会把它们一并移除。
        system_material = cb.resolve_system_material(
            system_prompt=req.system_prompt,
            system_messages=[
                message
                for message in list(preserved) + list(delta)
                if isinstance(message, dict) and message.get("role") == "system"
            ],
        )

        if not settings.preserve_leading_system:
            preserved = self._strip_system(preserved)

        # 正常对话里每轮都会新增 2 条消息，所以 delta 一般非空。
        # 这里只在「简报之后确实什么都没有」时短路（例如请求被重放）。
        if not delta and brief_index >= 0:
            self._debug(settings, "简报之后没有新内容，原样保留已有形态。")
            return

        ctx_tokens = 0
        if settings.trigger_min_tokens > 0:
            ctx_tokens = cb.estimate_tokens(cb.serialize_history(delta))

        engage, why = cb.should_engage(
            history_len=len(delta),
            ctx_tokens=ctx_tokens,
            min_history_messages=settings.min_history_messages,
            trigger_min_tokens=settings.trigger_min_tokens,
        )
        if not engage:
            self._debug(settings, "暂不整理（{}）。".format(why))
            return

        delta_text = cb.serialize_history(
            delta, per_message_max_chars=settings.per_message_max_chars
        )
        if (
            settings.brain_input_max_chars > 0
            and len(delta_text) > settings.brain_input_max_chars
        ):
            delta_text = (
                "（更早的新增内容已省略，请结合上一份简报）\n"
                + delta_text[-settings.brain_input_max_chars :]
            )

        carry_previous = previous_brief if settings.carry_forward_brief else None
        prompt = cb.build_brain_prompt(
            previous_brief=carry_previous,
            delta_text=delta_text,
            current_prompt=req.prompt or "",
            max_chars=settings.max_digest_chars,
            system_text=(
                system_material if settings.system_context_mode == "digest" else ""
            ),
        )

        started = time.monotonic()
        raw = await self._brain_client(settings).digest(prompt, settings.instruction)
        elapsed = time.monotonic() - started

        if not raw:
            self._debug(
                settings, "聪明模型没有返回可用内容（{:.2f}s），本轮原样透传。".format(elapsed)
            )
            return

        brief, standalone = cb.parse_brain_output(raw)
        if not brief:
            self._debug(settings, "简报解析结果为空，本轮原样透传。")
            return

        if settings.sanitize_output:
            brief = cb.sanitize_brief(brief)
        brief = cb.clamp(
            brief, settings.max_digest_chars, suffix="\n[简报已截断]"
        )
        if not brief:
            self._debug(settings, "简报净化后为空，本轮原样透传。")
            return

        if settings.include_standalone and standalone:
            if settings.sanitize_output:
                standalone = cb.sanitize_brief(standalone)
            standalone = cb.clamp(standalone, 600, suffix="…[已截断]").strip() or None
        else:
            standalone = None

        if settings.mode == "shadow":
            self._logger.info(
                "[上下文代理][shadow] 本会话简报（{} 字，{:.2f}s，未改动请求）：\n{}".format(
                    len(brief), elapsed, brief
                )
            )
            return

        recent = cb.tail_rounds(delta, settings.keep_recent_turns)
        if self._apply_system_context_mode(settings, req, brief, system_material):
            preserved = self._strip_system(preserved)
            recent = self._strip_system(recent)

        req.contexts = cb.build_final_contexts(
            prefix=preserved,
            brief=brief,
            recent=recent,
        )

        if settings.mode == "rewrite" and standalone:
            original = (req.prompt or "").strip()
            req.prompt = (
                "{}\n\n[用户原话] {}".format(standalone, original)
                if original
                else standalone
            )

        self._debug(
            settings,
            "已整理：新增 {} 条历史 → 简报 {} 字 + 最近 {} 条原文（{:.2f}s）。".format(
                len(delta), len(brief), len(recent), elapsed
            ),
        )

    # ----------------------------------------------------------------- #
    # 聊天指令
    # ----------------------------------------------------------------- #

    @filter.command_group("上下文代理", alias={"ctxproxy", "ctx"})
    def ctx_proxy(self, event: Optional[AstrMessageEvent] = None) -> None:
        """上下文代理：聪明模型管上下文，笨模型只管回答。"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ctx_proxy.command("开启", alias={"打开", "on"})
    async def ctx_enable(self, event: AstrMessageEvent):
        """在本会话开启上下文代理。"""
        yield event.plain_result(await self._cmd_set_enabled(event, True))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ctx_proxy.command("关闭", alias={"关掉", "off"})
    async def ctx_disable(self, event: AstrMessageEvent):
        """在本会话关闭上下文代理。"""
        yield event.plain_result(await self._cmd_set_enabled(event, False))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ctx_proxy.command("跟随默认", alias={"默认", "reset"})
    async def ctx_follow_default(self, event: AstrMessageEvent):
        """清除本会话的手动开关，恢复为跟随默认。"""
        umo = event.unified_msg_origin
        settings = self._read_settings()
        try:
            await self._set_session_override(umo, None)
        except Exception:  # noqa: BLE001
            self._logger.error(
                "[上下文代理] 清除会话开关失败。\n" + traceback.format_exc()
            )
            yield event.plain_result("❌ 清除会话开关失败，请查看 AstrBot 日志。")
            return

        state, reason = cb.resolve_effective_state(
            global_enable=settings.enable,
            excluded=self._is_excluded(umo, settings.exclude_sessions),
            session_override=None,
        )
        yield event.plain_result(
            "↩️ 已清除本会话的手动开关，恢复为默认。\n"
            "当前：{}（{}）".format(
                "已开启" if state else "已关闭", cb.describe_state_reason(reason)
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @ctx_proxy.command("重建", alias={"rebuild"})
    async def ctx_rebuild(self, event: AstrMessageEvent):
        """移除本会话的简报，让下一轮重新整理。"""
        umo = event.unified_msg_origin
        cid, history = await self._load_conversation(umo)
        if not history:
            yield event.plain_result("本会话没有对话历史，无需重建。")
            return

        brief, brief_index = cb.extract_existing_brief(history)
        if brief_index < 0:
            yield event.plain_result("本会话当前没有简报，无需重建。")
            return

        new_history = list(history[:brief_index]) + list(history[brief_index + 2 :])
        try:
            await self.context.conversation_manager.update_conversation(
                umo, conversation_id=cid, history=new_history
            )
        except Exception:  # noqa: BLE001
            self._logger.error(
                "[上下文代理] 重建简报失败。\n" + traceback.format_exc()
            )
            yield event.plain_result("❌ 重建失败，请查看 AstrBot 日志。")
            return

        yield event.plain_result(
            "🗑️ 已移除本会话的简报，下一轮对话会重新整理。\n"
            "注意：简报里已经折叠掉的早期对话内容无法恢复。"
        )

    @ctx_proxy.command("状态", alias={"status"})
    async def ctx_status(self, event: AstrMessageEvent):
        """查看本会话的上下文代理状态。"""
        yield event.plain_result(await self._cmd_status(event))

    @ctx_proxy.command("查看", alias={"show"})
    async def ctx_show(self, event: AstrMessageEvent):
        """查看本会话当前的上下文简报。"""
        yield event.plain_result(await self._cmd_show(event))

    # ----------------------------------------------------------------- #
    # 指令实现
    # ----------------------------------------------------------------- #

    async def _cmd_set_enabled(self, event: AstrMessageEvent, enabled: bool) -> str:
        settings = self._read_settings()
        umo = event.unified_msg_origin

        if enabled:
            allowed, why = cb.can_override(
                global_enable=settings.enable,
                excluded=self._is_excluded(umo, settings.exclude_sessions),
            )
            if not allowed:
                if why == "global_disabled":
                    return (
                        "⚠️ 插件总开关在 WebUI 配置中是关闭的，本会话无法开启"
                        "（未写入设置）。"
                    )
                return "⚠️ 本会话在插件 exclude_sessions 黑名单中，无法开启（未写入设置）。"

        current = await self._get_session_override(umo)
        if current is enabled:
            return "ℹ️ 本会话已经是{}状态，无需重复操作。".format(
                "开启" if enabled else "关闭"
            )

        try:
            await self._set_session_override(umo, enabled)
        except Exception:  # noqa: BLE001
            self._logger.error(
                "[上下文代理] 写入会话开关失败。\n" + traceback.format_exc()
            )
            return "❌ 写入会话开关失败，请查看 AstrBot 日志。"

        if not enabled:
            return (
                "✅ 已在本会话关闭「上下文代理」，后续对话直接使用完整上下文。\n"
                "发送 /上下文代理 开启 恢复。"
            )

        if not settings.brain_provider_id:
            return (
                "⚠️ 已记录开启状态，但尚未配置有效的「聪明模型」"
                "(brain_provider_id)，代理不会生效。请在 WebUI 插件配置里选择模型。"
            )

        return (
            "✅ 已在本会话开启「上下文代理」。\n"
            "聪明模型：{}；发送 /上下文代理 关闭 可关闭。".format(
                self._brain_model_label(settings.brain_provider_id)
            )
        )

    async def _cmd_status(self, event: AstrMessageEvent) -> str:
        settings = self._read_settings()
        umo = event.unified_msg_origin
        excluded = self._is_excluded(umo, settings.exclude_sessions)
        override = await self._get_session_override(umo)
        state, reason = cb.resolve_effective_state(
            global_enable=settings.enable,
            excluded=excluded,
            session_override=override,
        )

        lines = [
            "上下文代理 · 本会话状态",
            "总开关：{}".format("已开启" if settings.enable else "已关闭"),
            "黑名单命中：{}".format("是" if excluded else "否"),
            "会话手动开关：{}".format(
                {None: "未设置", True: "已开启", False: "已关闭"}[override]
            ),
            "最终生效：{}（{}）".format(
                "是" if state else "否", cb.describe_state_reason(reason)
            ),
            "运行模式：{}".format(settings.mode),
            "系统上下文：{}".format(settings.system_context_mode),
            "聪明模型：{}".format(
                self._brain_model_label(settings.brain_provider_id)
                if settings.brain_provider_id
                else "（未配置）"
            ),
        ]

        _cid, history = await self._load_conversation(umo)
        if history:
            brief, _brief_index = cb.extract_existing_brief(history)
            if brief:
                lines.append("当前简报：{} 字".format(len(brief)))
            else:
                lines.append("当前简报：无（历史 {} 条）".format(len(history)))
        else:
            lines.append("当前简报：无（本会话还没有对话）")

        return "\n".join(lines)

    async def _cmd_show(self, event: AstrMessageEvent) -> str:
        _cid, history = await self._load_conversation(event.unified_msg_origin)
        if not history:
            return "本会话还没有对话历史，暂时没有简报可看。"
        brief, _brief_index = cb.extract_existing_brief(history)
        if not brief:
            return "本会话还没有生成过简报。"
        shown = cb.clamp(
            brief, _SHOW_MAX_CHARS, suffix="\n…[简报较长，已截断]"
        )
        return "本会话当前简报：\n\n" + shown


# ``Star`` 的子类会被 AstrBot 自动发现；这里显式再导出一次便于阅读。
Plugin = ContextBrokerPlugin
