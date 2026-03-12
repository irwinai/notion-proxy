#!/usr/bin/env python3
"""
Notion AI 反向代理 — HTTP 直接请求方案

不操控 DOM，直接通过 curl_cffi 调用 Notion API。
认证信息和 threadId 通过 CDP 自动获取。

接口:
- POST /chat         同步返回完整回复
- POST /chat/stream  SSE 流式返回（逐 token）
- GET  /health       健康检查

前置: Notion 以 --remote-debugging-port=9333 启动，并打开 AI 聊天页面
启动: python3 notion_proxy_http.py
"""

import asyncio
import json
import logging
import os
import uuid
import time
import queue
import threading

import aiohttp
from curl_cffi.requests import Session as CurlSession
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from common import (
    ChatRequest,
    StreamingTokenParser,
    create_app,
    parse_patches,
    register_routes,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("notion-proxy-http")

CDP_PORT = int(os.environ.get("CDP_PORT", "9333"))

# Notion AI 完整 config 模板（从原生请求捕获，缺少任何字段都会导致返回空流）
DEFAULT_CONFIG = {
    "type": "workflow",
    "enableAgentAutomations": True,
    "enableAgentIntegrations": True,
    "enableCustomAgents": True,
    "enableExperimentalIntegrations": False,
    "enableAgentViewNotificationsTool": False,
    "enableAgentDiffs": True,
    "enableAgentCreateDbTemplate": True,
    "enableCsvAttachmentSupport": True,
    "enableDatabaseAgents": False,
    "enableAgentThreadTools": False,
    "enableRunAgentTool": False,
    "enableAgentDashboards": False,
    "enableAgentCardCustomization": True,
    "enableSystemPromptAsPage": False,
    "enableUserSessionContext": False,
    "enableScriptAgentAdvanced": False,
    "enableScriptAgent": True,
    "enableScriptAgentSearchConnectorsInCustomAgent": False,
    "enableScriptAgentGoogleDriveInCustomAgent": False,
    "enableScriptAgentSlack": True,
    "enableScriptAgentMcpServers": False,
    "enableScriptAgentMail": False,
    "enableScriptAgentCalendar": True,
    "enableScriptAgentCustomAgentTools": False,
    "enableScriptAgentCustomToolCalling": False,
    "enableCreateAndRunThread": True,
    "enableAgentGenerateImage": True,
    "enableSpeculativeSearch": False,
    "enableQueryCalendar": False,
    "enableQueryMail": False,
    "enableMailExplicitToolCalls": True,
    "enableAgentVerification": False,
    "useRulePrioritization": True,
    "searchScopes": [{"type": "everything"}],
    "useSearchToolV2": False,
    "useWebSearch": True,
    "useReadOnlyMode": False,
    "writerMode": False,
    "model": "avocado-froyo-medium",
    "modelFromUser": True,
    "isCustomAgent": False,
    "isCustomAgentBuilder": False,
    "useCustomAgentDraft": False,
    "use_draft_actor_pointer": False,
    "enableUpdatePageAutofixer": True,
    "enableMarkdownVNext": False,
    "enableUpdatePageOrderUpdates": True,
    "enableAgentSupportPropertyReorder": True,
    "useServerUndo": True,
    "enableAgentAskSurvey": False,
    "databaseAgentConfigMode": False,
    "isOnboardingAgent": False,
}


class NotionAuth:
    """通过 CDP 自动获取 Notion 认证信息和 threadId"""

    def __init__(self, port: int = CDP_PORT):
        self.port = port
        self._cookie: str = ""
        self._user_id: str = ""
        self._space_id: str = ""
        self._space_view_id: str = ""
        self._user_name: str = ""
        self._user_email: str = ""
        self._space_name: str = ""
        self._thread_id: str = ""
        self._client_version: str = ""
        self._mac_version: str = ""
        self._body_template: dict = None
        self._last_refresh: float = 0

    async def _get_ws_url(self) -> str:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{self.port}/json") as r:
                targets = await r.json()
                for t in targets:
                    if "notion.so" in t.get("url", ""):
                        return t["webSocketDebuggerUrl"]
                raise RuntimeError("未找到 Notion 页面")

    async def refresh(self):
        """刷新认证信息（每 5 分钟自动刷新）"""
        now = time.time()
        if now - self._last_refresh < 300 and self._cookie:
            return

        ws_url = await self._get_ws_url()

        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(ws_url) as ws:
                mid = 0

                async def cmd(method, params=None):
                    nonlocal mid
                    mid += 1
                    msg = {"id": mid, "method": method}
                    if params:
                        msg["params"] = params
                    await ws.send_json(msg)
                    return mid

                async def wait_result(cid):
                    async for m in ws:
                        if m.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(m.data)
                            if d.get("id") == cid:
                                return d
                    return None

                # 获取 cookies
                cid = await cmd(
                    "Network.getCookies", {"urls": ["https://www.notion.so"]}
                )
                d = await wait_result(cid)
                cookies = d["result"]["cookies"]
                self._cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                for c in cookies:
                    if c["name"] == "notion_user_id":
                        self._user_id = c["value"]

                # 获取 threadId (从 URL 参数 t)
                cid = await cmd(
                    "Runtime.evaluate",
                    {
                        "expression": """
                    (function() {
                        const url = new URL(window.location.href);
                        let t = url.searchParams.get('t') || '';
                        // 添加横杠还原 UUID 格式
                        if (t.length === 32) {
                            t = t.substring(0,8) + '-' + t.substring(8,12) + '-' +
                                t.substring(12,16) + '-' + t.substring(16,20) + '-' + t.substring(20);
                        }
                        return t;
                    })()
                    """,
                        "returnByValue": True,
                    },
                )
                d = await wait_result(cid)
                self._thread_id = d["result"]["result"].get("value", "")

                # 获取 space 信息
                cid = await cmd(
                    "Runtime.evaluate",
                    {
                        "expression": """
                    (async () => {
                        const resp = await fetch('/api/v3/getSpaces', {
                            method: 'POST', credentials: 'same-origin',
                            headers: {'Content-Type': 'application/json'},
                            body: '{}',
                        });
                        const data = await resp.json();
                        const userId = Object.keys(data)[0];
                        const userRoot = data[userId];
                        const spaceIds = Object.keys(userRoot.space || {});
                        const spaceViewIds = Object.keys(userRoot.space_view || {});
                        const spaceId = spaceIds[0] || '';
                        const space = userRoot.space?.[spaceId]?.value || {};
                        const user = userRoot.notion_user?.[userId]?.value || {};
                        return JSON.stringify({
                            userId, spaceId,
                            spaceViewId: spaceViewIds[0] || '',
                            spaceName: space.name || 'Notion',
                            userName: user.name || '',
                            userEmail: user.email || '',
                        });
                    })()
                    """,
                        "awaitPromise": True,
                        "returnByValue": True,
                        "timeout": 10000,
                    },
                )
                d = await wait_result(cid)
                info = json.loads(d["result"]["result"]["value"])
                self._user_id = info.get("userId", self._user_id)
                self._space_id = info.get("spaceId", "")
                self._space_view_id = info.get("spaceViewId", "")
                self._space_name = info.get("spaceName", "Notion")
                self._user_name = info.get("userName", "")
                self._user_email = info.get("userEmail", "")

        # 捕获原生 body 模板（通过触发一条 AI 消息）
        if not self._body_template:
            await self._capture_body_template()

        self._last_refresh = now
        logger.info(
            f"✅ 认证刷新: user={self._user_name}, " f"thread={self._thread_id[:16]}..."
        )

    async def health_check(self) -> bool:
        try:
            await self._get_ws_url()
            return True
        except Exception:
            return False

    @property
    def cookie(self):
        return self._cookie

    @property
    def user_id(self):
        return self._user_id

    @property
    def space_id(self):
        return self._space_id

    @property
    def space_view_id(self):
        return self._space_view_id

    @property
    def user_name(self):
        return self._user_name

    @property
    def user_email(self):
        return self._user_email

    @property
    def space_name(self):
        return self._space_name

    @property
    def thread_id(self):
        return self._thread_id

    @property
    def body_template(self):
        return self._body_template

    async def _capture_body_template(self):
        """通过 CDP hook 捕获一次 Notion 原生请求体作为模板"""
        ws_url = await self._get_ws_url()
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(ws_url, max_msg_size=50 * 1024 * 1024) as ws:
                mid = 0

                async def evaluate(expr, timeout=15000):
                    nonlocal mid
                    mid += 1
                    await ws.send_json(
                        {
                            "id": mid,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": expr,
                                "returnByValue": True,
                                "awaitPromise": True,
                                "timeout": timeout,
                            },
                        }
                    )
                    async for m in ws:
                        if m.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(m.data)
                            if d.get("id") == mid:
                                return (
                                    d.get("result", {}).get("result", {}).get("value")
                                )

                # 注入 body 捕获 hook
                hook_result = await evaluate(
                    """
                (function() {
                    if (!window._origFetchForCapture) window._origFetchForCapture = window.fetch;
                    window._capturedBody = null;
                    window.fetch = function(input, init) {
                        const url = typeof input === 'string' ? input : input?.url;
                        if (url && url.includes('runInferenceTranscript') && !window._capturedBody) {
                            window._capturedBody = init?.body || '';
                        }
                        return window._origFetchForCapture.apply(this, arguments);
                    };
                    return 'hook ready';
                })()
                """,
                    timeout=5000,
                )
                logger.info(f"Hook 注入结果: {hook_result}")

                # 触发 AI 消息（通过 DOM 操控）
                trigger_result = await evaluate(
                    """
                (async () => {
                    const btn = document.querySelector('[data-testid="sidebar-ai-button"]');
                    const btnInfo = btn ? 'found' : 'not found';
                    if (btn) btn.click();
                    await new Promise(r => setTimeout(r, 1500));
                    const editors = document.querySelectorAll('[contenteditable="true"]');
                    if (!editors.length) return 'no editors, btn=' + btnInfo;
                    const input = editors[editors.length - 1];
                    input.focus();
                    document.execCommand('selectAll', false, null);
                    document.execCommand('delete', false, null);
                    document.execCommand('insertText', false, 'hi');
                    await new Promise(r => setTimeout(r, 500));
                    input.dispatchEvent(new KeyboardEvent('keydown', {
                        key: 'Enter', code: 'Enter', keyCode: 13,
                        bubbles: true, cancelable: true
                    }));
                    return 'sent, editors=' + editors.length + ', btn=' + btnInfo;
                })()
                """,
                    timeout=15000,
                )
                logger.info(f"触发结果: {trigger_result}")

                if not trigger_result or "sent" not in str(trigger_result):
                    logger.warning(f"模板捕获触发失败: {trigger_result}")
                    return

                # 等待捕获
                for i in range(15):
                    await asyncio.sleep(1)
                    body_str = await evaluate("window._capturedBody || ''")
                    body_len = len(body_str) if body_str else 0
                    if i % 3 == 0:
                        logger.info(f"等待模板... 第{i+1}秒, 捕获长度={body_len}")
                    if body_str and body_len > 100:
                        self._body_template = json.loads(body_str)
                        logger.info(f"✅ Body 模板捕获成功 ({body_len} chars)")
                        break

                # 恢复 fetch
                await evaluate(
                    """
                if (window._origFetchForCapture) {
                    window.fetch = window._origFetchForCapture;
                    delete window._origFetchForCapture;
                }
                """
                )

                if not self._body_template:
                    logger.error("❌ 未能捕获 body 模板")


