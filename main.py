import asyncio
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Any
import tempfile
from urllib.parse import urljoin

import httpx
import aiofiles
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.message_components import Image, Reply, Plain


@register("grok-video", "沐沐沐倾", "Grok视频生成插件，支持根据图片和提示词生成视频", "1.0.2")
class GrokVideoPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # API配置
        self.server_url = config.get("server_url", "https://api.x.ai").rstrip('/')
        self.model_id = config.get("model_id", "grok-imagine-0.9")
        self.api_key = config.get("api_key", "")
        self.enabled = config.get("enabled", True)
        
        # 请求配置
        self.timeout_seconds = config.get("timeout_seconds", 180)
        self.max_retry_attempts = config.get("max_retry_attempts", 3)
        
        # 群组控制
        self.group_control_mode = config.get("group_control_mode", "off").lower()
        self.group_list = list(config.get("group_list", []))
        
        # 速率限制
        self.rate_limit_enabled = config.get("rate_limit_enabled", True)
        self.rate_limit_window_seconds = config.get("rate_limit_window_seconds", 3600)
        self.rate_limit_max_calls = config.get("rate_limit_max_calls", 5)
        self._rate_limit_bucket = {}  # group_id -> {"window_start": float, "count": int}
        self._rate_limit_locks = {}  # group_id -> asyncio.Lock() 用于并发安全
        self._processing_tasks = {}  # user_id -> task_id 防止重复触发
        
        # 管理员用户（优化为set提高查询效率）
        self.admin_users = set(str(u) for u in config.get("admin_users", []))

        self.save_video_enabled = config.get("save_video_enabled", False)

        # 使用 AstrBot data 目录保存视频，确保 NapCat 可访问
        try:
            plugin_data_dir = Path(StarTools.get_data_dir("astrbot_plugin_grok_video"))
            self.videos_dir = plugin_data_dir / "videos"
            self.videos_dir.mkdir(parents=True, exist_ok=True)
            self.videos_dir = self.videos_dir.resolve()
        except Exception as e:
            # 如果StarTools不可用，使用插件目录下的videos文件夹
            logger.warning(f"无法使用StarTools数据目录，使用插件目录: {e}")
            self.videos_dir = Path(__file__).parent / "videos"
            self.videos_dir.mkdir(parents=True, exist_ok=True)
            self.videos_dir = self.videos_dir.resolve()
        
        # 构建完整的API URL
        self.api_url = urljoin(self.server_url + "/", "v1/chat/completions")
        
        logger.info(f"Grok视频生成插件已初始化，API地址: {self.api_url}")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查是否为管理员"""
        return str(event.get_sender_id()) in self.admin_users

    async def _check_group_access(self, event: AstrMessageEvent) -> Optional[str]:
        """检查群组访问权限和速率限制（并发安全）"""
        try:
            group_id = None
            try:
                group_id = event.get_group_id()
            except Exception:
                group_id = None

            # 群组白名单/黑名单检查
            if group_id:
                if self.group_control_mode == "whitelist" and group_id not in self.group_list:
                    return "当前群组未被授权使用视频生成功能"
                if self.group_control_mode == "blacklist" and group_id in self.group_list:
                    return "当前群组已被限制使用视频生成功能"

                # 速率限制检查（仅对群组）- 使用异步锁确保并发安全
                if self.rate_limit_enabled:
                    # 获取或创建该群组的锁
                    if group_id not in self._rate_limit_locks:
                        self._rate_limit_locks[group_id] = asyncio.Lock()
                    
                    # 正确使用异步锁保护临界区
                    async with self._rate_limit_locks[group_id]:
                        now = time.time()
                        bucket = self._rate_limit_bucket.get(group_id, {"window_start": now, "count": 0})
                        window_start = bucket.get("window_start", now)
                        count = int(bucket.get("count", 0))
                        
                        # 检查是否需要重置窗口
                        if now - window_start >= self.rate_limit_window_seconds:
                            window_start = now
                            count = 0
                        
                        # 检查是否超过限制
                        if count >= self.rate_limit_max_calls:
                            return f"本群调用已达上限（{self.rate_limit_max_calls}次/{self.rate_limit_window_seconds}秒），请稍后再试"
                        
                        # 原子性更新计数器
                        bucket["window_start"], bucket["count"] = window_start, count + 1
                        self._rate_limit_bucket[group_id] = bucket

        except Exception as e:
            logger.error(f"群组访问检查失败: {e}")
            return None
        
        return None

    async def _extract_images_from_message(self, event: AstrMessageEvent) -> List[str]:
        """按 sora 插件风格提取图片：先 Reply，再当前消息；仅 url/file 两来源"""
        out: List[str] = []
        if not (hasattr(event, 'message_obj') and event.message_obj and hasattr(event.message_obj, 'message')):
            return out

        async def _download_media(url: str) -> Optional[bytes]:
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.get(url, timeout=30)
                    if r.status_code == 200:
                        return r.content
            except Exception:
                return None
            return None

        async def _load_bytes(src: str) -> Optional[bytes]:
            if Path(src).is_file():
                try:
                    async with aiofiles.open(src, 'rb') as f:
                        return await f.read()
                except Exception:
                    return None
            if isinstance(src, str) and src.startswith('http'):
                return await _download_media(src)
            if isinstance(src, str) and src.startswith('base64://'):
                import base64
                return base64.b64decode(src[9:])
            return None

        async def _find(seg_list: List[Any]) -> Optional[bytes]:
            for seg in seg_list:
                if isinstance(seg, Image):
                    if getattr(seg, 'url', None):
                        b = await _load_bytes(seg.url)
                        if b is not None:
                            return b
                    if getattr(seg, 'file', None):
                        b = await _load_bytes(seg.file)
                        if b is not None:
                            return b
            return None

        image_bytes: Optional[bytes] = None
        for seg in event.message_obj.message:
            if isinstance(seg, Reply) and getattr(seg, 'chain', None):
                image_bytes = await _find(seg.chain)
                if image_bytes is not None:
                    break
        if image_bytes is None:
            image_bytes = await _find(event.message_obj.message)

        if image_bytes:
            import base64
            b64 = base64.b64encode(image_bytes).decode('utf-8')
            out.append(f"data:image/png;base64,{b64}")
        return out

    async def _call_grok_api(self, prompt: str, image_base64: str) -> Tuple[Optional[str], Optional[str]]:
        """调用Grok API生成视频"""
        if not self.api_key:
            return None, "未配置API密钥"
        
        # 强制图生视频模式
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_base64
                        }
                    }
                ]
            }
        ]

        # 构建请求数据
        payload = {
            "model": self.model_id,
            "messages": messages,
            "stream": True  # 启用流式输出
        }
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        timeout_config = httpx.Timeout(connect=10.0, read=self.timeout_seconds, write=10.0, pool=self.timeout_seconds)

        for attempt in range(self.max_retry_attempts):
            try:
                logger.info(f"调用Grok API (尝试 {attempt + 1}/{self.max_retry_attempts})")
                logger.debug(f"请求URL: {self.api_url}")
                logger.debug(f"请求模型: {self.model_id}")

                async with httpx.AsyncClient(timeout=timeout_config) as client:
                    # 采用流式SSE读取，兼容 grok2api 的 data: 行格式
                    async with client.stream("POST", self.api_url, json=payload, headers=headers) as resp:
                        status = resp.status_code
                        logger.info(f"API响应状态码: {status}")
                        if status == 403:
                            return None, "API访问被拒绝，请检查密钥和权限"
                        if status != 200:
                            text = await resp.aread()
                            snippet = text.decode(errors="ignore")[:400]
                            return None, f"API请求失败 (状态码: {status}): {snippet}"

                        accumulated = []
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            payload_str = line.split("data:", 1)[1].strip()
                            if payload_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(payload_str)
                            except Exception:
                                # 忽略无法解析的行
                                continue

                            # x.ai/grok 风格：choices[0].delta 或 choices[0].message.content
                            try:
                                if chunk.get("choices"):
                                    c0 = chunk["choices"][0]
                                    if "delta" in c0 and isinstance(c0["delta"], dict):
                                        delta = c0["delta"].get("content")
                                        if isinstance(delta, str):
                                            accumulated.append(delta)
                                    elif "message" in c0 and isinstance(c0["message"], dict):
                                        content = c0["message"].get("content")
                                        if isinstance(content, str):
                                            accumulated.append(content)
                            except Exception:
                                pass

                            # 增量尝试提取 URL
                            content_joined = "".join(accumulated)
                            url = self._try_content_extraction(content_joined)
                            if not url:
                                url = self._try_structured_extraction(chunk)
                            if url:
                                logger.info(f"成功提取到视频URL: {url}")
                                return url, None

                        # 流结束后再做一次提取
                        final_text = "".join(accumulated)
                        url = self._try_content_extraction(final_text)
                        if url:
                            return url, None
                        return None, "API响应中未包含有效的视频URL"

            except httpx.TimeoutException:
                err = f"请求超时 ({self.timeout_seconds}秒)"
                if attempt == self.max_retry_attempts - 1:
                    return None, err
                logger.warning(f"{err}，等待重试...")
                await asyncio.sleep(1)
            except Exception as e:
                err = f"请求异常: {str(e)}"
                if attempt == self.max_retry_attempts - 1:
                    return None, err
                logger.warning(f"{err}，等待重试...")
                await asyncio.sleep(1)
        
        return None, "所有重试均失败"

    def _extract_video_url_from_response(self, response_data: dict) -> Tuple[Optional[str], Optional[str]]:
        """
        从 API 响应中提取视频 URL，采用更健墮的解析策略
        
        返回: (video_url, error_message)
        """
        try:
            # 1. 首先检查响应结构是否符合预期
            if not isinstance(response_data, dict):
                return None, f"无效的响应格式: {type(response_data)}"
            
            if "choices" not in response_data or not response_data["choices"]:
                return None, "API响应中缺少 choices 字段"
            
            # 2. 提取内容
            choice = response_data["choices"][0]
            if not isinstance(choice, dict) or "message" not in choice:
                return None, "choices[0] 缺少 message 字段"
            
            message = choice["message"]
            if not isinstance(message, dict) or "content" not in message:
                return None, "message 缺少 content 字段"
            
            content = message["content"]
            if not isinstance(content, str):
                return None, f"content 不是字符串类型: {type(content)}"
            
            logger.debug(f"API返回内容长度: {len(content)} 字符")
            
            # 3. 优先尝试结构化解析（如果 API 支持）
            video_url = self._try_structured_extraction(response_data)
            if video_url:
                return video_url, None
            
            # 4. 如果结构化解析失败，使用改进的文本解析
            video_url = self._try_content_extraction(content)
            if video_url:
                return video_url, None
            
            # 5. 所有方法都失败
            logger.warning(f"无法从响应中提取视频URL，内容片段: {content[:200]}...")
            return None, f"未能从 API 响应中提取到有效的视频 URL"
            
        except Exception as e:
            logger.error(f"URL 提取过程中发生异常: {e}")
            return None, f"URL 提取失败: {str(e)}"
    
    def _try_structured_extraction(self, response_data: dict) -> Optional[str]:
        """
        尝试从结构化数据中提取 URL（为未来 API 改进做准备）
        """
        try:
            # 检查是否有直接的 video_url 字段
            if "video_url" in response_data:
                url = response_data["video_url"]
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    logger.info("使用结构化 video_url 字段")
                    return url
            
            # 检查 choices[0].message 中是否有结构化数据
            choice = response_data.get("choices", [{}])[0]
            message = choice.get("message", {})
            
            # 检查是否有 attachments 或 media 字段
            for field in ["attachments", "media", "files"]:
                if field in message and isinstance(message[field], list):
                    for item in message[field]:
                        if isinstance(item, dict) and "url" in item:
                            url = item["url"]
                            if isinstance(url, str) and url.endswith(".mp4"):
                                logger.info(f"使用结构化 {field} 字段")
                                return url
            
            return None
            
        except Exception as e:
            logger.debug(f"结构化提取失败: {e}")
            return None
    
    def _try_content_extraction(self, content: str) -> Optional[str]:
        """
        从文本内容中提取 URL，使用改进的策略
        """
        try:
            # 策略 1: 查找最常见的 HTML video 标签
            video_url = self._extract_from_html_tag(content)
            if video_url:
                return video_url
            
            # 策略 2: 查找直接的 .mp4 URL
            video_url = self._extract_direct_url(content)
            if video_url:
                return video_url
            
            # 策略 3: 查找 Markdown 格式链接
            video_url = self._extract_from_markdown(content)
            if video_url:
                return video_url
            
            return None
            
        except Exception as e:
            logger.debug(f"内容提取失败: {e}")
            return None
    
    def _extract_from_html_tag(self, content: str) -> Optional[str]:
        """从 HTML video 标签中提取 URL"""
        if "<video" not in content or "src=" not in content:
            return None
        
        # 更宽松的正则，支持多种引号和空格
        patterns = [
            r'<video[^>]*src=["\']([^"\'>]+)["\'][^>]*>',  # 标准 video 标签
            r'src=["\']([^"\'>]+\.mp4[^"\'>]*)["\']',      # 任意 src 属性
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                url = match.group(1)
                if self._is_valid_video_url(url):
                    logger.debug(f"从 HTML 标签提取到 URL: {url}")
                    return url
        
        return None
    
    def _extract_direct_url(self, content: str) -> Optional[str]:
        """提取直接的 .mp4 URL"""
        # 更精确的 URL 正则，避免误匹配
        pattern = r'(https?://[^\s<>"\')\]\}]+\.mp4(?:\?[^\s<>"\')\]\}]*)?)'
        
        matches = re.findall(pattern, content, re.IGNORECASE)
        for url in matches:
            if self._is_valid_video_url(url):
                logger.debug(f"提取到直接 URL: {url}")
                return url
        
        return None
    
    def _extract_from_markdown(self, content: str) -> Optional[str]:
        """从 Markdown 链接中提取 URL"""
        # Markdown 格式: [text](url) 或 ![alt](url)
        patterns = [
            r'!?\[[^\]]*\]\(([^\)]+\.mp4[^\)]*)\)',  # Markdown 链接
            r'!?\[[^\]]*\]:\s*([^\s]+\.mp4[^\s]*)',   # Markdown 引用式链接
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                url = match.group(1)
                if self._is_valid_video_url(url):
                    logger.debug(f"从 Markdown 提取到 URL: {url}")
                    return url
        
        return None
    
    def _is_valid_video_url(self, url: str) -> bool:
        """验证 URL 是否为有效的视频 URL"""
        if not isinstance(url, str) or len(url) < 10:
            return False
        
        # 检查协议
        if not url.startswith(("http://", "https://")):
            return False
        
        # 检查文件扩展名
        if not url.lower().endswith(".mp4") and ".mp4" not in url.lower():
            return False
        
        # 检查是否包含明显的非法字符
        invalid_chars = ['<', '>', '"', "'", '\n', '\r', '\t']
        if any(char in url for char in invalid_chars):
            return False
        
        return True

    async def _download_video(self, video_url: str) -> Optional[str]:
        """下载视频到本地"""
        try:
            filename = f"grok_video_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.mp4"
            file_path = self.videos_dir / filename
            
            timeout_config = httpx.Timeout(
                connect=10.0,
                read=300.0,  # 视频文件可能较大，给更长的读取时间
                write=10.0,
                pool=300.0
            )
            
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                response = await client.get(video_url)
                response.raise_for_status()
                
                # 保存视频文件
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                
                # 确保返回绝对路径，避免路径问题
                absolute_path = file_path.resolve()
                logger.info(f"视频已保存到: {absolute_path}")
                return str(absolute_path)
        
        except Exception as e:
            logger.error(f"下载视频失败: {e}")
            return None

    async def _cleanup_video_file(self, video_path: Optional[str]):
        """删除临时视频缓存（按照配置可选）"""
        if not video_path:
            return
        if self.save_video_enabled:
            return
        try:
            path = Path(video_path)
            if path.exists():
                path.unlink()
                logger.debug(f"已清理本地视频缓存: {path}")
        except Exception as e:
            logger.warning(f"清理视频文件失败: {e}")

    async def _create_video_component(self, video_path: Optional[str], video_url: Optional[str]):
        """根据配置构建最终 Video 组件，优先使用URL发送（适合Docker部署）"""
        from astrbot.api.message_components import Video

        # Docker部署下优先使用远程URL（避免文件系统共享问题）
        if video_url:
            logger.info(f"使用远程视频URL发送: {video_url}")
            return Video.fromURL(video_url)
        
        # 如果没有远程URL，且用户配置了保存，尝试本地文件
        if video_path and self.save_video_enabled:
            logger.warning(f"Docker部署下使用本地文件可能失败: {video_path}")
            return Video.fromFileSystem(path=video_path)

        raise ValueError("缺少可用的视频URL，无法发送")

    async def _generate_video_core(self, event: AstrMessageEvent, prompt: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """核心视频生成逻辑"""
        # 检查功能是否启用
        if not self.enabled:
            return None, None, "视频生成功能已禁用"
        
        # 提取图片
        images = await self._extract_images_from_message(event)
        
        # 使用第一张图片（如果有）
        image_base64 = images[0] if images else None
        
        # Grok Imagine 0.9 仅支持图生视频
        if not image_base64:
            return None, None, "请发送图片或引用图片进行视频生成。"
        
        # 记录生成模式，模仿Sora插件的日志风格
        logger.info(f"图生视频 - 用户: {event.get_sender_id()}, 提示词: {prompt[:20]}...")

        # 调用API生成视频
        video_url, error_msg = await self._call_grok_api(prompt, image_base64)
        if error_msg:
            return None, None, error_msg

        if not video_url:
            return None, None, "API未返回视频URL"

        # Docker部署下优先使用URL，不下载本地文件避免文件系统问题
        local_path = None
        if self.save_video_enabled:
            logger.info("用户配置了保存，但Docker部署下建议使用URL发送")
            # 可选下载，但不强制
            try:
                local_path = await self._download_video(video_url)
                if local_path:
                    logger.info(f"视频已下载到: {local_path}")
            except Exception as e:
                logger.warning(f"视频下载失败，将使用URL发送: {e}")

        return video_url, local_path, None

    async def _async_generate_video(self, event: AstrMessageEvent, prompt: str, task_id: str):
        """异步视频生成，避免超时和重复触发"""
        user_id = str(event.get_sender_id())
        try:
            logger.info(f"开始处理用户 {user_id} 的视频生成任务: {task_id}")
            
            video_url, video_path, error_msg = await self._generate_video_core(event, prompt)
            
            if error_msg:
                await event.send(event.plain_result(f"❌ {error_msg}"))
                return
            
            if video_url or video_path:
                try:
                    video_component = await self._create_video_component(video_path, video_url)
                    
                    # 使用更长的超时时间，但提供更好的反馈
                    try:
                        await asyncio.wait_for(
                            event.send(event.chain_result([video_component])),
                            timeout=90.0  # 增加到90秒超时
                        )
                        logger.info(f"用户 {user_id} 的视频发送成功")
                        
                    except asyncio.TimeoutError:
                        logger.warning(f"用户 {user_id} 的视频发送超时，但可能仍在传输")
                        await event.send(event.plain_result(
                            "⚠️ 视频发送超时，但可能仍在传输中。\n"
                            "如果稍后收到视频，说明发送成功。"
                        ))
                    
                    # 清理文件（如果配置允许）
                    if video_path:
                        await self._cleanup_video_file(video_path)
                        
                except Exception as e:
                    # 区分WebSocket超时和真正的错误
                    if "WebSocket API call timeout" in str(e):
                        logger.warning(f"用户 {user_id} 的视频发送WebSocket超时: {e}")
                        await event.send(event.plain_result(
                            "⚠️ 视频发送超时，但可能仍在传输中。\n"
                            "如果稍后收到视频，说明发送成功。"
                        ))
                    else:
                        logger.error(f"用户 {user_id} 的视频发送真正失败: {e}")
                        await event.send(event.plain_result(f"❌ 视频发送失败: {str(e)}"))
            else:
                await event.send(event.plain_result("❌ 视频生成失败，请稍后再试"))
        
        except Exception as e:
            logger.error(f"用户 {user_id} 的异步视频生成异常: {e}")
            await event.send(event.plain_result(f"❌ 视频生成时遇到问题: {str(e)}"))
        
        finally:
            # 清理任务记录
            if user_id in self._processing_tasks and self._processing_tasks[user_id] == task_id:
                del self._processing_tasks[user_id]
                logger.info(f"用户 {user_id} 的任务 {task_id} 已完成")

    # 移除LLM工具函数，因为grok不需要函数调用功能

    @filter.command("grok")
    async def cmd_generate_video(self, event: AstrMessageEvent, *, prompt: str):
        """生成视频：/grok <提示词>（可选图片）"""
        # 群组访问检查
        access_error = await self._check_group_access(event)
        if access_error:
            yield event.plain_result(access_error)
            return
        
        # 防止重复触发检查
        user_id = str(event.get_sender_id())
        if user_id in self._processing_tasks:
            yield event.plain_result(f"⚠️ 您已有一个视频生成任务在进行中，请等待完成后再试。")
            return
        
        # 检查是否包含图片 (仅用于反馈消息)
        images = await self._extract_images_from_message(event)
        
        if not images:
            yield event.plain_result("❌ 视频生成需要您在消息中包含图片。请上传图片后再试。")
            return

        try:
            # 生成任务ID并记录
            task_id = str(uuid.uuid4())[:8]
            self._processing_tasks[user_id] = task_id
            
            # 反馈消息
            yield event.plain_result(f"🎬 收到指令，正在进行 [图生视频] ...")
            
            # 启动异步任务避免超时
            asyncio.create_task(self._async_generate_video(event, prompt, task_id))
        
        except Exception as e:
            logger.error(f"视频生成命令异常: {e}")
            yield event.plain_result(f"❌ 生成视频时遇到问题: {str(e)}")

    @filter.command("grok测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """测试Grok API连接（管理员专用）"""
        if not self._is_admin(event):
            yield event.plain_result("此命令仅限管理员使用")
            return
        
        try:
            # 使用更整洁的纯文本排版
            status_icon = "✅" if self.enabled else "❌"
            key_status = "✅ 已配置" if self.api_key else "❌ 未配置"
            
            lines = [
                "🔍 Grok视频生成插件测试结果",
                "------------------------------",
                f"{status_icon} 功能状态: {'已启用' if self.enabled else '已禁用'}",
                f"🔑 API密钥: {key_status}",
                f"📡 API地址: {self.api_url}",
                f"🤖 模型ID: {self.model_id}",
                f"⏱️ 超时设置: {self.timeout_seconds}秒",
                f"🔄 最大重试: {self.max_retry_attempts}次",
                f"📁 存储目录: {self.videos_dir}",
                "------------------------------"
            ]
            
            yield event.plain_result("\n".join(lines))
        
        except Exception as e:
            logger.error(f"测试命令异常: {e}")
            yield event.plain_result(f"❌ 测试失败: {str(e)}")

    @filter.command("grok帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """帮助信息"""
        help_text = (
            "🎬 Grok视频生成插件帮助\n\n"
            "指令：/grok <提示词>\n\n"
            "模式支持：\n"
            "• 图生视频：必须发送图片并带上指令，或引用图片发送指令\n\n"
            "示例：\n"
            "• /grok 让画面动起来 (需带图/引用图)\n\n"
            "管理员命令：\n"
            "• /grok测试 - 测试API连接\n"
            "• /grok帮助 - 显示此帮助信息\n\n"
            "注意：视频生成需要较长时间，请耐心等待"
        )
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件卸载时调用"""
        # 清理速率限制锁
        self._rate_limit_locks.clear()
        logger.info("Grok视频生成插件已卸载")
