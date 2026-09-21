# CmdBridge

将白名单内 AstrBot 插件的**指令**桥接为 **LLM 函数工具**，供 Agent 自动调用。

## 配置

在 WebUI → 扩展 → CmdBridge → 配置：

| 项 | 说明 |
|---|---|
| 白名单插件 | 目标插件 `metadata.yaml` 中的 `name` |
| 排除的指令 | 不桥接的指令名（管理员指令自动排除） |
| 覆盖项 | JSON，可**只覆盖**工具说明或参数描述，未写字段沿用指令 docstring |

保存后自动热重载。

### overrides 示例

完整示例

```json
{
  "bt": {
    "description": "搜索 BitTorrent 磁力链接",
    "params": {
      "query": "要搜索的资源名称或关键词"
    }
  }
}
```

只改参数说明（工具说明仍用指令 docstring）：

```json
{
  "bt": {
    "params": {
      "query": "要搜索的资源名称或关键词"
    }
  }
}
```

等价写法（参数名写在顶层）：

```json
{
  "bt": {
    "query": "要搜索的资源名称或关键词"
  }
}
```

参数说明优先级：`overrides` > docstring 的 `Args`；无说明时不填 description（类型由 schema 的 `type` 提供）。

### overrides 匹配顺序

优先级如下：

1. 桥接后的**工具全名**（WebUI 工具列表里看到的名字，如 `bit_torrent_btp`）
2. `插件name:指令名`（如 `astrbot_plugin_xxx:btp`，白名单里的完整插件名）
3. `插件name:指令末级片段`（子指令时有用）
4. 仅写指令名（如 `btp`）——**仅当当前白名单里只有一个工具使用该指令名时**才会命中；否则会跳过并在日志里警告

## 指令

`/cmdbridge`（管理员）：手动刷新桥接工具。

## 工具命名

`{插件 name 去掉 astrbot_plugin_ 前缀}_{指令名}`，仅 ASCII 小写，例如 `bit_torrent_bt`。非 ASCII 字符会被去掉。
