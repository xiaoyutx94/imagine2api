"""SSO 密钥管理器 - 文件版本（支持多种轮询策略）

支持功能：
1. 每个 key 的使用次数限制 (24小时10次)
2. 最后使用时间记录
3. 多种轮询策略
4. 状态保存到 JSON 文件（重启不丢失）
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from enum import Enum
from dataclasses import dataclass, field, asdict
from app.core.config import settings
from app.core.logger import logger


class RotationStrategy(Enum):
    """轮询策略"""
    ROUND_ROBIN = "round_robin"        # 简单轮询
    LEAST_USED = "least_used"          # 最少使用优先
    LEAST_RECENT = "least_recent"      # 最久未用优先
    WEIGHTED = "weighted"              # 权重轮询（剩余配额加权）
    HYBRID = "hybrid"                  # 混合策略（推荐）


@dataclass
class KeyUsage:
    """单个 key 的使用统计"""
    count: int = 0              # 今日使用次数
    last_used: float = 0        # 最后使用时间戳
    first_used: float = 0       # 首次使用时间戳
    failed: bool = False        # 是否标记为失败
    age_verified: int = 0       # 年龄是否验证过 (0=未验证, 1=已验证)


class SSOManager:
    """SSO 密钥管理器 - 支持多种轮询策略

    从 SSO_FILE 文件加载 token，每行一个
    状态保存到 JSON 文件以实现持久化
    """

    # 配置
    RESET_INTERVAL = 86400     # 24小时（秒）

    def __init__(
        self,
        strategy: str = "hybrid",
        daily_limit: int = 10
    ):
        self._sso_list: List[str] = []
        self._current_index: int = 0
        self._lock = asyncio.Lock()
        self._usage: Dict[str, KeyUsage] = {}
        self._last_reset: float = 0
        self.strategy = RotationStrategy(strategy)
        self.daily_limit = daily_limit
        self._state_file = settings.SSO_FILE.parent / "sso_state.json"

    def _key_hash(self, sso: str) -> str:
        """生成 key 的短哈希"""
        import hashlib
        return hashlib.md5(sso.encode()).hexdigest()[:12]

    def load_sso_list(self) -> int:
        """从文件加载 SSO 列表"""
        self._sso_list = []

        sso_file = settings.SSO_FILE
        if not sso_file.exists():
            logger.warning(f"[SSO] 文件不存在: {sso_file}")
            return 0

        with open(sso_file, 'r', encoding='utf-8') as f:
            for line in f:
                sso = line.strip()
                if sso and not sso.startswith('#'):
                    self._sso_list.append(sso)
                    # 初始化使用统计
                    if sso not in self._usage:
                        self._usage[sso] = KeyUsage(first_used=time.time())

        # 加载持久化状态
        self._load_state()

        logger.info(f"[SSO] 从文件加载了 {len(self._sso_list)} 个 SSO，策略: {self.strategy.value}")
        return len(self._sso_list)

    def _load_state(self):
        """从文件加载状态"""
        if not self._state_file.exists():
            return

        try:
            with open(self._state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self._last_reset = data.get("last_reset", 0)
            self._current_index = data.get("current_index", 0)

            # 检查是否需要每日重置
            if time.time() - self._last_reset >= self.RESET_INTERVAL:
                self._do_daily_reset()
            else:
                # 恢复使用统计
                for key_hash, usage_data in data.get("usage", {}).items():
                    # 找到对应的 sso
                    for sso in self._sso_list:
                        if self._key_hash(sso) == key_hash:
                            self._usage[sso] = KeyUsage(**usage_data)
                            break

            logger.info("[SSO] 已加载持久化状态")
        except Exception as e:
            logger.warning(f"[SSO] 加载状态失败: {e}")

    def _save_state(self):
        """保存状态到文件"""
        try:
            usage_data = {}
            for sso, usage in self._usage.items():
                usage_data[self._key_hash(sso)] = asdict(usage)

            data = {
                "last_reset": self._last_reset,
                "current_index": self._current_index,
                "usage": usage_data
            }

            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"[SSO] 保存状态失败: {e}")

    def _do_daily_reset(self):
        """执行每日重置"""
        logger.info("[SSO] 执行每日重置...")
        for sso in self._sso_list:
            if sso in self._usage:
                self._usage[sso].count = 0
                self._usage[sso].failed = False
        self._last_reset = time.time()
        self._save_state()
        logger.info("[SSO] 每日重置完成")

    def _check_daily_reset(self):
        """检查是否需要每日重置"""
        if self._last_reset == 0:
            self._last_reset = time.time()
            return

        if time.time() - self._last_reset >= self.RESET_INTERVAL:
            self._do_daily_reset()

    def _get_available_keys(self) -> List[str]:
        """获取所有可用的 key（未失败且未超限）"""
        available = []
        for sso in self._sso_list:
            usage = self._usage.get(sso, KeyUsage())
            if usage.failed:
                continue
            if usage.count >= self.daily_limit:
                continue
            available.append(sso)
        return available

    async def get_next_sso(self) -> Optional[str]:
        """获取下一个可用的 SSO"""
        async with self._lock:
            if not self._sso_list:
                self.load_sso_list()

            if not self._sso_list:
                return None

            # 检查每日重置
            self._check_daily_reset()

            # 根据策略选择
            if self.strategy == RotationStrategy.ROUND_ROBIN:
                return self._get_round_robin()
            elif self.strategy == RotationStrategy.LEAST_USED:
                return self._get_least_used()
            elif self.strategy == RotationStrategy.LEAST_RECENT:
                return self._get_least_recent()
            elif self.strategy == RotationStrategy.WEIGHTED:
                return self._get_weighted()
            else:  # HYBRID
                return self._get_hybrid()

    def _get_round_robin(self) -> Optional[str]:
        """简单轮询"""
        available = self._get_available_keys()
        if not available:
            return self._handle_all_exhausted()

        # 确保索引在范围内
        self._current_index = self._current_index % len(available)
        selected = available[self._current_index]
        self._current_index = (self._current_index + 1) % len(available)
        return selected

    def _get_least_used(self) -> Optional[str]:
        """最少使用优先"""
        available = self._get_available_keys()
        if not available:
            return self._handle_all_exhausted()

        min_count = float('inf')
        selected = available[0]

        for sso in available:
            usage = self._usage.get(sso, KeyUsage())
            if usage.count < min_count:
                min_count = usage.count
                selected = sso

        return selected

    def _get_least_recent(self) -> Optional[str]:
        """最久未用优先"""
        available = self._get_available_keys()
        if not available:
            return self._handle_all_exhausted()

        oldest_time = float('inf')
        selected = available[0]

        for sso in available:
            usage = self._usage.get(sso, KeyUsage())
            if usage.last_used < oldest_time:
                oldest_time = usage.last_used
                selected = sso

        return selected

    def _get_weighted(self) -> Optional[str]:
        """权重轮询（剩余配额作为权重）"""
        import random

        available = self._get_available_keys()
        if not available:
            return self._handle_all_exhausted()

        weights = []
        for sso in available:
            usage = self._usage.get(sso, KeyUsage())
            remaining = self.daily_limit - usage.count
            weights.append(max(1, remaining))

        total = sum(weights)
        r_val = random.uniform(0, total)
        cumulative = 0
        for i, w in enumerate(weights):
            cumulative += w
            if r_val <= cumulative:
                return available[i]

        return available[-1]

    def _get_hybrid(self) -> Optional[str]:
        """混合策略：综合考虑剩余配额和最后使用时间"""
        available = self._get_available_keys()
        if not available:
            return self._handle_all_exhausted()

        now = time.time()
        best_score = -1
        selected = available[0]

        for sso in available:
            usage = self._usage.get(sso, KeyUsage())
            remaining = self.daily_limit - usage.count

            if usage.last_used == 0:
                time_factor = 10
            else:
                minutes_ago = (now - usage.last_used) / 60
                time_factor = min(10, minutes_ago * 0.1)

            score = remaining * (1 + time_factor)

            if score > best_score:
                best_score = score
                selected = sso

        return selected

    def _handle_all_exhausted(self) -> Optional[str]:
        """处理所有 key 都用完的情况"""
        logger.warning("[SSO] 所有 SSO 都已耗尽或失败")

        # 检查是否所有 key 都是因为失败
        all_failed = all(
            self._usage.get(sso, KeyUsage()).failed
            for sso in self._sso_list
        )

        if all_failed:
            # 重置失败状态
            for sso in self._sso_list:
                if sso in self._usage:
                    self._usage[sso].failed = False
            self._save_state()
            logger.info("[SSO] 重置失败列表")
            return self._sso_list[0] if self._sso_list else None

        return None

    async def record_usage(self, sso: str):
        """记录使用"""
        async with self._lock:
            if sso not in self._usage:
                self._usage[sso] = KeyUsage()

            self._usage[sso].count += 1
            self._usage[sso].last_used = time.time()
            self._save_state()
            logger.debug(f"[SSO] 记录使用: {sso[:20]}... 今日次数: {self._usage[sso].count}")

    async def mark_failed(self, sso: str, reason: str = ""):
        """标记 SSO 为失败"""
        async with self._lock:
            if sso not in self._usage:
                self._usage[sso] = KeyUsage()
            self._usage[sso].failed = True
            self._save_state()
            logger.warning(f"[SSO] 标记失败: {sso[:20]}... 原因: {reason}")

    async def mark_success(self, sso: str):
        """标记 SSO 为成功（从失败列表移除）"""
        async with self._lock:
            if sso in self._usage:
                self._usage[sso].failed = False
                self._save_state()

    async def get_age_verified(self, sso: str) -> int:
        """获取年龄验证状态 (0=未验证, 1=已验证)"""
        async with self._lock:
            if sso in self._usage:
                return self._usage[sso].age_verified
            return 0

    async def set_age_verified(self, sso: str, verified: int = 1):
        """设置年龄验证状态"""
        async with self._lock:
            if sso not in self._usage:
                self._usage[sso] = KeyUsage()
            self._usage[sso].age_verified = verified
            self._save_state()
            logger.info(f"[SSO] 设置年龄验证状态: {sso[:20]}... -> {verified}")

    def get_status(self) -> dict:
        """获取详细状态"""
        keys_status = []
        for sso in self._sso_list:
            usage = self._usage.get(sso, KeyUsage())
            keys_status.append({
                "key_prefix": sso[:20] + "...",
                "used_today": usage.count,
                "remaining": max(0, self.daily_limit - usage.count),
                "last_used": int(usage.last_used),
                "failed": usage.failed
            })

        next_reset = int(self._last_reset + self.RESET_INTERVAL) if self._last_reset else 0

        return {
            "total_keys": len(self._sso_list),
            "failed_count": sum(1 for u in self._usage.values() if u.failed),
            "strategy": self.strategy.value,
            "daily_limit": self.daily_limit,
            "next_reset_timestamp": next_reset,
            "keys": keys_status
        }

    async def reload(self) -> int:
        """重新加载 SSO 列表"""
        async with self._lock:
            self._usage.clear()
            self._current_index = 0
            return self.load_sso_list()

    async def reset_daily_usage(self):
        """手动重置每日使用量"""
        async with self._lock:
            self._do_daily_reset()
            logger.info("[SSO] 手动重置每日使用量完成")


# 工厂函数
def create_file_sso_manager(
    strategy: str = "hybrid",
    daily_limit: int = 10
) -> SSOManager:
    """创建文件版 SSO 管理器"""
    return SSOManager(strategy=strategy, daily_limit=daily_limit)


# 全局实例（使用配置）
sso_manager = SSOManager(
    strategy=settings.SSO_ROTATION_STRATEGY,
    daily_limit=settings.SSO_DAILY_LIMIT
)