class NotionAIProxy:
    """通过 curl_cffi 直接调用 Notion AI 接口"""

    API_URL = "https://www.notion.so/api/v3/runInferenceTranscript"

    def __init__(self):
        self.auth = NotionAuth()
        self._lock = asyncio.Lock()

    async def health_check(self) -> bool:
        return await self.auth.health_check()

    def _now_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000+08:00")

    def _build_body(self, message: str, model: str = None, system: str = None) -> dict:
        """基于原生模板构建请求体，只替换动态字段"""
        import copy

        if not self.auth.body_template:
            raise RuntimeError(
                "Body 模板未捕获，请确保 Notion 已打开 AI 聊天页面后重启服务"
            )

        body = copy.deepcopy(self.auth.body_template)

        # 如果提供了 system prompt，嵌入到 user message 前面
        if system:
            full_message = (
                f"<system>\n{system}\n</system>\n\n"
                f"请严格遵循上述 system 指令回答以下问题。\n\n{message}"
            )
        else:
            full_message = message

        # 替换动态字段
        body["traceId"] = str(uuid.uuid4())

        for step in body["transcript"]:
            step["id"] = str(uuid.uuid4())
            if step["type"] == "user":
                step["value"] = [[full_message]]
                step["createdAt"] = self._now_iso()
            if step["type"] == "context":
                step["value"]["currentDatetime"] = self._now_iso()
            if step["type"] == "config" and model:
                step["value"]["model"] = model

        return body

    def _build_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
            "Accept-Encoding": "gzip, deflate, br",
            "notion-client-version": "23.13.20260310.0442",
            "notion-mac-version": "7.6.1",
            "notion-audit-log-platform": "mac-desktop",
            "x-notion-active-user-header": self.auth.user_id,
            "x-notion-space-id": self.auth.space_id,
            "Cookie": self.auth.cookie,
        }

    def _parse_patches(self, ndjson_text: str) -> str:
        """从 NDJSON patch 流提取 AI 回复文本"""
        return parse_patches(ndjson_text)

    async def chat(self, message: str, model: str = None, system: str = None) -> dict:
        """同步接口"""
        async with self._lock:
            await self.auth.refresh()
            body = self._build_body(message, model, system)
            headers = self._build_headers()

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._do_request, body, headers)

            if not result or len(result) < 10:
                raise HTTPException(500, f"响应不完整: {result}")

            reply = self._parse_patches(result)
            logger.info(f"✅ AI 回复 ({len(reply)} chars)")
            return {"reply": reply}

    def _do_request(self, body: dict, headers: dict) -> str:
        with CurlSession(impersonate="chrome") as s:
            resp = s.post(
                self.API_URL,
                data=json.dumps(body),
                headers=headers,
                timeout=120,
            )
            if resp.status_code != 200:
                raise HTTPException(resp.status_code, f"Notion 返回 {resp.status_code}")
            return resp.text

    async def chat_stream(self, message: str, model: str = None, system: str = None):
        """SSE 流式接口"""
        async with self._lock:
            await self.auth.refresh()
            body = self._build_body(message, model, system)
            headers = self._build_headers()

            loop = asyncio.get_event_loop()
            q = queue.Queue()

            def stream_worker():
                try:
                    # 使用增量解码器正确处理跨 chunk 的多字节 UTF-8 字符
                    import codecs

                    utf8_decoder = codecs.getincrementaldecoder("utf-8")("replace")
                    with CurlSession(impersonate="chrome") as s:
                        resp = s.post(
                            self.API_URL,
                            data=json.dumps(body),
                            headers=headers,
                            timeout=120,
                            stream=True,
                        )
                        if resp.status_code != 200:
                            q.put(("error", f"Notion 返回 {resp.status_code}"))
                            return
                        for chunk in resp.iter_content(chunk_size=None):
                            if chunk:
                                text = utf8_decoder.decode(chunk, False)
                                if text:
                                    q.put(("data", text))
                        # 刷新解码器缓冲区中剩余的字节
                        remaining = utf8_decoder.decode(b"", True)
                        if remaining:
                            q.put(("data", remaining))
                    q.put(("done", None))
                except Exception as e:
                    q.put(("error", str(e)))

            t = threading.Thread(target=stream_worker, daemon=True)
            t.start()

            parser = StreamingTokenParser()

            while True:
                try:
                    msg_type, data = await loop.run_in_executor(
                        None, lambda: q.get(timeout=1.0)
                    )
                except Exception:
                    if not t.is_alive():
                        break
                    continue

                if msg_type == "data":
                    tokens = parser.feed(data)
                    for token in tokens:
                        yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"

                elif msg_type == "done":
                    logger.info("✅ 流式完成")
                    yield "data: [DONE]\n\n"
                    break

                elif msg_type == "error":
                    yield f"data: {json.dumps({'error': data})}\n\n"
                    yield "data: [DONE]\n\n"
                    break


# ============================================================
#  FastAPI
# ============================================================

app = create_app("Notion AI Proxy (HTTP)", "2.0.0")
proxy = NotionAIProxy()
register_routes(app, proxy)


if __name__ == "__main__":
    import uvicorn

    async def check():
        try:
            await proxy.auth.refresh()
            logger.info(
                f"✅ 认证就绪: user={proxy.auth.user_name}, "
                f"thread={proxy.auth.thread_id[:16]}..."
            )
        except Exception as e:
            logger.error(f"❌ 初始化失败: {e}")

    logger.info("Notion AI 反向代理 (HTTP) | http://0.0.0.0:8100")
    asyncio.get_event_loop().run_until_complete(check())
    uvicorn.run(app, host="0.0.0.0", port=8100)
