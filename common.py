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

    Notion AI 的响应有两种格式：
    格式 A（简单消息）:
      - ADD /s/- agent-inference, value[0].type='text' → 正式回复
      - 增量 token: /s/{index}/value/0/content

    格式 B（工具调用/深度思考模式）:
      - ADD /s/- agent-inference, value[0].type='thinking' → 思考内容
      - ADD /s/{idx}/value/- type='text' → 正式回复（后续追加）
      - 增量 token: /s/{index}/value/{sub_idx}/content
    """
    # 追踪哪些 /s/{idx} 包含 text 类型子节点（用实际的 /s/ 索引）
    text_indices = set()
    # 从 patch-start 获取初始节点数，用于计算追加节点的实际索引
    node_count = 0

    for line in ndjson_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        # 从 patch-start 获取初始节点数
        if obj.get("type") == "patch-start":
            node_count = len(obj.get("data", {}).get("s", []))
            continue

        if obj.get("type") != "patch":
            continue
        for v in obj.get("v", []):
            op, path, val = v.get("o"), v.get("p", ""), v.get("v")

            # 顶级 /s/- 追加节点（格式 A）
            if op == "a" and path == "/s/-":
                if isinstance(val, dict) and val.get("type") == "agent-inference":
                    items = val.get("value", [])
                    if items and isinstance(items[0], dict):
                        if items[0].get("type") == "text":
                            text_indices.add(node_count)
                            content = items[0].get("content", "")
                            if content:
                                yield content
                node_count += 1

            # 子节点追加: /s/{idx}/value/- 追加 text 节点（格式 B）
            if op == "a" and isinstance(val, dict) and val.get("type") == "text":
                parts = path.split("/")
                if len(parts) >= 4 and parts[3] == "value":
                    try:
                        idx = int(parts[2])
                        text_indices.add(idx)
                        content = val.get("content", "")
                        if content:
                            yield content
                    except (IndexError, ValueError):
                        pass

            # 增量 token: /s/{index}/value/{sub}/content
            if op == "x" and path.endswith("/content"):
                parts = path.split("/")
                try:
                    idx = int(parts[2])
                    if idx in text_indices:
                        yield val or ""
                except (IndexError, ValueError):
                    if text_indices:
                        yield val or ""


def parse_patches(ndjson_text: str) -> str:
    """从 NDJSON patch 流提取完整 AI 回复文本（过滤 thinking）"""
    return "".join(_iter_patches(ndjson_text))


class StreamingTokenParser:
    """有状态的流式 token 解析器。

    解决流式场景下每个 chunk 独立调用导致状态丢失的问题。
    支持两种 Notion AI 响应格式（格式 A 和格式 B）。
    """

    def __init__(self):
        self.text_indices = set()
        self.node_count = 0
        self._buffer = ""  # 处理跨 chunk 的不完整行

    def feed(self, ndjson_text: str) -> list[str]:
        """输入新的 NDJSON 数据块，返回解析出的 token 列表。"""
        tokens = []
        data = self._buffer + ndjson_text
        lines = data.split("\n")
        self._buffer = lines[-1]
        for line in lines[:-1]:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # 从 patch-start 获取初始节点数
            if obj.get("type") == "patch-start":
                self.node_count = len(obj.get("data", {}).get("s", []))
                continue

            if obj.get("type") != "patch":
                continue
            for v in obj.get("v", []):
                op, path, val = v.get("o"), v.get("p", ""), v.get("v")

                # 顶级 /s/- 追加节点（格式 A）
                if op == "a" and path == "/s/-":
                    if isinstance(val, dict) and val.get("type") == "agent-inference":
                        items = val.get("value", [])
                        if items and isinstance(items[0], dict):
                            if items[0].get("type") == "text":
                                self.text_indices.add(self.node_count)
                                content = items[0].get("content", "")
                                if content:
                                    tokens.append(content)
                    self.node_count += 1

                # 子节点追加: /s/{idx}/value/- 追加 text 节点（格式 B）
                if op == "a" and isinstance(val, dict) and val.get("type") == "text":
                    parts = path.split("/")
                    if len(parts) >= 4 and parts[3] == "value":
                        try:
                            idx = int(parts[2])
                            self.text_indices.add(idx)
                            content = val.get("content", "")
                            if content:
                                tokens.append(content)
                        except (IndexError, ValueError):
                            pass

                # 增量 token: /s/{index}/value/{sub}/content
                if op == "x" and path.endswith("/content"):
                    parts = path.split("/")
                    try:
                        idx = int(parts[2])
                        if idx in self.text_indices:
                            tokens.append(val or "")
                    except (IndexError, ValueError):
                        if self.text_indices:
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
