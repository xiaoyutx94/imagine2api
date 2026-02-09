"""Grok Imagine API Gateway

Grok 图片生成 API 代理网关，将 Grok Imagine 封装为 OpenAI 兼容的 REST API。
使用 WebSocket 直连 Grok，无需浏览器自动化，最小化资源占用。
"""

import time
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.imagine import router as imagine_router
from app.api.chat import router as chat_router
from app.api.admin import router as admin_router
from app.core.config import settings
from app.core.logger import logger, get_uvicorn_log_config
from app.services.sso_manager import sso_manager


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """请求日志中间件"""
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        logger.info(f"[Request] {request.method} {request.url.path}")

        response = await call_next(request)

        duration = time.time() - start_time
        logger.info(f"[Response] {request.method} {request.url.path} -> {response.status_code} ({duration:.2f}s)")
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 在子进程中重新初始化日志
    from app.core.logger import setup_logger
    setup_logger()

    logger.info("=" * 50)
    logger.info("Grok Imagine API Gateway 启动中...")

    # 显示配置信息
    logger.info(f"[Config] HOST: {settings.HOST}")
    logger.info(f"[Config] PORT: {settings.PORT}")
    logger.info(f"[Config] BASE_URL: {settings.get_base_url()}")

    # 代理配置
    if settings.PROXY_URL:
        logger.info(f"[Config] PROXY_URL: {settings.PROXY_URL}")
    elif settings.HTTP_PROXY or settings.HTTPS_PROXY:
        logger.info(f"[Config] HTTP_PROXY: {settings.HTTP_PROXY}")
        logger.info(f"[Config] HTTPS_PROXY: {settings.HTTPS_PROXY}")
    else:
        logger.info("[Config] 未配置代理")

    # 加载 SSO
    logger.info(f"[SSO] 从文件加载: {settings.SSO_FILE}")
    count = sso_manager.load_sso_list()
    logger.info(f"[SSO] 已加载 {count} 个 SSO")

    # 确保图片目录存在
    settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    yield

    logger.info("Grok Imagine API Gateway 已关闭")


app = FastAPI(
    title="Grok Imagine API Gateway",
    description="Grok 图片生成 OpenAI 兼容 API",
    version="2.0.0",
    lifespan=lifespan
)

# 请求日志中间件（放在最前面）
app.add_middleware(RequestLoggingMiddleware)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 确保图片目录存在
settings.IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# 静态文件服务 (图片缓存)
app.mount("/images", StaticFiles(directory=str(settings.IMAGES_DIR)), name="images")

# 注册路由
app.include_router(chat_router, prefix="/v1", tags=["Chat"])
app.include_router(imagine_router, prefix="/v1", tags=["Images"])
app.include_router(admin_router, prefix="/admin", tags=["Admin"])


@app.get("/")
async def root():
    """服务信息"""
    return {
        "service": "Grok Imagine API Gateway",
        "version": "2.0.0",
        "status": "running",
        "docs": "/docs"
    }


@app.get("/health")
async def health():
    """健康检查"""
    sso_status = sso_manager.get_status()
    return {
        "status": "healthy",
        "sso_count": sso_status["total"],
        "sso_failed": sso_status["failed"]
    }


@app.get("/gallery", response_class=HTMLResponse)
async def gallery():
    """图片画廊 - 实时查看生成的图片"""
    import os
    from datetime import datetime

    images = []
    if settings.IMAGES_DIR.exists():
        for f in settings.IMAGES_DIR.iterdir():
            if f.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                stat = f.stat()
                images.append({
                    "name": f.name,
                    "url": f"/images/{f.name}",
                    "mtime": stat.st_mtime,
                    "size": stat.st_size
                })

    # 按修改时间倒序排列
    images.sort(key=lambda x: x["mtime"], reverse=True)

    # 生成 HTML
    image_cards = ""
    for img in images[:50]:  # 最多显示50张
        dt = datetime.fromtimestamp(img["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
        size_kb = img["size"] / 1024
        image_cards += f'''
        <div class="card">
            <a href="{img['url']}" target="_blank">
                <img src="{img['url']}" alt="{img['name']}" loading="lazy">
            </a>
            <div class="info">
                <span class="time">{dt}</span>
                <span class="size">{size_kb:.1f} KB</span>
            </div>
        </div>
        '''

    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Grok Imagine Gallery</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background: #1a1a2e;
                color: #eee;
                min-height: 100vh;
                padding: 20px;
            }}
            h1 {{
                text-align: center;
                margin-bottom: 10px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            .subtitle {{ text-align: center; color: #888; margin-bottom: 30px; }}
            .refresh-btn {{
                display: block;
                margin: 0 auto 20px;
                padding: 10px 30px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                border: none;
                border-radius: 25px;
                color: white;
                font-size: 16px;
                cursor: pointer;
                transition: transform 0.2s, box-shadow 0.2s;
            }}
            .refresh-btn:hover {{
                transform: translateY(-2px);
                box-shadow: 0 5px 20px rgba(102, 126, 234, 0.4);
            }}
            .gallery {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
                gap: 20px;
                max-width: 1400px;
                margin: 0 auto;
            }}
            .card {{
                background: #16213e;
                border-radius: 12px;
                overflow: hidden;
                transition: transform 0.3s, box-shadow 0.3s;
            }}
            .card:hover {{
                transform: translateY(-5px);
                box-shadow: 0 10px 40px rgba(0,0,0,0.3);
            }}
            .card img {{
                width: 100%;
                height: 300px;
                object-fit: cover;
                display: block;
            }}
            .info {{
                padding: 12px 15px;
                display: flex;
                justify-content: space-between;
                font-size: 12px;
                color: #888;
            }}
            .empty {{
                text-align: center;
                padding: 60px;
                color: #666;
            }}
        </style>
    </head>
    <body>
        <h1>Grok Imagine Gallery</h1>
        <p class="subtitle">共 {len(images)} 张图片</p>
        <button class="refresh-btn" onclick="location.reload()">刷新</button>
        <div class="gallery">
            {image_cards if image_cards else '<div class="empty">暂无图片</div>'}
        </div>
        <script>
            // 每30秒自动刷新
            setTimeout(() => location.reload(), 30000);
        </script>
    </body>
    </html>
    '''
    return html


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_config=get_uvicorn_log_config()
    )
