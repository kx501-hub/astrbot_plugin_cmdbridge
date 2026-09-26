# -*- coding: utf-8 -*-
"""将白名单插件的 AstrBot 指令桥接为 LLM 函数工具。"""

from __future__ import annotations

import functools
import inspect
import json
import re
import types
import typing
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import docstring_parser

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain
from astrbot.core.pipeline.context_utils import call_handler
from astrbot.core.provider.func_tool_manager import PY_TO_JSON_TYPE
from astrbot.core.star.command_management import (
    _collect_descriptors,
    _determine_permission,
)
from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star import StarMetadata, star_map
from .page_api import BridgePageAPI

_FALLBACK_QUERY_PARAM = "query"
_OVERRIDE_DESC_KEYS = frozenset({"description", "desc"})
_OVERRIDE_RESERVED_KEYS = _OVERRIDE_DESC_KEYS | frozenset({"params"})
_MISSING = object()

_DEFAULT_EXCLUDES = {
    "help",
    "stop",
    "reset",
    "new",
    "sid",
    "stats",
    "plugin",
    "name",
    "cmdbridge",
}


_PLUGIN_NAME_PREFIX = "astrbot_plugin_"


def _slug_tool_part(text: str) -> str:
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"[^a-zA-Z0-9_]", "_", text)
    text = re.sub(r"_+", "_", text).strip("_").lower()
    return text


def _plugin_tool_prefix(plugin_meta: StarMetadata | None) -> str:
    if not plugin_meta or not plugin_meta.name:
        return "plugin"
    name = plugin_meta.name.strip()
    if name.lower().startswith(_PLUGIN_NAME_PREFIX):
        name = name[len(_PLUGIN_NAME_PREFIX) :]
    slug = _slug_tool_part(name)
    return slug or "plugin"


def _make_tool_name(
    plugin_meta: StarMetadata | None,
    effective_command: str,
) -> str:
    prefix = _plugin_tool_prefix(plugin_meta)
    cmd_part = _slug_tool_part(effective_command.replace(" ", "_")) or "cmd"
    return f"{prefix}_{cmd_part}"


def _unwrap_handler(handler: Callable[..., Any]) -> Callable[..., Any]:
    if isinstance(handler, functools.partial):
        return handler.func  # type: ignore[return-value]
    return handler


def _is_type_annotation(value: Any) -> bool:
    if value is GreedyStr or value is inspect.Parameter.empty:
        return True
    if isinstance(value, type):
        return True
    return typing.get_origin(value) is not None


def _signature_command_params(handler: Callable[..., Any]) -> list[inspect.Parameter]:
    sig = inspect.signature(handler)
    params = list(sig.parameters.values())
    for index, param in enumerate(params):
        if param.name == "event":
            return params[index + 1 :]
    if params and params[0].name in ("self", "cls"):
        return params[2:]
    return params[1:] if params else []


def _collect_command_param_specs(
    handler: Callable[..., Any],
    command_filter: CommandFilter | None,
) -> tuple[list[tuple[str, Any, bool]], bool]:
    """返回 (参数列表, 是否将 query 注入 message_str)。

    每项为 (参数名, 类型或默认值, 是否为默认值)。
    """
    specs: list[tuple[str, Any, bool]] = []

    if isinstance(command_filter, CommandFilter) and command_filter.handler_params:
        for name, type_or_default in command_filter.handler_params.items():
            is_default = not _is_type_annotation(type_or_default)
            specs.append((name, type_or_default, is_default))
    else:
        for param in _signature_command_params(handler):
            is_default = param.default is not inspect.Parameter.empty
            if is_default:
                specs.append((param.name, param.default, True))
            else:
                specs.append((param.name, param.annotation, False))

    if specs:
        return specs, False

    specs = [(_FALLBACK_QUERY_PARAM, GreedyStr, False)]
    return specs, True


