"""流程编排 Agent 的真实模型通道 —— 一句话需求 → 一份可校验的流程配置。

**边界先说清楚**：这里只负责「把需求翻成配置」。落库、试跑、审批都不在这
（分别由 /agent/draft、/{id}/trial、/{id}/approve 负责）。分工的原因：
生成能力会换（今天 OpenAI，明天自建模型），治理链路不该跟着换。

**为什么必须有自修**：模型写配置一定会错 —— 漏字段、引用不存在的节点、
把文件字段填成字面量。一次失败就让人重来的体验等于没有这个功能；把校验
错误原样回喂给模型，通常一次就能改对。回喂的轮数有上限（LLM_MAX_ATTEMPTS），
因为错误到第三轮还改不对，说明它没理解规格，再问只是烧钱。

**为什么生成后仍然要人审批**：校验只能证明「配置合法」，不能证明「流程
符合业务意图」。合法但没用的流程（把提示词接错节点）校验是查不出来的，
所以落库一律 draft，试跑看效果，人批了才上线。

依赖注入：`transport` 参数是给测试用的（httpx.MockTransport），生产不传。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import settings
from app.schemas.schemas import AgentDraftRequest
from app.services.flow_validate import validate_flow_config

logger = logging.getLogger(__name__)

# 单次回喂里最多列多少条错误 —— 全列会把上下文撑爆，模型反而抓不住重点
MAX_ERRORS_PER_TURN = 8
# 回喂时带上模型上次输出的前多少字符（截断只为了控上下文，不是隐藏错误）
MAX_ECHO_CHARS = 4000


class AgentLLMError(RuntimeError):
    """模型通道本身不可用（没配、连不上、返回空）。和「配置写错」是两回事。"""


@dataclass
class GenerationResult:
    """一次生成尝试的最终结果。

    attempts 是**给人看的轨迹**：第几次、错在哪。审批时最该看的不是
    「模型说了什么」，而是「它改了几轮才过」—— 一次就过和三轮才过，
    可信度完全不同。
    """

    ok: bool
    config: dict | None = None
    note: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    layers: list[list[str]] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    model: str = ""
    message: str = ""


def is_configured() -> bool:
    """模型通道是否可用。只缺 key 也算可用（本地模型常常不要 key）。"""
    return bool(
        settings.LLM_BASE_URL.strip()
        and settings.LLM_MODEL.strip()
    )


def endpoint_url() -> str:
    """拼 /chat/completions 地址。

    容忍三种写法：填到根（https://api.openai.com）、填到 /v1、
    直接填完整地址。不要求部署方记住「要不要带 v1」—— 这是最常见的
    配错点，猜一次比报错让人自己试更省事。
    """
    base = settings.LLM_BASE_URL.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _as_flow(obj):
    """有的模型会包一层 {"flow": {...}} —— 有 steps 就当配置，没有就剥一层。"""
    if isinstance(obj, dict) and "steps" not in obj and isinstance(obj.get("flow"), dict):
        return obj["flow"]
    return obj


def extract_json_object(text: str) -> dict:
    """从模型输出里抠出 JSON 对象。

    真实模型不会永远只吐 JSON：常见的是 ```json 围栏、前后加一句
    「好的，配置如下」。这些都不该让人重试 —— 能解析就解析。
    解析不出来才返回错误，交给自修轮。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("模型返回了空内容")

    cleaned = text.strip().lstrip("\ufeff").strip()
    if cleaned.startswith("```"):
        # 去掉围栏和语言标记，保留里面
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()

    candidates = [cleaned]
    # 前后有解释文字时，从第一个 { 开始按 JSON 解码（raw_decode 会忽略尾部杂字）
    start = cleaned.find("{")
    if start > 0:
        candidates.append(cleaned[start:])

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return _as_flow(obj)

    # 尾部还有解释文字（「以上。」）时 json.loads 会失败，
    # 用 raw_decode 从每个 { 试一次 —— 它解析完一个对象就停，不管后面。
    decoder = json.JSONDecoder()
    for start in [i for i, ch in enumerate(cleaned) if ch == "{"][:20]:
        try:
            obj, _end = decoder.raw_decode(cleaned[start:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return _as_flow(obj)

    raise ValueError("模型返回的不是 JSON 对象")


def _validation_errors(exc: ValidationError) -> list[str]:
    """把 pydantic 的错误压成人话 —— 这些会回喂给模型，也会给用户看。"""
    out: list[str] = []
    for item in exc.errors()[:MAX_ERRORS_PER_TURN]:
        loc = ".".join(str(p) for p in item.get("loc", ()))
        msg = str(item.get("msg") or "格式不对")
        out.append(f"{loc}：{msg}" if loc else msg)
    return out


def _system_prompt(spec: dict) -> str:
    """系统提示词 —— 规则 + 规格。

    **规格放进提示词而不是让模型查接口**：一次调用就能拿到全部约束，
    少一轮工具调用、少一次失败机会。规格本身从代码生成（agent_spec），
    节点增删自动同步，不存在「文档过期」。
    """
    spec_json = json.dumps(spec, ensure_ascii=False, indent=1)
    return f"""你是「雨范AI工作台」的流程编排助手。你的唯一任务：把内容团队的自然语言需求，翻译成这个系统能执行的流程配置。

流程配置是**纯 JSON 数据，不是代码**。生成后会被程序校验、试跑，再由人审批上线。所以：
- 只输出数据，不写函数、不写注释、不写 Markdown。
- 只用下面「编排规格」里真实存在的节点 id 和字段名。不要编造，不要猜。
- 拿不准的地方宁可少一步，并在 note 里写清楚需要人工确认什么。

编排规格（可用节点、表达式语法、函数、规则）：
{spec_json}

输出必须是一个 JSON 对象，结构如下（字段都可选，但 name 和 steps 必须有）：
{{
  "name": "简短流程名，让人一眼看出干什么（不超过 30 字）",
  "description": "一句话说明这条流程做什么",
  "category": "image",
  "icon": "",
  "input_schema": {{"fields": [
    {{"name": "prompt", "label": "提示词", "type": "textarea", "required": true, "default": ""}}
  ]}},
  "outputs": [{{"name": "image", "from": "s2", "pick": "all"}}],
  "steps": [
    {{"id": "s1", "title": "生成参考图", "type": "template", "template_id": "真实节点id",
      "params": {{"prompt": "{{{{ input.prompt }}}}"}}}}
  ],
  "note": "给审批人看的说明：为什么这样串、哪里需要人工确认"
}}

硬性规则（违反任何一条都会被校验打回）：
1. steps 按执行顺序排列，id 用 s1、s2…；params 里引用上游产物写 {{{{ steps.s1.outputs }}}}，只要一张写 {{{{ steps.s1.outputs.first }}}} 或 .last。
2. input_schema.fields 里声明的字段，才能用 {{{{ input.字段名 }}}} 引用。字段类型只能是 text / textarea / number / select / boolean / file_upload。
3. 节点的文件类字段（accepts_output=true）必须用表达式取值（流程输入或上游产物），不能填字面量。
4. 普通字段不能接上游产物；只有文件字段能接。
5. 不能有循环依赖：只能引用排在前面的步骤。
6. 循环步骤 type=repeat 时，count 用 0 表示按列表长度跑，此时参数里必须出现 {{{{ input.列表字段[index] }}}}。
7. 子流程步骤 type=flow 时填 flow_id，不能调用自己。
8. 不要输出解释文字、不要用 ```json 围栏。只输出一个 JSON 对象。

写 params 之前，逐项确认：这个字段在节点 fields 里存在吗？类型对得上吗？引用的流程输入声明过吗？引用的步骤在前面吗？"""


def _user_prompt(requirement: str) -> str:
    return f"内容团队的需求：\n{requirement.strip()}\n\n请输出完整流程配置 JSON。"


def _repair_prompt(errors: list[str], previous: str) -> str:
    listed = "\n".join(f"- {e}" for e in errors[:MAX_ERRORS_PER_TURN])
    echoed = previous[:MAX_ECHO_CHARS]
    return f"""这份配置没通过校验：
{listed}

请修正后只返回完整的 JSON 对象（不要解释、不要代码围栏）。
你上次的输出是：
{echoed}"""


async def _chat(
    messages: list[dict],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """调一次 /chat/completions，返回文本内容。

    `response_format=json_object` 不是所有兼容层都支持（有的直接 400）。
    先带它请求 —— 支持的话模型更守规矩；被拒就摘掉重发一次。
    这比让部署方去猜「我的模型支不支持 JSON 模式」现实。
    """
    if not is_configured():
        raise AgentLLMError(
            "还没有配置编排模型（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）。"
            "让部署同学配好再用，或先用人工编排。"
        )

    payload: dict[str, Any] = {
        "model": settings.LLM_MODEL.strip(),
        "messages": messages,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if settings.LLM_API_KEY.strip():
        headers["Authorization"] = f"Bearer {settings.LLM_API_KEY.strip()}"

    url = endpoint_url()
    async with httpx.AsyncClient(
        timeout=settings.LLM_TIMEOUT,
        trust_env=settings.LLM_TRUST_ENV,
        transport=transport,
    ) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 400 and "response_format" in resp.text:
                payload.pop("response_format", None)
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise AgentLLMError(
                f"模型响应超时（{settings.LLM_TIMEOUT:g} 秒）。"
                "需求太长或模型太慢，可以缩短需求再试"
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentLLMError(f"连不上模型服务：{exc}") from exc

    if resp.status_code >= 400:
        # 别把整段响应体塞给用户：可能含上游的内部信息，也可能很长
        detail = resp.text.strip().replace("\n", " ")[:200]
        raise AgentLLMError(f"模型服务返回 {resp.status_code}：{detail}")

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AgentLLMError("模型服务返回的结构不是 OpenAI 兼容格式") from exc

    if not isinstance(content, str) or not content.strip():
        raise AgentLLMError("模型返回了空内容，稍后再试或换一个模型")
    return content


async def generate_flow_draft(
    requirement: str,
    spec: dict,
    templates: dict,
    flows: dict[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    max_attempts: int | None = None,
) -> GenerationResult:
    """把需求生成成流程配置（**还没落库**）。

    templates 与 flows 是校验用的真实数据（active 节点、可调用流程），
    必须由调用方从数据库取 —— 这个模块不碰数据库，方便单测。
    """
    requirement = (requirement or "").strip()
    if not requirement:
        return GenerationResult(ok=False, errors=["需求不能为空"], message="需求不能为空")

    attempts_limit = max(1, int(max_attempts or settings.LLM_MAX_ATTEMPTS))
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(spec)},
        {"role": "user", "content": _user_prompt(requirement)},
    ]

    attempts: list[dict] = []
    last_errors: list[str] = []

    for index in range(1, attempts_limit + 1):
        raw = await _chat(messages, transport=transport)

        try:
            parsed = extract_json_object(raw)
        except ValueError as exc:
            last_errors = [str(exc)]
            attempts.append({"attempt": index, "stage": "json", "errors": last_errors})
            messages.extend(
                [
                    {"role": "assistant", "content": raw[:MAX_ECHO_CHARS]},
                    {
                        "role": "user",
                        "content": "你上次的输出不是合法 JSON 对象。只输出一个 JSON 对象，不要任何其它文字。",
                    },
                ]
            )
            continue

        try:
            payload = AgentDraftRequest.model_validate(parsed)
        except ValidationError as exc:
            last_errors = _validation_errors(exc)
            attempts.append({"attempt": index, "stage": "schema", "errors": last_errors})
            messages.extend(
                [
                    {"role": "assistant", "content": raw[:MAX_ECHO_CHARS]},
                    {"role": "user", "content": _repair_prompt(last_errors, raw)},
                ]
            )
            continue

        if not payload.steps:
            last_errors = ["流程至少要有一个步骤（steps 不能为空）"]
            attempts.append({"attempt": index, "stage": "semantic", "errors": last_errors})
            messages.extend(
                [
                    {"role": "assistant", "content": raw[:MAX_ECHO_CHARS]},
                    {"role": "user", "content": _repair_prompt(last_errors, raw)},
                ]
            )
            continue

        result = validate_flow_config(
            steps=[s.model_dump() for s in payload.steps],
            input_schema=payload.input_schema,
            outputs=[o.model_dump() for o in payload.outputs],
            templates=templates,
            flows=flows,
        )
        if not result["ok"]:
            last_errors = result["errors"]
            attempts.append(
                {"attempt": index, "stage": "validate", "errors": last_errors[:MAX_ERRORS_PER_TURN]}
            )
            messages.extend(
                [
                    {"role": "assistant", "content": raw[:MAX_ECHO_CHARS]},
                    {"role": "user", "content": _repair_prompt(last_errors, raw)},
                ]
            )
            continue

        attempts.append({"attempt": index, "stage": "ok", "errors": []})
        return GenerationResult(
            ok=True,
            # 统一成内部口径（from_step），和编排器保存的流程一致；
            # 接口序列化时再按 FlowOutput 的字段名输出
            config=payload.model_dump(),
            note=payload.note,
            warnings=result["warnings"],
            layers=result["layers"],
            attempts=attempts,
            model=settings.LLM_MODEL.strip(),
            message=f"已生成配置（第 {index} 次尝试通过校验）",
        )

    logger.info("Agent 生成未通过：%s", last_errors[:MAX_ERRORS_PER_TURN])
    return GenerationResult(
        ok=False,
        errors=last_errors[:MAX_ERRORS_PER_TURN],
        attempts=attempts,
        model=settings.LLM_MODEL.strip(),
        message=f"试了 {attempts_limit} 次仍没生成出合法配置，把需求说得更具体一点再试",
    )
