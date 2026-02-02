"""SSO 密钥管理器 - Redis 版本

支持功能：
1. 每个 key 的使用次数限制 (24小时10次)
2. 最后使用时间记录
3. 多种轮询策略
4. 持久化状态（重启不丢失）
5. 分布式支持（多实例部署）
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, List, Dict, Any
from enum import Enum
from app.core.config import settings
from app.core.logger import logger

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    aioredis = None
    logger.warning("[SSO] redis 库未安装，将使用内存模式")


class RotationStrategy(Enum):
    """轮询策略"""
    ROUND_ROBIN = "round_robin"        # 简单轮询
    LEAST_USED = "least_used"          # 最少使用优先
    LEAST_RECENT = "least_recent"      # 最久未用优先
    WEIGHTED = "weighted"              # 权重轮询（剩余配额加权）
    HYBRID = "hybrid"                  # 混合策略（推荐）


class RedisSSOManager:
    """Redis 版 SSO 密钥管理器

    Redis 数据结构：
    - sso:keys              -> Set: 所有可用的 SSO key
    - sso:failed            -> Set: 当前失败的 SSO key
    - sso:usage:{key_hash}  -> Hash: {count: int, last_used: timestamp, first_used: timestamp}
    - sso:index             -> String: 当前轮询索引（用于 round_robin）
    - sso:daily_reset       -> String: 上次重置时间戳
    """

    # 配置
    DAILY_LIMIT = 10           # 每个 key 每24小时限制次数
    RESET_INTERVAL = 86400     # 24小时（秒）

    # Redis key 前缀
    PREFIX = "sso:"
    KEYS_SET = f"{PREFIX}keys"
    FAILED_SET = f"{PREFIX}failed"
    INDEX_KEY = f"{PREFIX}index"
    DAILY_RESET_KEY = f"{PREFIX}daily_reset"

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        strategy: RotationStrategy = RotationStrategy.HYBRID,
        daily_limit: int = 10
    ):
        self.redis_url = redis_url
        self.strategy = strategy
        self.DAILY_LIMIT = daily_limit
        self._redis = None
        self._lock = asyncio.Lock()
        self._sso_list: List[str] = []  # 本地缓存
        self._initialized = False

    async def _get_redis(self):
        """获取 Redis 连接"""
        if self._redis is None:
            self._redis = aioredis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True
            )
        return self._redis

    def _key_hash(self, sso: str) -> str:
        """生成 key 的短哈希（用于 Redis key）"""
        import hashlib
        return hashlib.md5(sso.encode()).hexdigest()[:12]

    def _usage_key(self, sso: str) -> str:
        """获取某个 SSO 的使用统计 Redis key"""
        return f"{self.PREFIX}usage:{self._key_hash(sso)}"

    async def initialize(self) -> int:
        """初始化：加载 SSO 列表到 Redis"""
        async with self._lock:
            if self._initialized:
                return len(self._sso_list)

            # 从文件加载
            self._sso_list = self._load_from_file()
            if not self._sso_list:
                return 0

            r = await self._get_redis()

            # 检查是否需要每日重置
            await self._check_daily_reset(r)

            # 同步到 Redis
            pipe = r.pipeline()
            pipe.delete(self.KEYS_SET)
            for sso in self._sso_list:
                pipe.sadd(self.KEYS_SET, sso)
                # 初始化使用统计（如果不存在）
                usage_key = self._usage_key(sso)
                pipe.hsetnx(usage_key, "count", 0)
                pipe.hsetnx(usage_key, "last_used", 0)
                pipe.hsetnx(usage_key, "first_used", int(time.time()))
            await pipe.execute()

            self._initialized = True
            logger.info(f"[SSO-Redis] 初始化完成，加载了 {len(self._sso_list)} 个 SSO")
            return len(self._sso_list)

    def _load_from_file(self) -> List[str]:
        """从文件加载 SSO 列表"""
        sso_list = []
        sso_file = settings.SSO_FILE

        if not sso_file.exists():
            logger.warning(f"[SSO-Redis] 文件不存在: {sso_file}")
            return sso_list

        with open(sso_file, 'r', encoding='utf-8') as f:
            for line in f:
                sso = line.strip()
                if sso and not sso.startswith('#'):
                    sso_list.append(sso)

        return sso_list

    async def _check_daily_reset(self, r):
        """检查并执行每日重置"""
        now = int(time.time())
        last_reset = await r.get(self.DAILY_RESET_KEY)

        if last_reset is None:
            # 首次运行
            await r.set(self.DAILY_RESET_KEY, now)
            return

        last_reset = int(last_reset)
        if now - last_reset >= self.RESET_INTERVAL:
            logger.info("[SSO-Redis] 执行每日重置...")
            # 重置所有 key 的使用次数
            for sso in self._sso_list:
                usage_key = self._usage_key(sso)
                await r.hset(usage_key, "count", 0)
            # 清空失败列表
            await r.delete(self.FAILED_SET)
            # 更新重置时间
            await r.set(self.DAILY_RESET_KEY, now)
            logger.info("[SSO-Redis] 每日重置完成")

    async def get_next_sso(self) -> Optional[str]:
        """获取下一个可用的 SSO"""
        if not self._initialized:
            await self.initialize()

        if not self._sso_list:
            return None

        r = await self._get_redis()

        # 检查每日重置
        await self._check_daily_reset(r)

        # 根据策略选择
        if self.strategy == RotationStrategy.ROUND_ROBIN:
            return await self._get_round_robin(r)
        elif self.strategy == RotationStrategy.LEAST_USED:
            return await self._get_least_used(r)
        elif self.strategy == RotationStrategy.LEAST_RECENT:
            return await self._get_least_recent(r)
        elif self.strategy == RotationStrategy.WEIGHTED:
            return await self._get_weighted(r)
        else:  # HYBRID
            return await self._get_hybrid(r)

    async def _get_available_keys(self, r) -> List[str]:
        """获取所有可用的 key（未失败且未超限）"""
        failed = await r.smembers(self.FAILED_SET)
        available = []

        for sso in self._sso_list:
            if sso in failed:
                continue

            # 检查使用次数
            usage = await r.hgetall(self._usage_key(sso))
            count = int(usage.get("count", 0))
            if count >= self.DAILY_LIMIT:
                continue

            available.append(sso)

        return available

    async def _get_round_robin(self, r) -> Optional[str]:
        """简单轮询"""
        available = await self._get_available_keys(r)
        if not available:
            return await self._handle_all_exhausted(r)

        # 获取并递增索引
        index = await r.incr(self.INDEX_KEY)
        index = (index - 1) % len(available)

        return available[index]

    async def _get_least_used(self, r) -> Optional[str]:
        """最少使用优先"""
        available = await self._get_available_keys(r)
        if not available:
            return await self._handle_all_exhausted(r)

        # 获取使用次数最少的
        min_count = float('inf')
        selected = available[0]

        for sso in available:
            usage = await r.hgetall(self._usage_key(sso))
            count = int(usage.get("count", 0))
            if count < min_count:
                min_count = count
                selected = sso

        return selected

    async def _get_least_recent(self, r) -> Optional[str]:
        """最久未用优先"""
        available = await self._get_available_keys(r)
        if not available:
            return await self._handle_all_exhausted(r)

        # 获取最久未使用的
        oldest_time = float('inf')
        selected = available[0]

        for sso in available:
            usage = await r.hgetall(self._usage_key(sso))
            last_used = int(usage.get("last_used", 0))
            if last_used < oldest_time:
                oldest_time = last_used
                selected = sso

        return selected

    async def _get_weighted(self, r) -> Optional[str]:
        """权重轮询（剩余配额作为权重）"""
        import random

        available = await self._get_available_keys(r)
        if not available:
            return await self._handle_all_exhausted(r)

        # 计算权重
        weights = []
        for sso in available:
            usage = await r.hgetall(self._usage_key(sso))
            count = int(usage.get("count", 0))
            remaining = self.DAILY_LIMIT - count
            weights.append(max(1, remaining))  # 至少为1

        # 加权随机选择
        total = sum(weights)
        r_val = random.uniform(0, total)
        cumulative = 0
        for i, w in enumerate(weights):
            cumulative += w
            if r_val <= cumulative:
                return available[i]

        return available[-1]

    async def _get_hybrid(self, r) -> Optional[str]:
        """混合策略：综合考虑剩余配额和最后使用时间

        评分公式: score = remaining_quota * time_factor
        - remaining_quota: 剩余配额 (1-10)
        - time_factor: 时间因子，距上次使用越久分数越高
        """
        available = await self._get_available_keys(r)
        if not available:
            return await self._handle_all_exhausted(r)

        now = time.time()
        best_score = -1
        selected = available[0]

        for sso in available:
            usage = await r.hgetall(self._usage_key(sso))
            count = int(usage.get("count", 0))
            last_used = int(usage.get("last_used", 0))

            remaining = self.DAILY_LIMIT - count
            # 时间因子：每分钟 +0.1 分，最高 +10 分
            if last_used == 0:
                time_factor = 10  # 从未使用过，给最高分
            else:
                minutes_ago = (now - last_used) / 60
                time_factor = min(10, minutes_ago * 0.1)

            score = remaining * (1 + time_factor)

            if score > best_score:
                best_score = score
                selected = sso

        return selected

    async def _handle_all_exhausted(self, r) -> Optional[str]:
        """处理所有 key 都用完的情况"""
        logger.warning("[SSO-Redis] 所有 SSO 都已耗尽或失败")

        # 检查是否所有 key 都是因为失败而不可用
        failed = await r.smembers(self.FAILED_SET)
        if len(failed) == len(self._sso_list):
            # 所有 key 都失败了，重置失败列表
            await r.delete(self.FAILED_SET)
            logger.info("[SSO-Redis] 重置失败列表")
            return self._sso_list[0] if self._sso_list else None

        # 否则是配额用完，返回 None
        return None

    async def record_usage(self, sso: str):
        """记录使用（调用后更新统计）"""
        r = await self._get_redis()
        usage_key = self._usage_key(sso)
        now = int(time.time())

        pipe = r.pipeline()
        pipe.hincrby(usage_key, "count", 1)
        pipe.hset(usage_key, "last_used", now)
        await pipe.execute()

        logger.debug(f"[SSO-Redis] 记录使用: {sso[:20]}...")

    async def mark_failed(self, sso: str, reason: str = ""):
        """标记 SSO 为失败"""
        r = await self._get_redis()
        await r.sadd(self.FAILED_SET, sso)
        logger.warning(f"[SSO-Redis] 标记失败: {sso[:20]}... 原因: {reason}")

    async def mark_success(self, sso: str):
        """标记 SSO 为成功（从失败列表移除）"""
        r = await self._get_redis()
        await r.srem(self.FAILED_SET, sso)

    async def get_status(self) -> Dict[str, Any]:
        """获取详细状态"""
        if not self._initialized:
            await self.initialize()

        r = await self._get_redis()
        failed = await r.smembers(self.FAILED_SET)

        keys_status = []
        for sso in self._sso_list:
            usage = await r.hgetall(self._usage_key(sso))
            count = int(usage.get("count", 0))
            last_used = int(usage.get("last_used", 0))

            keys_status.append({
                "key_prefix": sso[:20] + "...",
                "used_today": count,
                "remaining": max(0, self.DAILY_LIMIT - count),
                "last_used": last_used,
                "failed": sso in failed
            })

        # 获取下次重置时间
        last_reset = await r.get(self.DAILY_RESET_KEY)
        next_reset = int(last_reset or 0) + self.RESET_INTERVAL

        return {
            "total_keys": len(self._sso_list),
            "failed_count": len(failed),
            "strategy": self.strategy.value,
            "daily_limit": self.DAILY_LIMIT,
            "next_reset_timestamp": next_reset,
            "keys": keys_status
        }

    async def reload(self) -> int:
        """重新加载 SSO 列表"""
        async with self._lock:
            self._initialized = False
            self._sso_list = []
            r = await self._get_redis()
            await r.delete(self.KEYS_SET)
            return await self.initialize()

    async def reset_daily_usage(self):
        """手动重置每日使用量"""
        r = await self._get_redis()
        for sso in self._sso_list:
            await r.hset(self._usage_key(sso), "count", 0)
        await r.delete(self.FAILED_SET)
        await r.set(self.DAILY_RESET_KEY, int(time.time()))
        logger.info("[SSO-Redis] 手动重置每日使用量完成")

    async def close(self):
        """关闭 Redis 连接"""
        if self._redis:
            await self._redis.close()
            self._redis = None


# 工厂函数：根据配置决定使用哪个管理器
def create_sso_manager(
    use_redis: bool = True,
    redis_url: str = "redis://localhost:6379/0",
    strategy: str = "hybrid",
    daily_limit: int = 10
):
    """创建 SSO 管理器

    Args:
        use_redis: 是否使用 Redis（否则使用内存版本）
        redis_url: Redis 连接 URL
        strategy: 轮询策略 (round_robin/least_used/least_recent/weighted/hybrid)
        daily_limit: 每个 key 每日限制次数
    """
    if use_redis and REDIS_AVAILABLE:
        return RedisSSOManager(
            redis_url=redis_url,
            strategy=RotationStrategy(strategy),
            daily_limit=daily_limit
        )
    else:
        # 回退到文件版本
        from app.services.sso_manager import SSOManager
        logger.warning("[SSO] 使用文件版本管理器")
        return SSOManager(strategy=strategy, daily_limit=daily_limit)
