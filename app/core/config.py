"""配置管理"""

import os
from pathlib import Path
from typing import Optional, List
from pydantic_settings import BaseSettings


# 项目根目录
ROOT_DIR = Path(__file__).parents[2]

# .env 文件路径 (支持通过环境变量自定义)
ENV_FILE_PATH = Path(os.getenv("ENV_FILE_PATH", ROOT_DIR / ".env"))


class Settings(BaseSettings):
    """应用配置

    配置优先级: 环境变量 > .env 文件 > 默认值
    """

    # 服务器配置
    HOST: str = "0.0.0.0"
    PORT: int = 9563
    DEBUG: bool = False

    # API 密钥 (用于保护此网关)
    API_KEY: str = ""

    # 代理配置 (可选) - 支持 http/https/socks5
    PROXY_URL: Optional[str] = None  # 例如: http://127.0.0.1:7890 或 socks5://127.0.0.1:1080

    # HTTP 代理 (用于 requests 库)
    HTTP_PROXY: Optional[str] = None
    HTTPS_PROXY: Optional[str] = None

    # SSO 配置
    SSO_FILE: Path = ROOT_DIR / "key.txt"

    # 图片存储 (可选, 用于缓存)
    IMAGES_DIR: Path = ROOT_DIR / "data" / "images"
    BASE_URL: Optional[str] = None  # 用于生成图片URL，不设置则自动使用 HOST:PORT

    # 生成配置
    DEFAULT_ASPECT_RATIO: str = "2:3"  # 默认宽高比
    GENERATION_TIMEOUT: int = 120  # 生成超时(秒)

    # Grok 官方 WebSocket 地址 (固定值，无需配置)
    GROK_WS_URL: str = "wss://grok.com/ws/imagine/listen"

    # Redis 配置 (用于 SSO 轮询状态持久化)
    REDIS_ENABLED: bool = False  # 是否启用 Redis
    REDIS_URL: str = "redis://localhost:6379/0"  # Redis 连接 URL

    # SSO 轮询配置
    SSO_ROTATION_STRATEGY: str = "hybrid"  # 轮询策略: round_robin/least_used/least_recent/weighted/hybrid
    SSO_DAILY_LIMIT: int = 10  # 每个 key 每24小时限制次数

    def get_base_url(self) -> str:
        """获取图片的基础 URL，如果未设置则根据 HOST:PORT 自动生成"""
        if self.BASE_URL:
            return self.BASE_URL
        host = "127.0.0.1" if self.HOST == "0.0.0.0" else self.HOST
        return f"http://{host}:{self.PORT}"

    class Config:
        env_file = str(ENV_FILE_PATH)
        env_file_encoding = "utf-8"
        extra = "ignore"  # 忽略未定义的环境变量

    def get_proxy_dict(self) -> Optional[dict]:
        """获取代理配置字典 (用于 requests)"""
        if self.PROXY_URL:
            return {
                "http": self.PROXY_URL,
                "https": self.PROXY_URL
            }
        if self.HTTP_PROXY or self.HTTPS_PROXY:
            return {
                "http": self.HTTP_PROXY,
                "https": self.HTTPS_PROXY
            }
        return None


def _ensure_env_file():
    """确保 .env 文件存在，不存在则创建默认模板"""
    if not ENV_FILE_PATH.exists():
        # 确保父目录存在
        ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

        # 创建默认 .env 文件
        default_env = """# Grok Imagine API Gateway 配置文件
# 配置优先级: 环境变量 > .env 文件 > 默认值

# ============ 服务器配置 ============
HOST=0.0.0.0
PORT=9563
# DEBUG 模式: true 时保存日志到 log.txt, false 时不保存
DEBUG=false

# ============ API 安全 ============
API_KEY=your-secure-api-key-here

# ============ 代理配置 ============
# 支持 http/https/socks5 代理，取消注释并填写你的代理地址
# PROXY_URL=http://127.0.0.1:7890
# PROXY_URL=socks5://127.0.0.1:1080

# ============ SSO 配置 ============
# SSO 密钥文件路径 (每行一个 token)
SSO_FILE=key.txt

# ============ 图片存储 ============
# 注意: 如果不设置 BASE_URL，会自动根据 HOST:PORT 生成
# 如果通过反向代理或域名访问，填写实际的外部访问地址
# BASE_URL=http://your-domain.com

# ============ 生成配置 ============
DEFAULT_ASPECT_RATIO=2:3
GENERATION_TIMEOUT=120

# ============ Redis 配置 ============
# 启用 Redis 后，SSO 状态将持久化，支持分布式部署
# REDIS_ENABLED=true
# REDIS_URL=redis://localhost:6379/0

# ============ SSO 轮询配置 ============
# 轮询策略: round_robin(简单轮询) / least_used(最少使用) / least_recent(最久未用) / weighted(权重) / hybrid(混合推荐)
# SSO_ROTATION_STRATEGY=hybrid
# 每个 key 每24小时限制调用次数
# SSO_DAILY_LIMIT=10
"""
        ENV_FILE_PATH.write_text(default_env, encoding="utf-8")


# 确保 .env 文件存在
_ensure_env_file()

# 创建全局配置实例
settings = Settings()
