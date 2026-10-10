"""astrbot_plugin_context_proxy 插件包（astrbot上下文代理）。

AstrBot 会以 ``data.plugins.astrbot_plugin_context_proxy.main`` 的形式导入插件，
因此这里用相对导入拆分模块：``main`` 负责接线，``context_broker`` / ``brain_client``
是不依赖 astrbot 的纯逻辑层（可脱离 AstrBot 单测）。

⚠️ 本项目是 Vibe Coding 产物（AI 辅助、边聊边写），详见 README。
"""
