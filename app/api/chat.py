"""Chat Completions API - OpenAI 兼容的 LLM 网关，用于图片生成"""

import time
import json
import uuid
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logger import logger
from app.services.grok_client import grok_client, ImageProgress, GenerationProgress


router = APIRouter()


# ============== 请求/响应模型 ==============

class ChatMessage(BaseModel):
    """聊天消息"""
    role: str = Field(..., description="角色: user/assistant/system")
    content: str = Field(..., description="消息内容")


class ChatCompletionRequest(BaseModel):
    """OpenAI Chat Completion 请求"""
    model: str = Field("grok-imagine", description="模型名称")
    messages: List[ChatMessage] = Field(..., description="消息列表")
    stream: bool = Field(True, description="是否流式返回")
    max_tokens: Optional[int] = Field(4096, description="最大 token 数")
    temperature: Optional[float] = Field(1.0, description="温度")
    n: Optional[int] = Field(4, description="生成图片数量", ge=1, le=4)

    class Config:
        json_schema_extra = {
            "example": {
                "model": "grok-imagine",
                "messages": [{"role": "user", "content": "画一只可爱的猫咪"}],
                "stream": True
            }
        }


# ============== 辅助函数 ==============

def verify_api_key(authorization: Optional[str] = Header(None)) -> bool:
    """验证 API 密钥"""
    if not settings.API_KEY:
        return True

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid Authorization format")

    token = authorization[7:]
    if token != settings.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return True


def extract_prompt(messages: List[ChatMessage]) -> str:
    """从消息列表中提取图片生成提示词"""
    # 取最后一条 user 消息作为提示词
    for msg in reversed(messages):
        if msg.role == "user" and msg.content.strip():
            return msg.content.strip()
    return ""


def create_chat_chunk(
    chunk_id: str,
    content: str = "",
    finish_reason: Optional[str] = None,
    thinking: Optional[str] = None,
    thinking_progress: Optional[int] = None
) -> str:
    """创建 SSE 格式的聊天响应块"""
    delta: Dict[str, Any] = {}

    if content:
        delta["content"] = content
    if thinking:
        delta["thinking"] = thinking
    if thinking_progress is not None:
        delta["thinking_progress"] = thinking_progress

    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "grok-imagine",
        "choices": [{
            "index": 0,
            "delta": delta if delta else {},
            "finish_reason": finish_reason
        }]
    }

    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


# ============== API 路由 ==============

@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    authorization: Optional[str] = Header(None)
):
    """
    OpenAI 兼容的 Chat Completions API

    用户输入要画的内容，返回流式的思考进度和最终图片 URL
    """
    verify_api_key(authorization)

    # 提取提示词
    prompt = extract_prompt(request.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="No prompt found in messages")

    logger.info(f"[Chat] 生成请求: {prompt[:50]}... n={request.n}")

    # 流式模式
    if request.stream:
        return StreamingResponse(
            stream_chat_generate(prompt=prompt, n=request.n),
            media_type="text/event-stream"
        )

    # 非流式模式 - 等待完成后返回
    result = await grok_client.generate(
        prompt=prompt,
        n=request.n,
        enable_nsfw=True
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=500,
            detail=result.get("error", "Image generation failed")
        )

    # 构建响应内容
    urls = result.get("urls", [])
    content = "已为您生成图片：\n\n" + "\n".join([f"![图片]({url})" for url in urls])

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "grok-imagine",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": len(prompt),
            "completion_tokens": len(content),
            "total_tokens": len(prompt) + len(content)
        }
    }


async def stream_chat_generate(prompt: str, n: int):
    """
    流式生成图片，输出思考进度和最终 URL

    进度映射:
    - preview (预览): 33%
    - medium (中等): 66%
    - final (最终): 99%
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"

    # 阶段到进度的映射
    stage_progress = {
        "preview": 33,
        "medium": 66,
        "final": 99
    }

    # 记录每张图片的最新阶段，避免重复输出
    image_stages: Dict[str, str] = {}
    final_urls: List[str] = []

    try:
        # 开始思考
        yield create_chat_chunk(
            chunk_id,
            thinking=f"正在为您生成图片: {prompt[:50]}...",
            thinking_progress=0
        )

        async for item in grok_client.generate_stream(
            prompt=prompt,
            n=n,
            enable_nsfw=True
        ):
            if item.get("type") == "progress":
                image_id = item["image_id"]
                stage = item["stage"]
                completed = item["completed"]
                total = item["total"]

                # 只在阶段变化时输出
                if image_stages.get(image_id) != stage:
                    image_stages[image_id] = stage
                    progress = stage_progress.get(stage, 0)

                    # 计算整体进度
                    overall_progress = int((completed / total) * 100) if total > 0 else progress

                    # 构建思考内容
                    stage_names = {"preview": "预览", "medium": "中等", "final": "高清"}
                    thinking_text = (
                        f"图片 {len(image_stages)}/{total} - "
                        f"{stage_names.get(stage, stage)} ({progress}%)"
                    )

                    yield create_chat_chunk(
                        chunk_id,
                        thinking=thinking_text,
                        thinking_progress=progress
                    )

            elif item.get("type") == "result":
                if item.get("success"):
                    final_urls = item.get("urls", [])

                    # 输出 100% 完成
                    yield create_chat_chunk(
                        chunk_id,
                        thinking=f"生成完成! 共 {len(final_urls)} 张图片",
                        thinking_progress=100
                    )

                    # 输出最终内容 - 使用 Markdown 图片格式
                    content = "已为您生成图片：\n\n"
                    for i, url in enumerate(final_urls, 1):
                        content += f"![图片{i}]({url})\n\n"

                    yield create_chat_chunk(chunk_id, content=content)

                else:
                    # 错误
                    error_msg = item.get("error", "生成失败")
                    yield create_chat_chunk(
                        chunk_id,
                        content=f"生成失败: {error_msg}"
                    )

                # 结束
                yield create_chat_chunk(chunk_id, finish_reason="stop")
                break

        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"[Chat] 流式生成错误: {e}")
        yield create_chat_chunk(chunk_id, content=f"生成出错: {str(e)}")
        yield create_chat_chunk(chunk_id, finish_reason="stop")
        yield "data: [DONE]\n\n"


@router.get("/models")
async def list_models():
    """列出可用模型"""
    return {
        "object": "list",
        "data": [
            {
                "id": "grok-imagine",
                "object": "model",
                "created": 1700000000,
                "owned_by": "xai",
                "permission": [],
                "root": "grok-imagine",
                "parent": None
            }
        ]
    }
