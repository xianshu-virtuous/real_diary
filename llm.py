"""real_diary 的模型调用层。

只做一件事：**把「选模型 → 组请求 → 取文本」收敛到一处**。日记的输出是
一段 markdown 正文，不需要抠 JSON，所以这里比 ``context_archiver.llm`` 简单。

模型选择（与 ``context_archiver`` / ``daily_schedule`` 同一套任务名体系）：

1. ``model.model_name`` 非空 → 按名字直接点名；
2. 否则用 ``model.task_name``（默认 ``summary``）；
3. 那个任务在实例里没配 → **回落到 ``actor``**，而不是让日记写不出来。

调用失败一律返回 ``LLMCallResult(ok=False)``，由调用方决定退避，绝不抛给主流程。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from src.app.plugin_system.api import llm_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import LLMPayload, ROLE, Text

from .config import RealDiaryConfig

logger = get_logger("real_diary.llm")


@dataclass
class LLMCallResult:
    """一次模型调用的结果。

    Attributes:
        ok: 是否成功拿到文本。
        text: 返回的正文。
        model_tag: 实际使用的模型标识（写日志与留档用）。
        error: 失败原因摘要。
    """

    ok: bool
    text: str = ""
    model_tag: str = ""
    error: str = ""


def _entry_identifier(entry: Any) -> str:
    """读模型条目里的标识（写日志用）。"""
    if isinstance(entry, dict):
        for key in ("model_identifier", "name", "id"):
            value = entry.get(key)
            if value:
                return str(value)
    return ""


def _by_name(config: RealDiaryConfig, temperature: float, max_tokens: int) -> tuple[Any, str]:
    """按模型名点名。"""
    model_name = str(config.model.model_name or "").strip()
    model_set = llm_api.get_model_set_by_name(
        model_name,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return model_set, f"by_name:{model_name}"


def _by_task(config: RealDiaryConfig, temperature: float, max_tokens: int) -> tuple[Any, str]:
    """按任务名取模型集，并覆盖温度与输出上限。

    任务不存在（实例没配）时回落到 ``actor``——**静默回落会让人以为配错了没生效**，
    所以这里打一条 INFO。
    """
    task_name = str(config.model.task_name or "").strip() or "summary"
    try:
        model_set = llm_api.get_model_set_by_task(task_name)
    except Exception as error:  # noqa: BLE001 - 任务不存在等情况
        logger.info(
            f"[real_diary] 模型任务 {task_name!r} 不可用（{error}），回落到 actor"
        )
        task_name = "actor"
        model_set = llm_api.get_model_set_by_task(task_name)

    if not model_set:
        logger.info(f"[real_diary] 模型任务 {task_name!r} 是空的，回落到 actor")
        task_name = "actor"
        model_set = llm_api.get_model_set_by_task(task_name)

    overridden: list[Any] = []
    identifiers: list[str] = []
    for entry in model_set or []:
        if isinstance(entry, dict):
            patched = dict(entry)
            patched["temperature"] = temperature
            patched["max_tokens"] = max_tokens
            overridden.append(patched)
            identifier = _entry_identifier(entry)
            if identifier:
                identifiers.append(identifier)
        else:
            overridden.append(entry)

    tag = f"task:{task_name}"
    if identifiers:
        tag = f"{tag}:{identifiers[0]}"
    return overridden, tag


def resolve_model_set(
    config: RealDiaryConfig,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> tuple[Any, str]:
    """按配置解析模型集。

    Args:
        config: 插件配置。
        temperature: 覆盖温度；``None`` 用配置值。
        max_tokens: 覆盖输出上限；``None`` 用配置值。

    Returns:
        ``(model_set, model_tag)``。
    """
    target_temperature = (
        float(temperature) if temperature is not None else float(config.model.temperature)
    )
    target_max_tokens = (
        int(max_tokens) if max_tokens is not None else int(config.model.max_tokens)
    )
    if str(config.model.model_name or "").strip():
        return _by_name(config, target_temperature, target_max_tokens)
    return _by_task(config, target_temperature, target_max_tokens)


def extract_text(response: Any) -> str:
    """从 ``LLMResponse`` 里提取纯文本。"""
    message = getattr(response, "message", None)
    if message is None:
        return ""
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        parts: list[str] = []
        for item in message:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts).strip()
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return text.strip()
    return str(message).strip()


async def call(
    config: RealDiaryConfig,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    request_name: str = "real_diary",
) -> LLMCallResult:
    """执行一次单轮模型调用。

    Args:
        config: 插件配置。
        system_prompt: 系统提示词。
        user_prompt: 用户提示词。
        temperature: 覆盖温度。
        max_tokens: 覆盖输出上限。
        request_name: 请求名（写进 LLM 统计，便于事后排查花了多少钱）。

    Returns:
        调用结果。
    """
    try:
        model_set, model_tag = resolve_model_set(
            config,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as error:  # noqa: BLE001 - 模型解析失败按调用失败处理
        logger.warning(f"[real_diary] 解析模型集失败: {error}")
        return LLMCallResult(ok=False, error=f"resolve model set: {error}")

    started = time.time()
    try:
        request = llm_api.create_llm_request(model_set, request_name=request_name)
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))
        response = await request.send(stream=False)
        await response
    except Exception as error:  # noqa: BLE001 - 调用失败按失败结果返回
        logger.warning(f"[real_diary] 模型调用失败: {error}")
        return LLMCallResult(ok=False, model_tag=model_tag, error=str(error))

    text = extract_text(response)
    if not text:
        # 「空响应」多半是 max_tokens 被思考链吃光了（context_archiver 踩过），
        # 所以把这件事写进日志，别让它变成一句没头没尾的失败。
        logger.warning(
            f"[real_diary] 模型返回空正文（model={model_tag}, "
            f"max_tokens={int(config.model.max_tokens)}, 耗时 {time.time() - started:.1f}s）"
        )
        return LLMCallResult(ok=False, model_tag=model_tag, error="empty response")
    return LLMCallResult(ok=True, text=text, model_tag=model_tag)


__all__ = ["LLMCallResult", "call", "extract_text", "resolve_model_set"]
