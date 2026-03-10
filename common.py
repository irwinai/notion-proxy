"""
Notion AI 反向代理 — 公共模块

提供 NDJSON 解析、FastAPI 路由注册、ChatRequest 模型等共享逻辑。
两种方案（DOM 操控 / HTTP 直接请求）共用此模块。
"""

import json
import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger("notion-proxy")


class ChatRequest(BaseModel):
    """统一请求模型"""

    message: str
    model: str = None
    system: str = None


def parse_patches(ndjson_text: str) -> str:
    """从 NDJSON patch 流提取完整 AI 回复文本"""
    tokens = []
    first_token = ""
    for line in ndjson_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "patch":
            continue
        for v in obj.get("v", []):
            op, path, val = v.get("o"), v.get("p", ""), v.get("v")
            if op == "a" and path == "/s/-":
                if isinstance(val, dict) and val.get("type") == "agent-inference":
                    for item in val.get("value", []):
                        if item.get("type") == "text":
                            first_token = item.get("content", "")
            if op == "x" and path.endswith("/content"):
                tokens.append(val or "")
    return first_token + "".join(tokens)


def extract_tokens(ndjson_text: str) -> list[str]:
    """从 NDJSON patch 流提取 token 列表（用于流式返回）"""
    tokens = []
    for line in ndjson_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "patch":
            continue
        for v in obj.get("v", []):
            op, path, val = v.get("o"), v.get("p", ""), v.get("v")
            # 首个 token
            if op == "a" and path == "/s/-":
                if isinstance(val, dict) and val.get("type") == "agent-inference":
                    for item in val.get("value", []):
                        if item.get("type") == "text":
                            tokens.append(item.get("content", ""))
            # 增量 token
            if op == "x" and path.endswith("/content"):
                tokens.append(val or "")
    return tokens


def create_app(title: str, version: str) -> FastAPI:
    """创建统一配置的 FastAPI 应用"""
    app = FastAPI(title=title, version=version)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


def register_routes(app: FastAPI, proxy):
    """注册公共路由到 FastAPI 应用

    proxy 需实现:
      - proxy.health_check() -> bool
      - proxy.chat(message, model, system) -> dict
      - proxy.chat_stream(message, model, system) -> AsyncGenerator
    """

    @app.get("/health")
    async def health():
        ok = await proxy.health_check()
        return {"status": "ok" if ok else "error"}

    @app.post("/chat")
    async def chat(req: ChatRequest):
        """同步返回完整 AI 回复"""
        try:
            return await proxy.chat(req.message, req.model, req.system)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"错误: {e}", exc_info=True)
            raise HTTPException(500, str(e))

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest):
        """SSE 流式返回 AI 回复"""
        return StreamingResponse(
            proxy.chat_stream(req.message, req.model, req.system),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
