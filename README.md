# Grok视频生成插件

基于 Grok API 的图生视频插件，支持根据图片和提示词生成短视频，具备稳健的流式解析与完善的发送/日志链路。

## 功能特性
- 🎬 图生视频：必须携带图片与提示词
- 🔀 流式解析：兼容 SSE `data:` 输出，增强 URL 提取（文本/HTML/Markdown/结构化）
- 📤 发送策略：默认使用远程 URL 发送；开启保存时下载到本地并使用本地文件发送
- 🛡️ 访问控制：支持群组白名单/黑名单与小时级速率限制
- 🔄 自动重试：请求异常/超时自动重试，提供用户侧超时提示
- 🪵 详细日志：模型、状态码、提取成功、下载/发送进度与回退提示

## 使用方法
- 指令：`/grok <提示词>`（需带图或引用已有图片）
- 示例：
  ```
  /grok 让画面动起来
  /grok 给角色添加奔跑效果
  /grok 补全人物并动起来
  ```

## 配置说明
- 必需：
  - `server_url`：Grok API 服务地址
  - `model_id`：模型 ID（默认：`grok-imagine-0.9`）
  - `api_key`：API 密钥
- 可选：
  - `timeout_seconds`：请求超时（默认：180）
  - `max_retry_attempts`：最大重试次数（默认：3）
  - `group_control_mode`：群组控制（`off`/`whitelist`/`blacklist`）
  - `rate_limit_enabled`：是否启用限流（默认：true）
  - `rate_limit_window_seconds`：限流时间窗（默认：3600）
  - `rate_limit_max_calls`：时间窗内最大次数（默认：5）
  - `save_video_enabled`：是否在发送前下载本地并使用本地文件发送（默认：false）

## 命令
- `/grok帮助`：显示帮助信息

## 注意事项
- 必须发送或引用图片，否则不生成
- 视频生成与下载较耗时，注意耐心与网络质量
- `save_video_enabled=false` 时发送成功后自动清理缓存；为 true 时保留本地文件
- 容器部署或跨主机环境下，优先使用远程 URL 发送更稳定

## 依赖要求
- httpx >= 0.24.0
- aiofiles >= 23.0.0

## 版本信息
- 版本：1.1.0
- 作者：沐沐沐倾
- 兼容：AstrBot 插件系统
