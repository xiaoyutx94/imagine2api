"""Admin API 路由"""

import asyncio
from fastapi import APIRouter
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

router = APIRouter()


@router.get("/status")
async def get_status():
    """获取服务状态"""
    # Redis 版本是异步的
    if hasattr(sso_manager, 'get_status') and asyncio.iscoroutinefunction(sso_manager.get_status):
        sso_status = await sso_manager.get_status()
    else:
        sso_status = sso_manager.get_status()

    # 构建代理配置信息
    proxy_config = {
        "proxy_url": settings.PROXY_URL,
        "http_proxy": settings.HTTP_PROXY,
        "https_proxy": settings.HTTPS_PROXY
    }
    # 过滤掉 None 值
    proxy_config = {k: v for k, v in proxy_config.items() if v}

    return {
        "service": "running",
        "sso": sso_status,
        "proxy": proxy_config if proxy_config else "none",
        "config": {
            "host": settings.HOST,
            "port": settings.PORT,
            "images_dir": str(settings.IMAGES_DIR),
            "base_url": settings.get_base_url(),
            "sso_file": str(settings.SSO_FILE),
            "redis_enabled": settings.REDIS_ENABLED,
            "rotation_strategy": settings.SSO_ROTATION_STRATEGY,
            "daily_limit": settings.SSO_DAILY_LIMIT
        }
    }


@router.post("/sso/reload")
async def reload_sso():
    """重新加载 SSO 列表"""
    count = await sso_manager.reload()
    logger.info(f"[Admin] 重新加载 SSO: {count} 个")
    return {
        "success": True,
        "count": count
    }


@router.post("/sso/reset-usage")
async def reset_sso_usage():
    """手动重置每日使用量（仅 Redis 模式）"""
    if hasattr(sso_manager, 'reset_daily_usage'):
        await sso_manager.reset_daily_usage()
        logger.info("[Admin] 手动重置每日使用量")
        return {"success": True, "message": "每日使用量已重置"}
    return {"success": False, "message": "该功能仅在 Redis 模式下可用"}


@router.get("/images/list")
async def list_images(limit: int = 50):
    """列出已缓存的图片"""
    images = []
    if settings.IMAGES_DIR.exists():
        files = sorted(settings.IMAGES_DIR.glob("*.jpg"), key=lambda x: x.stat().st_mtime, reverse=True)
        for f in files[:limit]:
            images.append({
                "filename": f.name,
                "url": f"{settings.get_base_url()}/images/{f.name}",
                "size": f.stat().st_size
            })
    return {"images": images, "count": len(images)}


@router.delete("/images/clear")
async def clear_images():
    """清空图片缓存"""
    count = 0
    if settings.IMAGES_DIR.exists():
        for f in settings.IMAGES_DIR.glob("*"):
            if f.is_file():
                f.unlink()
                count += 1

    logger.info(f"[Admin] 已清空 {count} 张图片")
    return {"success": True, "deleted": count}
