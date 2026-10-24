import asyncio
import httpx
from .mcp import MCPError
from .store import BudgetExhausted


def describe(exc):
    if isinstance(exc, BudgetExhausted):
        code, message = "budget_exhausted", "今日后台调用预算已用完"
    elif isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        code, message = "timeout", "等待返回超时，已保留之前成功取得的内容"
    elif isinstance(exc, httpx.HTTPStatusError):
        code, message = "http_error", f"工具接口返回 HTTP {exc.response.status_code}"
    elif isinstance(exc, httpx.TransportError):
        code, message = "connection_error", "无法连接工具接口"
    elif isinstance(exc, MCPError):
        code, message = "tool_error", str(exc) or "工具报告执行失败"
        if "deadline exceeded" in message or "timeout" in message.lower():
            code = "timeout"
        elif "Failed to get the debug url" in message or "launcher" in message:
            code = "browser_start_failed"
        elif any(word in message for word in ("笔记不可访问", "不存在", "私密")):
            code = "post_unavailable"
        elif "未登录" in message:
            code = "login_required"
    elif isinstance(exc, ValueError):
        code, message = "invalid_output", str(exc) or "模型输出未通过验证"
    else:
        code, message = "execution_error", str(exc) or type(exc).__name__
    return {"code": code, "message": message[:600], "exception_type": type(exc).__name__}
