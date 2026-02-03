"""Grok Imagine 图片生成器 - 使用 WebSocket 直连，支持流式预览和 HTTP 代理"""

import asyncio
import json
import uuid
import time
import base64
import ssl
import re
from typing import Optional, List, Dict, Any, Callable, Awaitable
from dataclasses import dataclass, field

import aiohttp
from aiohttp_socks import ProxyConnector

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    curl_requests = None

from app.core.config import settings
from app.core.logger import logger

# 根据配置选择 SSO 管理器
if settings.REDIS_ENABLED:
    from app.services.redis_sso_manager import create_sso_manager
    sso_manager = create_sso_manager(
        use_redis=True,
        redis_url=settings.REDIS_URL,
        strategy=settings.SSO_ROTATION_STRATEGY,
        daily_limit=settings.SSO_DAILY_LIMIT
    )
else:
    from app.services.sso_manager import sso_manager


@dataclass
class ImageProgress:
    """单张图片的生成进度"""
    image_id: str  # 从 URL 提取的 UUID
    stage: str = "preview"  # preview -> medium -> final
    blob: str = ""
    blob_size: int = 0
    url: str = ""
    is_final: bool = False


@dataclass
class GenerationProgress:
    """整体生成进度"""
    total: int = 4  # 预期生成数量
    images: Dict[str, ImageProgress] = field(default_factory=dict)
    completed: int = 0  # 已完成的最终图片数量
    has_medium: bool = False  # 是否有 medium 阶段的图片

    def get_completed_images(self) -> List[ImageProgress]:
        """获取所有已完成的图片"""
        return [img for img in self.images.values() if img.is_final]

    def check_blocked(self) -> bool:
        """检查是否被 blocked (有 medium 但没有 final)"""
        has_medium = any(img.stage == "medium" for img in self.images.values())
        has_final = any(img.is_final for img in self.images.values())
        return has_medium and not has_final


# 流式回调类型
StreamCallback = Callable[[ImageProgress, GenerationProgress], Awaitable[None]]


