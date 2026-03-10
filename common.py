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


def _iter_patches(ndjson_text: str):
    """遍历 NDJSON patch 流，过滤 thinking 内容，只 yield 正式回复的 token。

    Notion AI 的响应结构：
    - ADD agent-inference + value[0].type='thinking' → 思考内容（过滤）
    - ADD agent-inference + value[0].type='tool_use'  → 工具调用（过滤）
    - ADD agent-inference + value[0].type='text'      → 正式回复（保留）
    - 增量 token 的 path: /s/{index}/value/0/content
    """
    # 追踪哪些 /s/{index} 是正式回复（text 类型）
    text_indices = set()
    node_count = 0  # 当前 /s/ 下的节点数

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

            # 新增 agent-inference 节点
            if op == "a" and path == "/s/-":
                if isinstance(val, dict) and val.get("type") == "agent-inference":
                    node_count += 1
                    items = val.get("value", [])
                    if items and isinstance(items[0], dict):
                        item_type = items[0].get("type", "")
                        if item_type == "text":
                            # 这是正式回复节点
                            text_indices.add(node_count)
                            content = items[0].get("content", "")
                            if content:
                                yield content
                else:
                    node_count += 1

            # 增量 token: /s/{index}/value/0/content
            if op == "x" and path.endswith("/content"):
                # 提取 path 中的索引: /s/3/value/0/content → 3
                parts = path.split("/")
                try:
                    idx = int(parts[2])
                    if idx in text_indices:
                        yield val or ""
                except (IndexError, ValueError):
                    # 无法解析索引，保底输出
                    if text_indices:
                        yield val or ""


def parse_patches(ndjson_text: str) -> str:
    """从 NDJSON patch 流提取完整 AI 回复文本（过滤 thinking）"""
    return "".join(_iter_patches(ndjson_text))


def extract_tokens(ndjson_text: str) -> list[str]:
    """从 NDJSON patch 流提取 token 列表（过滤 thinking）"""
    return list(_iter_patches(ndjson_text))


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