def _resolve_json_type(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return "string"
    if annotation is GreedyStr:
        return "string"
    if isinstance(annotation, type):
        return PY_TO_JSON_TYPE.get(annotation.__name__, "string")
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _resolve_json_type(args[0])
    return "string"


def _normalize_override_cfg(
    override_cfg: dict[str, Any],
) -> tuple[Any, dict[str, str]]:
    """解析 overrides 条目，支持只写 params 或顶层参数名。

    Returns:
        (description, param_overrides)
        description 为 _MISSING 表示未配置，沿用指令 docstring。
    """
    param_overrides: dict[str, str] = {}
    nested = override_cfg.get("params")
    if isinstance(nested, dict):
        param_overrides.update(
            {str(k): str(v) for k, v in nested.items() if v is not None}
        )

    for key, value in override_cfg.items():
        if key in _OVERRIDE_RESERVED_KEYS or value is None:
            continue
        if isinstance(value, str):
            param_overrides[str(key)] = value

    if "description" in override_cfg:
        return override_cfg.get("description"), param_overrides
    if "desc" in override_cfg:
        return override_cfg.get("desc"), param_overrides
    return _MISSING, param_overrides


def _build_param_schema(
    handler: Callable[..., Any],
    command_filter: CommandFilter | None,
    *,
    param_overrides: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    param_overrides = param_overrides or {}
    source_handler = _unwrap_handler(handler)
    doc = docstring_parser.parse(source_handler.__doc__ or "")
    doc_params = {p.arg_name: p for p in doc.params}

    param_specs, inject_query = _collect_command_param_specs(handler, command_filter)
    func_args: list[dict[str, Any]] = []

    for name, type_or_default, is_default in param_specs:
        if type_or_default is GreedyStr:
            json_type = "string"
        elif _is_type_annotation(type_or_default):
            json_type = _resolve_json_type(type_or_default)
        else:
            json_type = _resolve_json_type(type(type_or_default))

        desc = param_overrides.get(name, "").strip()
        if not desc and name in doc_params:
            desc = (doc_params[name].description or "").strip()

        entry: dict[str, Any] = {
            "type": json_type,
            "name": name,
        }
        if desc:
            entry["description"] = desc
        func_args.append(entry)

    return func_args, inject_query


def _build_tool_description(
    handler: Callable[..., Any],
    effective_command: str,
    plugin_meta: StarMetadata | None,
    *,
    override: Any = _MISSING,
) -> str:
    if override is not _MISSING:
        text = str(override or "").strip()
        if text:
            return text

    doc = docstring_parser.parse(_unwrap_handler(handler).__doc__ or "")
    desc = (doc.short_description or doc.description or "").strip()
    if desc:
        return desc

    plugin_label = ""
    if plugin_meta:
        plugin_label = (plugin_meta.display_name or plugin_meta.name or "").strip()
    if plugin_label:
        return f"执行指令 {effective_command}，来自插件「{plugin_label}」。"
    return f"执行指令 {effective_command}。"


def _extract_result_text(event: AstrMessageEvent) -> str | None:
    result = event.get_result()
    if result is None or not result.chain:
        return None

    parts: list[str] = []
    for comp in result.chain:
        if isinstance(comp, Plain):
            parts.append(comp.text)
        elif hasattr(comp, "text"):
            parts.append(str(comp.text))
    text = "\n".join(parts).strip()
    return text or None


def _make_bridge_handler(
    original_handler: Callable[..., Any],
    effective_command: str,
    *,
    inject_query_into_message: bool,
) -> Callable[..., Awaitable[str]]:
    async def bridge(event: AstrMessageEvent, **kwargs: Any) -> str:
        event._result = None
        call_kwargs = dict(kwargs)

        if inject_query_into_message:
            query = str(call_kwargs.pop(_FALLBACK_QUERY_PARAM, "") or "").strip()
            if query:
                event.message_str = f"{effective_command} {query}".strip()
            call_kwargs = {}
        else:
            call_kwargs = {
                key: value
                for key, value in call_kwargs.items()
                if value is not None
            }

        try:
            async for _ in call_handler(event, original_handler, **call_kwargs):
                pass
        except Exception as exc:
            logger.exception("CmdBridge: 指令执行失败")
            return f"指令执行失败: {exc}"

        text = _extract_result_text(event)
        if text:
            return text

        result = event.get_result()
        if result and result.chain:
            return "指令已执行，结果已发送给用户（可能包含图片等非文本内容）。"
        return "指令已执行。"

    bridge.__name__ = (
        f"cmdbridge_{getattr(_unwrap_handler(original_handler), '__name__', 'handler')}"
    )
    return bridge


def _is_excluded_command(
    effective_command: str,
    handler_name: str,
    exclude_commands: set[str],
) -> bool:
    cmd = effective_command.strip().lower()
    fragment = effective_command.split()[-1].lower() if effective_command else ""
    handler = handler_name.strip().lower()
    excludes = {x.strip().lower() for x in exclude_commands if x.strip()}
    return cmd in excludes or fragment in excludes or handler in excludes


def _parse_bridge_settings(config: AstrBotConfig) -> tuple[set[str], set[str], dict[str, Any]]:
    raw_plugins = config.get("plugins") or []
    whitelist = {str(x).strip() for x in raw_plugins if str(x).strip()}

    raw_excludes = config.get("exclude_commands")
    if raw_excludes is None:
        exclude_commands = set(_DEFAULT_EXCLUDES)
    else:
        exclude_commands = {str(x).strip() for x in raw_excludes if str(x).strip()}

    raw_overrides = config.get("overrides") or "{}"
    overrides: dict[str, Any] = {}
    if isinstance(raw_overrides, dict):
        overrides = raw_overrides
    elif isinstance(raw_overrides, str) and raw_overrides.strip():
        try:
            parsed = json.loads(raw_overrides)
            if isinstance(parsed, dict):
                overrides = parsed
        except json.JSONDecodeError:
            logger.warning("CmdBridge: overrides 不是合法 JSON，已忽略")

    return whitelist, exclude_commands, overrides


def _command_fragment(effective_command: str) -> str:
    return effective_command.split()[-1].strip() if effective_command else ""


def _count_bare_override_keys(targets: Iterable[dict[str, str]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for target in targets:
        for key in (
            target["effective"],
            target["fragment"],
            target["handler_name"],
        ):
            if key:
                counts[key] += 1
    return counts


def _resolve_override_entry(
    overrides: dict[str, Any],
    *,
    tool_name: str,
    effective: str,
    plugin_name: str,
    handler_name: str,
    bare_key_counts: Counter[str],
) -> dict[str, Any]:
    """按优先级为单个桥接工具解析 overrides 条目（非 JSON 书写顺序）。"""
    fragment = _command_fragment(effective)
    prioritized = (
        tool_name,
        f"{plugin_name}:{effective}",
        f"{plugin_name}:{fragment}" if fragment else "",
        f"{plugin_name}:{handler_name}",
    )
    for key in prioritized:
        if not key:
            continue
        cfg = overrides.get(key)
        if isinstance(cfg, dict):
            logger.debug(
                "CmdBridge: overrides 命中 %r -> %s (%s)",
                key,
                tool_name,
                plugin_name,
            )
            return cfg

    for key in (fragment, effective, handler_name):
        if not key:
            continue
        cfg = overrides.get(key)
        if not isinstance(cfg, dict):
            continue
        if bare_key_counts.get(key, 0) == 1:
            logger.debug(
                "CmdBridge: overrides 命中 %r -> %s (%s)",
                key,
                tool_name,
                plugin_name,
            )
            return cfg
        if bare_key_counts.get(key, 0) > 1:
            logger.warning(
                "CmdBridge: overrides[%r] 对应 %s 个桥接工具，已跳过；"
                "请改用 %r 或 %r",
                key,
                bare_key_counts[key],
                tool_name,
                f"{plugin_name}:{fragment or effective}",
            )
    return {}


def _bridge_target(
    desc: Any,
    *,
    whitelist: set[str],
    exclude_commands: set[str],
) -> dict[str, str] | None:
    if not CmdBridgeStar._should_bridge_descriptor_static(
        desc,
        whitelist=whitelist,
        exclude_commands=exclude_commands,
    ):
        return None

    plugin_meta = star_map.get(desc.module_path)
    effective = (desc.effective_command or desc.handler_name or "").strip()
    return {
        "tool_name": _make_tool_name(plugin_meta, effective),
        "effective": effective,
        "fragment": _command_fragment(effective),
        "plugin_name": desc.plugin_name,
        "handler_name": desc.handler_name,
    }


_astrbot_loaded = False


class CmdBridgeStar(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._registered_tools: set[str] = set()
        self._registered_by_plugin: dict[str, set[str]] = {}
        self._page_api = BridgePageAPI(self)

    def _unregister_tools(self, tool_names: set[str]) -> None:
        if not tool_names:
            return
        llm_tools = self.context.provider_manager.llm_tools
        for name in tool_names:
            llm_tools.remove_func(name)
            self._registered_tools.discard(name)

    def _unregister_plugin(self, plugin_name: str) -> None:
        tool_names = self._registered_by_plugin.pop(plugin_name, set())
        self._unregister_tools(set(tool_names))

    def _unregister_all(self) -> None:
        self._unregister_tools(set(self._registered_tools))
        self._registered_by_plugin.clear()

    def _should_bridge_descriptor(
        self,
        desc: Any,
        *,
        whitelist: set[str],
        exclude_commands: set[str],
    ) -> bool:
        return self._should_bridge_descriptor_static(
            desc,
            whitelist=whitelist,
            exclude_commands=exclude_commands,
        )

    @staticmethod
    def _should_bridge_descriptor_static(
        desc: Any,
        *,
        whitelist: set[str],
        exclude_commands: set[str],
    ) -> bool:
        return not CmdBridgeStar._skip_reason(desc, whitelist, exclude_commands)

    @staticmethod
    def _skip_reason(desc, whitelist, exclude_commands) -> str:
        if desc.is_group or isinstance(desc.filter_ref, CommandGroupFilter):
            return "指令组入口"
        if not desc.enabled:
            return "指令已禁用"
        plugin_meta = star_map.get(desc.module_path)
        if plugin_meta and not plugin_meta.activated:
            return "插件未启用"
        if _determine_permission(desc.handler) == "admin":
            return "管理员指令，自动排除"
        if desc.plugin_name not in whitelist:
            return "插件未加入白名单"
        effective = (desc.effective_command or desc.handler_name or "").strip()
        if not effective:
            return "指令名为空"
        if _is_excluded_command(effective, desc.handler_name, exclude_commands):
            return "命中排除项"
        return ""

    def describe_bridges(self) -> dict:
        """Read inventory without registering tools or executing any command."""
        whitelist, excludes, overrides = _parse_bridge_settings(self.config)
        targets = self._collect_bridge_targets(whitelist=whitelist, exclude_commands=excludes)
        counts = _count_bare_override_keys(targets)
        rows = []
        plugins = {
            meta.name: {"name": meta.name, "title": meta.display_name or meta.name,
                        "enabled": meta.activated}
            for meta in star_map.values() if meta.name
        }
        for name in whitelist:
            plugins.setdefault(name, {"name": name, "title": name, "enabled": False})
        for desc in _collect_descriptors(include_sub_commands=True):
            meta = star_map.get(desc.module_path)
            effective = (desc.effective_command or desc.handler_name or "").strip()
            name = _make_tool_name(meta, effective)
            reason = self._skip_reason(desc, whitelist, excludes)
            row = {
                "plugin": desc.plugin_name, "command": effective, "tool": name,
                "reason": reason,
                "registered": not reason and name in self._registered_by_plugin.get(desc.plugin_name, set()),
                "description": "", "params": [],
            }
            if not desc.is_group and not isinstance(desc.filter_ref, CommandGroupFilter):
                try:
                    entry = _resolve_override_entry(
                        overrides, tool_name=name, effective=effective,
                        plugin_name=desc.plugin_name, handler_name=desc.handler_name,
                        bare_key_counts=counts,
                    )
                    description, params = _normalize_override_cfg(entry)
                    handler = desc.handler.handler
                    row["description"] = _build_tool_description(handler, effective, meta, override=description)
                    row["params"], _ = _build_param_schema(
                        handler, desc.filter_ref if isinstance(desc.filter_ref, CommandFilter) else None,
                        param_overrides=params,
                    )
                except Exception:
                    row["inspection_error"] = "参数信息读取失败，请查看后台日志"
                    logger.exception("CmdBridge: 无法读取指令 %s", effective)
            rows.append(row)
        return {"plugins": sorted(plugins.values(), key=lambda p: p["name"]),
                "commands": rows, "registered_count": len(self._registered_tools)}

    def _register_bridge(
        self,
        desc: Any,
        *,
        overrides: dict[str, Any],
        bare_key_counts: Counter[str],
    ) -> str | None:
        plugin_meta = star_map.get(desc.module_path)
        effective = (desc.effective_command or desc.handler_name or "").strip()
        tool_name = _make_tool_name(plugin_meta, effective)

        override_cfg = _resolve_override_entry(
            overrides,
            tool_name=tool_name,
            effective=effective,
            plugin_name=desc.plugin_name,
            handler_name=desc.handler_name,
            bare_key_counts=bare_key_counts,
        )

        description_override, param_overrides = _normalize_override_cfg(override_cfg)

        command_filter = (
            desc.filter_ref if isinstance(desc.filter_ref, CommandFilter) else None
        )
        handler = desc.handler.handler
        func_args, inject_query = _build_param_schema(
            handler,
            command_filter,
            param_overrides=param_overrides,
        )
        description = _build_tool_description(
            handler,
            effective,
            plugin_meta,
            override=description_override,
        )
        bridge_handler = _make_bridge_handler(
            handler,
            effective,
            inject_query_into_message=inject_query,
        )

        llm_tools = self.context.provider_manager.llm_tools
        llm_tools.remove_func(tool_name)
        tool = llm_tools.spec_to_func(
            tool_name,
            func_args,
            description,
            bridge_handler,
        )
        tool.handler_module_path = desc.module_path
        llm_tools.func_list.append(tool)

        self._registered_tools.add(tool_name)
        plugin_tools = self._registered_by_plugin.setdefault(desc.plugin_name, set())
        plugin_tools.add(tool_name)
        return tool_name

    def _collect_bridge_targets(
        self,
        *,
        whitelist: set[str],
        exclude_commands: set[str],
        plugin_name: str | None = None,
    ) -> list[dict[str, str]]:
        targets: list[dict[str, str]] = []
        for desc in _collect_descriptors(include_sub_commands=True):
            if plugin_name and desc.plugin_name != plugin_name:
                continue
            if desc.plugin_name not in whitelist:
                continue
            target = _bridge_target(
                desc,
                whitelist=whitelist,
                exclude_commands=exclude_commands,
            )
            if target:
                targets.append(target)
        return targets

    async def _bridge_plugin(self, plugin_name: str) -> int:
        whitelist, exclude_commands, overrides = _parse_bridge_settings(self.config)
        if plugin_name not in whitelist:
            return 0

        self._unregister_plugin(plugin_name)
        targets = self._collect_bridge_targets(
            whitelist=whitelist,
            exclude_commands=exclude_commands,
            plugin_name=plugin_name,
        )
        bare_key_counts = _count_bare_override_keys(targets)

        bridged = 0
        for desc in _collect_descriptors(include_sub_commands=True):
            if desc.plugin_name != plugin_name:
                continue
            if not self._should_bridge_descriptor(
                desc,
                whitelist=whitelist,
                exclude_commands=exclude_commands,
            ):
                continue
            if self._register_bridge(
                desc,
                overrides=overrides,
                bare_key_counts=bare_key_counts,
            ):
                bridged += 1

        if bridged:
            logger.info(
                "CmdBridge: 插件 %s 已桥接 %s 个工具",
                plugin_name,
                bridged,
            )
        return bridged

    async def _refresh_bridges(self) -> None:
        whitelist, exclude_commands, overrides = _parse_bridge_settings(self.config)

        self._unregister_all()
        if not whitelist:
            logger.info("CmdBridge: 白名单为空，未注册任何桥接工具")
            return

        targets = self._collect_bridge_targets(
            whitelist=whitelist,
            exclude_commands=exclude_commands,
        )
        bare_key_counts = _count_bare_override_keys(targets)

        bridged = 0
        skipped = 0
        for desc in _collect_descriptors(include_sub_commands=True):
            if desc.plugin_name not in whitelist:
                skipped += 1
                continue
            if not self._should_bridge_descriptor(
                desc,
                whitelist=whitelist,
                exclude_commands=exclude_commands,
            ):
                skipped += 1
                continue
            if self._register_bridge(
                desc,
                overrides=overrides,
                bare_key_counts=bare_key_counts,
            ):
                bridged += 1

        logger.info(
            "CmdBridge: 全量桥接完成，注册 %s 个工具，跳过 %s 个指令",
            bridged,
            skipped,
        )

    async def initialize(self) -> None:
        await self._refresh_bridges()

    async def terminate(self) -> None:
        self._unregister_all()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        global _astrbot_loaded
        if not _astrbot_loaded:
            await self._refresh_bridges()
        _astrbot_loaded = True

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata: StarMetadata) -> None:
        if not _astrbot_loaded:
            return

        plugin_name = (metadata.name or "").strip()
        if not plugin_name or plugin_name == (self.name or "").strip():
            return

        whitelist, _, _ = _parse_bridge_settings(self.config)
        if plugin_name in whitelist:
            await self._bridge_plugin(plugin_name)

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self, metadata: StarMetadata) -> None:
        if not _astrbot_loaded:
            return

        plugin_name = (metadata.name or "").strip()
        if not plugin_name or plugin_name not in self._registered_by_plugin:
            return

        count = len(self._registered_by_plugin.get(plugin_name, ()))
        self._unregister_plugin(plugin_name)
        if count:
            logger.info("CmdBridge: 插件 %s 已移除 %s 个桥接工具", plugin_name, count)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cmdbridge")
    async def cmdbridge_reload(self, event: AstrMessageEvent) -> None:
        """重新加载插件配置并刷新桥接工具"""
        await self._refresh_bridges()
        event.set_result(
            event.plain_result(
                f"CmdBridge 已刷新，当前桥接 {len(self._registered_tools)} 个工具。\n"
                "可在 WebUI 插件配置中编辑白名单与排除项。",
            ),
        )
