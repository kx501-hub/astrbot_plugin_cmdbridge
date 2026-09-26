"""Visual configuration and runtime inventory for CmdBridge Pages."""

from copy import deepcopy
import hashlib
import json

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


def revision(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_settings(body):
    if not isinstance(body, dict):
        raise ValueError("配置必须是对象")
    result = {}
    for key in ("plugins", "exclude_commands"):
        values = body.get(key)
        if not isinstance(values, list) or any(not isinstance(x, str) or not x.strip() for x in values):
            raise ValueError(f"{key} 必须是非空文本列表")
        result[key] = list(dict.fromkeys(x.strip() for x in values))
    overrides = body.get("overrides")
    if isinstance(overrides, str):
        try:
            overrides = json.loads(overrides)
        except ValueError as exc:
            raise ValueError("覆盖项不是有效 JSON") from exc
    if not isinstance(overrides, dict):
        raise ValueError("覆盖项必须是 JSON 对象")
    for key, value in overrides.items():
        if not key.strip() or not isinstance(value, dict):
            raise ValueError("每个覆盖项必须是对象，并具有工具或指令名称")
        for name, text in value.items():
            if name == "params":
                if not isinstance(text, dict) or any(not isinstance(x, str) for x in text.values()):
                    raise ValueError("参数说明必须是参数名到文本的映射")
            elif not isinstance(text, str):
                raise ValueError("工具和参数说明必须是文本")
    result["overrides"] = deepcopy(overrides)
    return result


class BridgePageAPI:
    def __init__(self, plugin):
        self.plugin = plugin
        for route, handler, method in (
            ("settings", self.settings, "GET"),
            ("settings/save", self.save, "POST"),
            ("refresh", self.refresh, "POST"),
        ):
            plugin.context.register_web_api(
                f"/astrbot_plugin_cmdbridge/{route}", handler, [method], f"CmdBridge {route}"
            )

    def document(self):
        from .main import _DEFAULT_EXCLUDES
        config = self.plugin.config
        return deepcopy({
            "plugins": config.get("plugins", []),
            "exclude_commands": config.get("exclude_commands", sorted(_DEFAULT_EXCLUDES)),
            "overrides": config.get("overrides", "{}"),
        })

    async def settings(self):
        try:
            document = self.document()
            return json_response({
                "settings": document, "revision": revision(document),
                **self.plugin.describe_bridges(),
            })
        except Exception:
            logger.exception("CmdBridge: 页面读取失败")
            return error_response("无法读取指令目录，请查看后台日志", status_code=500)

    async def save(self):
        try:
            body = await request.json(default=None)
            if not isinstance(body, dict):
                raise ValueError("请求必须是对象")
            document = validate_settings(body.get("settings"))
            if body.get("revision") != revision(self.document()):
                return error_response("配置已变更，请重新载入后再保存", status_code=409)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        source = self.plugin.config
        if not callable(getattr(source, "save_config", None)):
            return error_response("当前运行环境不支持保存插件配置", status_code=503)
        previous = deepcopy(dict(source))
        document["overrides"] = json.dumps(document["overrides"], ensure_ascii=False, indent=2)
        try:
            source.update(document)
            source.save_config()
        except Exception:
            source.clear()
            source.update(previous)
            logger.exception("CmdBridge: 保存配置失败")
            return error_response("保存失败，原配置已保留，请查看后台日志", status_code=500)
        try:
            await self.plugin._refresh_bridges()
        except Exception:
            logger.exception("CmdBridge: 配置已保存，但刷新失败")
            return error_response("配置已保存，但刷新工具失败，请重载插件并查看日志", status_code=500)
        return await self.settings()

    async def refresh(self):
        try:
            await self.plugin._refresh_bridges()
        except Exception:
            logger.exception("CmdBridge: 刷新失败")
            return error_response("刷新工具失败，请查看后台日志", status_code=500)
        return await self.settings()
