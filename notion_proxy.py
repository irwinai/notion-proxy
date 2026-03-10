#!/usr/bin/env python3
"""
Notion AI 反向代理服务 v4

架构: FastAPI → CDP DOM 操控 → Notion 原生 AI 请求

原理（不做任何请求拦截/修改）:
1. 通过 CDP 操控 Notion AI 面板 DOM 输入消息并发送
2. Notion 自己构建完整的 runInferenceTranscript 请求
3. 注入 fetch hook 用 clone().body.getReader() 读取 response stream
4. 解析 NDJSON patch 流提取 AI 回复文本

接口:
- POST /chat         同步返回完整回复
- POST /chat/stream  SSE 流式返回（逐 token）

前置: Notion 以 --remote-debugging-port=9333 启动
启动: python3 notion_proxy.py
"""

import asyncio
import json
import logging
import os
import uuid
import time

import aiohttp
from fastapi import HTTPException

from common import parse_patches, extract_tokens, create_app, register_routes

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("notion-proxy")

CDP_PORT = int(os.environ.get("CDP_PORT", "9333"))


class NotionAIProxy:
    """通过 CDP 操控 Notion AI 面板发送消息并获取回复"""

    def __init__(self):
        self._lock = asyncio.Lock()

    async def _get_ws_url(self) -> str:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{CDP_PORT}/json") as r:
                targets = await r.json()
                for t in targets:
                    if "notion.so" in t.get("url", ""):
                        return t["webSocketDebuggerUrl"]
                raise RuntimeError("未找到 Notion 页面")

    async def health_check(self) -> bool:
        try:
            await self._get_ws_url()
            return True
        except Exception:
            return False

    def _parse_patches(self, ndjson_text: str) -> str:
        """从 NDJSON patch 流提取 AI 回复文本"""
        return parse_patches(ndjson_text)

    def _extract_tokens_from_ndjson(self, ndjson_text: str) -> list[str]:
        """从 NDJSON patch 流提取 token 列表（用于流式返回）"""
        return extract_tokens(ndjson_text)

    async def _create_session(self):
        """创建 CDP 会话，返回 (ws, eval_js, request_id)"""
        ws_url = await self._get_ws_url()
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(ws_url, max_msg_size=100 * 1024 * 1024)

        mid_counter = [0]

        async def cmd(method, params=None):
            mid_counter[0] += 1
            msg = {"id": mid_counter[0], "method": method}
            if params:
                msg["params"] = params
            await ws.send_json(msg)
            return mid_counter[0]

        async def eval_js(expr, await_promise=True, timeout=15000):
            cid = await cmd(
                "Runtime.evaluate",
                {
                    "expression": expr,
                    "awaitPromise": await_promise,
                    "returnByValue": True,
                    "timeout": timeout,
                },
            )
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                if d.get("id") == cid:
                    r = d.get("result", {}).get("result", {})
                    exc = d.get("result", {}).get("exceptionDetails")
                    if exc:
                        desc = exc.get("exception", {}).get("description", "")
                        return f"ERROR: {desc}"
                    return r.get("value")
            return None

        return session, ws, eval_js

    async def _inject_hook_and_trigger(self, eval_js, message: str) -> str:
        """注入 fetch hook 并触发 Notion 原生 AI 请求，返回 request_id"""
        request_id = str(uuid.uuid4())[:8]

        # 注入 fetch hook
        hook_js = f"""
        (function() {{
            const reqId = '{request_id}';
            window['_ai_' + reqId] = {{
                status: 'pending', data: '', done: false,
                lastRead: 0  // 上次读取的位置，用于增量读取
            }};
            if (!window._origFetch) window._origFetch = window.fetch;

            window.fetch = function(...args) {{
                const url = typeof args[0] === 'string' ? args[0] : args[0]?.url;
                const promise = window._origFetch.apply(this, args);

                if (url && url.includes('runInferenceTranscript')) {{
                    promise.then(resp => {{
                        const state = window['_ai_' + reqId];
                        if (!state || state.done) return;
                        state.status = 'streaming';
                        const reader = resp.clone().body.getReader();
                        const decoder = new TextDecoder();

                        function readLoop() {{
                            reader.read().then(({{done, value}}) => {{
                                if (done) {{
                                    state.done = true;
                                    state.status = 'done';
                                    return;
                                }}
                                state.data += decoder.decode(value, {{stream: true}});
                                readLoop();
                            }}).catch(e => {{
                                state.status = 'error: ' + e.message;
                                state.done = true;
                            }});
                        }}
                        readLoop();
                    }}).catch(e => {{
                        const state = window['_ai_' + reqId];
                        if (state) {{
                            state.status = 'fetch_error: ' + e.message;
                            state.done = true;
                        }}
                    }});
                }}
                return promise;
            }};
            return reqId;
        }})()
        """
        await eval_js(hook_js, await_promise=False)
        logger.info(f"Hook 注入: reqId={request_id}")

        # 触发 Notion AI 请求
        msg_escaped = json.dumps(message)
        trigger_js = f"""
        (async () => {{
            const btn = document.querySelector('[data-testid="sidebar-ai-button"]');
            if (btn) btn.click();
            await new Promise(r => setTimeout(r, 1000));

            const editors = document.querySelectorAll('[contenteditable="true"]');
            if (editors.length === 0) return 'no editors';
            const input = editors[editors.length - 1];

            input.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
            document.execCommand('insertText', false, {msg_escaped});
            await new Promise(r => setTimeout(r, 300));

            input.dispatchEvent(new KeyboardEvent('keydown', {{
                key: 'Enter', code: 'Enter', keyCode: 13,
                bubbles: true, cancelable: true
            }}));
            return 'sent';
        }})()
        """
        trigger_result = await eval_js(trigger_js, timeout=10000)
        logger.info(f"触发结果: {trigger_result}")

        if trigger_result != "sent":
            raise HTTPException(500, f"触发失败: {trigger_result}")

        return request_id

    async def _cleanup(self, eval_js, request_id: str, session, ws):
        """清理 hook 和全局变量"""
        try:
            await eval_js(
                "if(window._origFetch) window.fetch = window._origFetch;",
                await_promise=False,
            )
            await eval_js(f"delete window['_ai_{request_id}']", await_promise=False)
        except Exception:
            pass
        try:
            await ws.close()
            await session.close()
        except Exception:
            pass

    async def chat(self, message: str, model: str = None, system: str = None) -> dict:
        """同步接口：等待完整回复后返回"""
        # DOM 方案: system prompt 嵌入到 message 前面
        if system:
            message = (
                f"<system>\n{system}\n</system>\n\n"
                f"请严格遵循上述 system 指令回答以下问题，"
                f"直接输出最终回答，不要输出思考过程。\n\n{message}"
            )
        async with self._lock:
            session, ws, eval_js = await self._create_session()
            try:
                request_id = await self._inject_hook_and_trigger(eval_js, message)
                logger.info("等待 AI 响应...")
                start = time.time()

                for i in range(60):
                    await asyncio.sleep(1)
                    status_js = f"""
                    (function() {{
                        const s = window['_ai_{request_id}'];
                        if (!s) return JSON.stringify({{status: 'not_found'}});
                        return JSON.stringify({{
                            status: s.status, done: s.done,
                            length: s.data.length,
                            data: s.done ? s.data : ''
                        }});
                    }})()
                    """
                    result = await eval_js(status_js, await_promise=False)
                    if not result:
                        continue
                    try:
                        state = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    elapsed = time.time() - start
                    if state.get("done"):
                        data = state.get("data", "")
                        logger.info(f"流完成 ({state['length']} chars, {elapsed:.1f}s)")
                        if len(data) < 10:
                            raise HTTPException(500, f"响应太短: {data}")
                        reply = self._parse_patches(data)
                        logger.info(f"✅ AI 回复 ({len(reply)} chars)")
                        return {"reply": reply}

                    if elapsed > 1 and i % 5 == 0:
                        logger.info(
                            f"等待中... status={state['status']}, "
                            f"length={state.get('length', 0)}"
                        )

                raise HTTPException(504, "AI 响应超时 (60s)")
            finally:
                await self._cleanup(eval_js, request_id, session, ws)

    async def chat_stream(self, message: str, model: str = None, system: str = None):
        """
        流式接口：逐 token 生成 SSE 事件。
        """
        # DOM 方案: system prompt 嵌入到 message 前面
        if system:
            message = (
                f"<system>\n{system}\n</system>\n\n"
                f"请严格遵循上述 system 指令回答以下问题，"
                f"直接输出最终回答，不要输出思考过程。\n\n{message}"
            )
        async with self._lock:
            session, ws, eval_js = await self._create_session()
            try:
                request_id = await self._inject_hook_and_trigger(eval_js, message)
                logger.info("流式等待 AI 响应...")
                start = time.time()
                last_parsed_pos = 0  # 上次解析到的 data 位置

                for i in range(120):  # 最多等 120 * 0.5s = 60s
                    await asyncio.sleep(0.5)

                    # 增量读取: 只取 lastRead 之后的新数据
                    poll_js = f"""
                    (function() {{
                        const s = window['_ai_{request_id}'];
                        if (!s) return JSON.stringify({{status: 'not_found'}});
                        const newData = s.data.substring(s.lastRead);
                        s.lastRead = s.data.length;
                        return JSON.stringify({{
                            status: s.status, done: s.done,
                            newData: newData
                        }});
                    }})()
                    """
                    result = await eval_js(poll_js, await_promise=False)
                    if not result:
                        continue
                    try:
                        state = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    # 解析新增数据中的 tokens
                    new_data = state.get("newData", "")
                    if new_data:
                        tokens = self._extract_tokens_from_ndjson(new_data)
                        for token in tokens:
                            yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"

                    if state.get("done"):
                        elapsed = time.time() - start
                        logger.info(f"✅ 流式完成 ({elapsed:.1f}s)")
                        yield "data: [DONE]\n\n"
                        return

                    if state.get("status", "").startswith("error"):
                        yield f"data: {json.dumps({'error': state['status']})}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                yield f"data: {json.dumps({'error': 'timeout'})}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                await self._cleanup(eval_js, request_id, session, ws)


# ============================================================
#  FastAPI
# ============================================================

app = create_app("Notion AI Proxy (DOM)", "4.2.0")
proxy = NotionAIProxy()
register_routes(app, proxy)


if __name__ == "__main__":
    import uvicorn

    async def check():
        ok = await proxy.health_check()
        logger.info("✅ CDP 正常" if ok else "❌ CDP 失败")

    logger.info("Notion AI 反向代理 v4.2 (DOM) | http://0.0.0.0:8100")
    asyncio.get_event_loop().run_until_complete(check())
    uvicorn.run(app, host="0.0.0.0", port=8100)