class GrokImagineClient:
    """Grok Imagine WebSocket 客户端"""

    def __init__(self):
        self._ssl_context = ssl.create_default_context()
        # 用于从 URL 提取图片 ID
        self._url_pattern = re.compile(r'/images/([a-f0-9-]+)\.(png|jpg)')

    def _get_connector(self) -> Optional[aiohttp.BaseConnector]:
        """获取连接器（支持代理）"""
        proxy_url = settings.PROXY_URL or settings.HTTP_PROXY or settings.HTTPS_PROXY

        if proxy_url:
            logger.info(f"[Grok] 使用代理: {proxy_url}")
            # 支持 http/https/socks4/socks5 代理
            return ProxyConnector.from_url(proxy_url, ssl=self._ssl_context)

        return aiohttp.TCPConnector(ssl=self._ssl_context)

    def _get_ws_headers(self, sso: str) -> Dict[str, str]:
        """构建 WebSocket 请求头"""
        return {
            "Cookie": f"sso={sso}; sso-rw={sso}",
            "Origin": "https://grok.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def _extract_image_id(self, url: str) -> Optional[str]:
        """从 URL 提取图片 ID"""
        match = self._url_pattern.search(url)
        if match:
            return match.group(1)
        return None

    def _is_final_image(self, url: str, blob_size: int) -> bool:
        """判断是否是最终高清图片"""
        # 最终版本是 .jpg 格式，大小通常 > 100KB
        return url.endswith('.jpg') and blob_size > 100000

    async def _verify_age(self, sso: str) -> bool:
        """验证年龄 - 使用 curl_cffi 模拟浏览器请求"""
        if not CURL_CFFI_AVAILABLE:
            logger.warning("[Grok] curl_cffi 未安装，跳过年龄验证")
            return False

        if not settings.CF_CLEARANCE:
            logger.warning("[Grok] CF_CLEARANCE 未配置，跳过年龄验证")
            return False

        cookie_str = f"sso={sso}; sso-rw={sso}; cf_clearance={settings.CF_CLEARANCE}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/",
            "Accept": "*/*",
            "Cookie": cookie_str,
            "Content-Type": "application/json",
        }

        proxy = settings.PROXY_URL or settings.HTTP_PROXY or settings.HTTPS_PROXY

        logger.info("[Grok] 正在进行年龄验证...")

        try:
            # 在线程池中运行同步的 curl_cffi 请求
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: curl_requests.post(
                    "https://grok.com/rest/auth/set-birth-date",
                    headers=headers,
                    json={"birthDate": "2001-01-01T16:00:00.000Z"},
                    impersonate="chrome133a",
                    proxy=proxy,
                    verify=False
                )
            )

            if resp.status_code == 200:
                logger.info(f"[Grok] 年龄验证成功 (状态码: {resp.status_code})")
                return True
            else:
                logger.warning(f"[Grok] 年龄验证响应: {resp.status_code} - {resp.text[:200]}")
                return False

        except Exception as e:
            logger.error(f"[Grok] 年龄验证失败: {e}")
            return False

    async def generate(
        self,
        prompt: str,
        aspect_ratio: str = "2:3",
        n: int = None,
        enable_nsfw: bool = True,
        sso: Optional[str] = None,
        max_retries: int = 5,
        stream_callback: Optional[StreamCallback] = None
    ) -> Dict[str, Any]:
        """
        生成图片

        Args:
            prompt: 提示词
            aspect_ratio: 宽高比 (1:1, 2:3, 3:2)
            n: 生成数量，如果不指定则使用配置的默认值
            enable_nsfw: 是否启用 NSFW
            sso: 指定 SSO，否则从池中获取
            max_retries: 最大重试次数 (用于轮询不同 SSO)
            stream_callback: 流式回调，每次收到图片更新时调用

        Returns:
            生成结果，包含图片 URL 列表
        """
        # 使用配置的默认图片数量
        if n is None:
            n = settings.DEFAULT_IMAGE_COUNT

        logger.info(f"[Grok] 请求生成 {n} 张图片 (DEFAULT_IMAGE_COUNT={settings.DEFAULT_IMAGE_COUNT})")

        last_error = None
        blocked_retries = 0  # blocked 重试计数
        max_blocked_retries = 3  # blocked 最大重试次数

        for attempt in range(max_retries):
            current_sso = sso if sso else await sso_manager.get_next_sso()

            if not current_sso:
                return {"success": False, "error": "没有可用的 SSO"}

            # 检查年龄验证状态
            age_verified = await sso_manager.get_age_verified(current_sso)
            if age_verified == 0:
                logger.info(f"[Grok] SSO {current_sso[:20]}... 未进行年龄验证，开始验证...")
                verify_success = await self._verify_age(current_sso)
                if verify_success:
                    await sso_manager.set_age_verified(current_sso, 1)
                else:
                    logger.warning(f"[Grok] SSO {current_sso[:20]}... 年龄验证失败，继续尝试生成")

            try:
                result = await self._do_generate(
                    sso=current_sso,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    n=n,
                    enable_nsfw=enable_nsfw,
                    stream_callback=stream_callback
                )

                if result.get("success"):
                    await sso_manager.mark_success(current_sso)
                    # 记录使用（Redis 模式下更新统计）
                    if hasattr(sso_manager, 'record_usage'):
                        await sso_manager.record_usage(current_sso)
                    return result

                error_code = result.get("error_code", "")

                # 检查是否被 blocked
                if error_code == "blocked":
                    blocked_retries += 1
                    logger.warning(
                        f"[Grok] 检测到 blocked，重试 {blocked_retries}/{max_blocked_retries}"
                    )
                    await sso_manager.mark_failed(current_sso, "blocked - 无法生成最终图片")

                    if blocked_retries >= max_blocked_retries:
                        return {
                            "success": False,
                            "error_code": "blocked",
                            "error": f"连续 {max_blocked_retries} 次被 blocked，请稍后重试"
                        }
                    # 如果指定了 SSO 则不重试
                    if sso:
                        return result
                    continue

                if error_code in ["rate_limit_exceeded", "unauthorized"]:
                    await sso_manager.mark_failed(current_sso, result.get("error", ""))
                    last_error = result
                    if sso:
                        return result
                    logger.info(f"[Grok] 尝试 {attempt + 1}/{max_retries} 失败，切换 SSO...")
                    continue
                else:
                    return result

            except Exception as e:
                logger.error(f"[Grok] 生成失败: {e}")
                await sso_manager.mark_failed(current_sso, str(e))
                last_error = {"success": False, "error": str(e)}
                if sso:
                    return last_error
                continue

        return last_error or {"success": False, "error": "所有重试都失败了"}

    async def _do_generate(
        self,
        sso: str,
        prompt: str,
        aspect_ratio: str,
        n: int,
        enable_nsfw: bool,
        stream_callback: Optional[StreamCallback] = None
    ) -> Dict[str, Any]:
        """执行生成"""
        request_id = str(uuid.uuid4())
        headers = self._get_ws_headers(sso)

        logger.info(f"[Grok] 连接 WebSocket: {settings.GROK_WS_URL}")

        connector = self._get_connector()

        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(
                    settings.GROK_WS_URL,
                    headers=headers,
                    heartbeat=20,
                    receive_timeout=settings.GENERATION_TIMEOUT
                ) as ws:
                    # 发送生成请求
                    message = {
                        "type": "conversation.item.create",
                        "timestamp": int(time.time() * 1000),
                        "item": {
                            "type": "message",
                            "content": [{
                                "requestId": request_id,
                                "text": prompt,
                                "type": "input_text",
                                "properties": {
                                    "section_count": 0,
                                    "is_kids_mode": False,
                                    "enable_nsfw": enable_nsfw,
                                    "skip_upsampler": False,
                                    "is_initial": False,
                                    "aspect_ratio": aspect_ratio
                                }
                            }]
                        }
                    }

                    await ws.send_json(message)
                    logger.info(f"[Grok] 已发送请求: {prompt[:50]}...")

                    # 进度跟踪
                    progress = GenerationProgress(total=n)
                    error_info = None
                    start_time = time.time()
                    last_activity = time.time()
                    medium_received_time = None  # 收到 medium 的时间

                    while time.time() - start_time < settings.GENERATION_TIMEOUT:
                        try:
                            ws_msg = await asyncio.wait_for(ws.receive(), timeout=5.0)

                            if ws_msg.type == aiohttp.WSMsgType.TEXT:
                                last_activity = time.time()
                                msg = json.loads(ws_msg.data)
                                msg_type = msg.get("type")

                                if msg_type == "image":
                                    blob = msg.get("blob", "")
                                    url = msg.get("url", "")

                                    if blob and url:
                                        image_id = self._extract_image_id(url)
                                        if not image_id:
                                            continue

                                        blob_size = len(blob)
                                        is_final = self._is_final_image(url, blob_size)

                                        # 确定阶段
                                        if is_final:
                                            stage = "final"
                                        elif blob_size > 30000:
                                            stage = "medium"
                                            # 记录收到 medium 的时间
                                            if medium_received_time is None:
                                                medium_received_time = time.time()
                                        else:
                                            stage = "preview"

                                        # 更新或创建图片进度
                                        img_progress = ImageProgress(
                                            image_id=image_id,
                                            stage=stage,
                                            blob=blob,
                                            blob_size=blob_size,
                                            url=url,
                                            is_final=is_final
                                        )

                                        # 只更新到更高阶段
                                        existing = progress.images.get(image_id)
                                        if not existing or (not existing.is_final):
                                            progress.images[image_id] = img_progress

                                            # 更新完成计数
                                            progress.completed = len([
                                                img for img in progress.images.values()
                                                if img.is_final
                                            ])

                                            logger.info(
                                                f"[Grok] 图片 {image_id[:8]}... "
                                                f"阶段={stage} 大小={blob_size} "
                                                f"进度={progress.completed}/{n}"
                                            )

                                            # 调用流式回调
                                            if stream_callback:
                                                try:
                                                    await stream_callback(img_progress, progress)
                                                except Exception as e:
                                                    logger.warning(f"[Grok] 流式回调错误: {e}")

                                elif msg_type == "error":
                                    error_code = msg.get("err_code", "")
                                    error_msg = msg.get("err_msg", "")
                                    logger.warning(f"[Grok] 错误: {error_code} - {error_msg}")
                                    error_info = {"error_code": error_code, "error": error_msg}

                                    if error_code == "rate_limit_exceeded":
                                        return {
                                            "success": False,
                                            "error_code": error_code,
                                            "error": error_msg
                                        }

                                # 检查是否收集够了最终图片
                                if progress.completed >= n:
                                    logger.info(f"[Grok] 已收集 {progress.completed} 张最终图片")
                                    break

                                # 检查是否被 blocked: 有 medium 但超过 15 秒没有 final
                                if medium_received_time and progress.completed == 0:
                                    time_since_medium = time.time() - medium_received_time
                                    if time_since_medium > 15:
                                        logger.warning(
                                            f"[Grok] 检测到 blocked: 收到 medium 后 "
                                            f"{time_since_medium:.1f}s 仍无 final"
                                        )
                                        return {
                                            "success": False,
                                            "error_code": "blocked",
                                            "error": "生成被阻止，无法获取最终图片"
                                        }

                            elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                logger.warning(f"[Grok] WebSocket 关闭或错误: {ws_msg.type}")
                                break

                        except asyncio.TimeoutError:
                            # 检查是否被 blocked
                            if medium_received_time and progress.completed == 0:
                                time_since_medium = time.time() - medium_received_time
                                if time_since_medium > 10:
                                    logger.warning(
                                        f"[Grok] 超时检测到 blocked: 收到 medium 后 "
                                        f"{time_since_medium:.1f}s 仍无 final"
                                    )
                                    return {
                                        "success": False,
                                        "error_code": "blocked",
                                        "error": "生成被阻止，无法获取最终图片"
                                    }

                            # 如果已经有一些最终图片且超过10秒没有新消息，认为完成
                            if progress.completed > 0 and time.time() - last_activity > 10:
                                logger.info(f"[Grok] 超时，已收集 {progress.completed} 张图片")
                                break
                            continue

                    # 保存最终图片
                    result_urls, result_b64 = await self._save_final_images(progress, n)

                    if result_urls:
                        return {
                            "success": True,
                            "urls": result_urls,
                            "b64_list": result_b64,
                            "count": len(result_urls)
                        }
                    elif error_info:
                        return {"success": False, **error_info}
                    else:
                        # 检查是否是 blocked
                        if progress.check_blocked():
                            return {
                                "success": False,
                                "error_code": "blocked",
                                "error": "生成被阻止，无法获取最终图片"
                            }
                        return {"success": False, "error": "未收到图片数据"}

        except aiohttp.ClientError as e:
            logger.error(f"[Grok] 连接错误: {e}")
            return {"success": False, "error": f"连接失败: {e}"}

    async def _save_final_images(
        self,
        progress: GenerationProgress,
        n: int
    ) -> tuple[List[str], List[str]]:
        """保存最终图片到本地，同时返回 URL 列表和 base64 列表"""
        result_urls = []
        result_b64 = []
        settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)

        # 优先保存最终版本，如果没有则使用最大的版本
        saved_ids = set()

        for img in sorted(
            progress.images.values(),
            key=lambda x: (x.is_final, x.blob_size),
            reverse=True
        ):
            if img.image_id in saved_ids:
                continue
            if len(saved_ids) >= n:
                break

            try:
                image_data = base64.b64decode(img.blob)

                # 根据是否是最终版本决定扩展名
                ext = "jpg" if img.is_final else "png"
                filename = f"{img.image_id}.{ext}"
                filepath = settings.IMAGES_DIR / filename

                with open(filepath, 'wb') as f:
                    f.write(image_data)

                url = f"{settings.get_base_url()}/images/{filename}"
                result_urls.append(url)
                result_b64.append(img.blob)
                saved_ids.add(img.image_id)

                logger.info(
                    f"[Grok] 保存图片: {filename} "
                    f"({len(image_data) / 1024:.1f}KB, {img.stage})"
                )

            except Exception as e:
                logger.error(f"[Grok] 保存图片失败: {e}")

        return result_urls, result_b64

    async def generate_stream(
        self,
        prompt: str,
        aspect_ratio: str = "2:3",
        n: int = None,
        enable_nsfw: bool = True,
        sso: Optional[str] = None
    ):
        """
        流式生成图片 - 使用异步生成器

        Yields:
            Dict 包含当前图片进度信息
        """
        # 使用配置的默认图片数量
        if n is None:
            n = settings.DEFAULT_IMAGE_COUNT

        queue: asyncio.Queue = asyncio.Queue()
        done = asyncio.Event()

        async def callback(img: ImageProgress, prog: GenerationProgress):
            await queue.put({
                "type": "progress",
                "image_id": img.image_id,
                "stage": img.stage,
                "blob_size": img.blob_size,
                "is_final": img.is_final,
                "completed": prog.completed,
                "total": prog.total
            })

        async def generate_task():
            result = await self.generate(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                n=n,
                enable_nsfw=enable_nsfw,
                sso=sso,
                stream_callback=callback
            )
            await queue.put({"type": "result", **result})
            done.set()

        task = asyncio.create_task(generate_task())

        try:
            while not done.is_set() or not queue.empty():
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield item
                    if item.get("type") == "result":
                        break
                except asyncio.TimeoutError:
                    continue
        finally:
            if not task.done():
                task.cancel()


# 全局实例
grok_client = GrokImagineClient()
