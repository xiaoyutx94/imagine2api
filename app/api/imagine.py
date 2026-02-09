"""Imagine API 路由 - OpenAI 兼容格式，支持流式预览"""

import time
import json
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logger import logger
from app.services.grok_client import grok_client


router = APIRouter()


# ============== 请求/响应模型 ==============

class OpenAIImageRequest(BaseModel):
    """OpenAI 兼容的图片生成请求"""
    prompt: str = Field(..., description="图片描述提示词", min_length=1)
    model: Optional[str] = Field("grok-2-image", description="模型名称")
    n: Optional[int] = Field(None, description="生成数量，不指定则使用默认配置", ge=1, le=4)
    size: Optional[str] = Field("1024x1536", description="图片尺寸")
    response_format: Optional[str] = Field("url", description="响应格式: url 或 b64_json")
    stream: Optional[bool] = Field(False, description="是否流式返回进度")

    class Config:
        json_schema_extra = {
            "example": {
                "prompt": "a beautiful sunset over the ocean",
                "n": 2,
                "size": "1024x1536"
            }
        }


class OpenAIImageData(BaseModel):
    """OpenAI 格式的图片数据"""
    url: Optional[str] = None
    b64_json: Optional[str] = None


class OpenAIImageResponse(BaseModel):
    """OpenAI 兼容的图片响应"""
    created: int
    data: List[OpenAIImageData]


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


def size_to_aspect_ratio(size: str) -> str:
    """将 OpenAI 的 size 转换为 aspect_ratio"""
    size_map = {
        "1024x1024": "1:1",
        "1024x1536": "2:3",
        "1536x1024": "3:2",
        "512x512": "1:1",
        "256x256": "1:1",
    }
    return size_map.get(size, "2:3")


# ============== API 路由 ==============

@router.post("/images/generations", response_model=OpenAIImageResponse)
async def generate_image(
    request: OpenAIImageRequest,
    authorization: Optional[str] = Header(None)
):
    """
    生成图片 (OpenAI 兼容 API)

    支持两种模式:
    - stream=false (默认): 返回完整结果
    - stream=true: 流式返回生成进度 (SSE 格式)
    """
    verify_api_key(authorization)

    logger.info(f"[API] 生成请求: {request.prompt[:50]}... stream={request.stream}")

    aspect_ratio = size_to_aspect_ratio(request.size)

    # 流式模式
    if request.stream:
        return StreamingResponse(
            stream_generate(
                prompt=request.prompt,
                aspect_ratio=aspect_ratio,
                n=request.n
            ),
            media_type="text/event-stream"
        )

    # 普通模式
    result = await grok_client.generate(
        prompt=request.prompt,
        aspect_ratio=aspect_ratio,
        n=request.n,
        enable_nsfw=True
    )

    if not result.get("success"):
        error_msg = result.get("error", "Image generation failed")
        error_code = result.get("error_code", "")

        if error_code == "rate_limit_exceeded":
            raise HTTPException(status_code=429, detail=error_msg)
        else:
            raise HTTPException(status_code=500, detail=error_msg)

    # 严格按照 response_format 返回
    if request.response_format == "b64_json":
        # 返回 base64 格式
        b64_list = result.get("b64_list", [])
        data = [OpenAIImageData(b64_json=b64) for b64 in b64_list]
    else:
        # 返回 URL 格式
        data = [OpenAIImageData(url=url) for url in result.get("urls", [])]

    return OpenAIImageResponse(
        created=int(time.time()),
        data=data
    )


async def stream_generate(prompt: str, aspect_ratio: str, n: int):
    """
    流式生成图片

    SSE 格式输出:
    - event: progress - 生成进度更新
    - event: complete - 生成完成，包含最终 URL
    - event: error - 发生错误
    """
    try:
        async for item in grok_client.generate_stream(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            n=n,
            enable_nsfw=True
        ):
            if item.get("type") == "progress":
                # 进度更新
                event_data = {
                    "image_id": item["image_id"],
                    "stage": item["stage"],
                    "is_final": item["is_final"],
                    "completed": item["completed"],
                    "total": item["total"],
                    "progress": f"{item['completed']}/{item['total']}"
                }
                yield f"event: progress\ndata: {json.dumps(event_data)}\n\n"

            elif item.get("type") == "result":
                # 最终结果
                if item.get("success"):
                    result_data = {
                        "created": int(time.time()),
                        "data": [{"url": url} for url in item.get("urls", [])]
                    }
                    yield f"event: complete\ndata: {json.dumps(result_data)}\n\n"
                else:
                    error_data = {"error": item.get("error", "Generation failed")}
                    yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
                break

    except Exception as e:
        logger.error(f"[API] 流式生成错误: {e}")
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"


@router.get("/models/imagine")
async def list_imagine_models():
    """列出图片生成模型"""
    return {
        "object": "list",
        "data": [
            {
                "id": "grok-imagine",
                "object": "model",
                "created": 1700000000,
                "owned_by": "xai"
            }
        ]
    }
