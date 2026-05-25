# 读空气 (KuukiYomi) - AstrBot 智能群聊感知插件

让 AI 学会读空气：智能缓存对话，自主判断是否回复，工具调用跨群交互。

## ✨ 功能

### 1. 对话缓存
- 群聊 / 私聊独立缓存，大小分别可配（最大 1000 条）
- FIFO 滚动覆盖：缓存满时自动淘汰最早的消息
- 定期持久化到磁盘，重启不丢失
- Bot 自己发的消息也会被缓存

### 2. 读空气
- 每条群消息按配置概率触发 LLM 判断
- 被 @、被提名、命中关键词时 **直接触发**（穿透概率和冷却）
- LLM 自行决定是否回复：输出 `<NO_RESPONSE>` 则沉默
- 同群冷却机制，防止刷屏

### 3. LLM 工具调用（需 AstrBot Agent 模式）
- **主动发消息** — LLM 可以主动给指定群/人发消息
- **读取缓存** — 对话中提到其他群/人时，读取对应缓存了解上下文
- **转发聊天记录** — 将某群的最近记录摘要转发到另一个群

## 🚀 安装

1. 确保已部署 [AstrBot](https://github.com/AstrBotDevs/AstrBot)
2. 在插件管理中添加仓库链接：
   ```
   https://github.com/chenluQwQ/astrbot_plugin_KuukiYomi
   ```
3. 进入插件配置页面，填写需要启用的群号等设置

## ⚙️ 配置说明

| 配置组 | 关键项 | 说明 |
|--------|--------|------|
| 缓存设置 | `group_cache_size` | 每群缓存上限（默认 200） |
| 缓存设置 | `private_cache_size` | 每私聊缓存上限（默认 100） |
| 读空气 | `trigger_probability` | 触发概率 0~1（默认 0.15） |
| 读空气 | `keywords` | 触发关键词列表 |
| 读空气 | `cooldown_seconds` | 同群冷却秒数（默认 30） |
| 群聊过滤 | `enabled_groups` | 白名单群号 |
| 工具设置 | `allowed_send_targets` | 允许主动发送的目标列表 |

## 📋 命令

使用 `/kuukiyomi`、`/ky` 或 `/读空气` 作为前缀：

| 命令 | 说明 |
|------|------|
| `/ky help` | 显示帮助 |
| `/ky status` | 查看缓存状态 |
| `/ky history [N]` | 查看最近 N 条缓存 |
| `/ky reset` | 清空当前会话缓存 |
| `/ky reset <群号>` | 清空指定群缓存 |

## ⚠️ 注意

- 使用本插件时建议 **关闭 AstrBot 自带的主动回复功能**，避免冲突
- LLM 工具功能需要 AstrBot 开启 **Agent 模式**
- `allowed_send_targets` 为空时 LLM 可以向任意目标发消息，建议配置白名单

## 📄 许可证

AGPL-3.0
