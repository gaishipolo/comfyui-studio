"""流程的导出与导入。

**为什么流程导出比模板导出复杂**：流程不只有自己的配置，它还**引用**别的东西 ——
节点（template_id）和子流程（flow_id）。导到另一台机器上，这些引用可能都不存在。

所以导出包要带上引用的快照，导入时按这个规则解析：

    本机有同 id 的 → 用本机的（对方可能已经改过，用本机的才是「当前真相」）
    本机没有     → 按快照创建（这才让流程能跨机器搬）

子流程是**递归**的：子流程里还有节点，还可能还有子流程。所以要一路收集到底。

**不导出运行记录和素材** —— 那是运行时产生的东西，不属于「流程配置」。
"""
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Flow, Template

FLOW_EXPORT_FORMAT = "comfyui-workbench/flow"
FLOW_EXPORT_VERSION = 1

# 子流程收集的最大深度 —— 防流程互相调用形成无限递归。
# 与执行器的 MAX_SUBFLOW_DEPTH 对齐。
_MAX_EXPORT_DEPTH = 6


async def collect_references(
    db: AsyncSession,
    flow: Flow,
) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """收集一条流程引用的节点与子流程（含递归）。

    返回 (节点快照 {id: 快照}, 子流程快照 {id: 快照}, 缺失列表)。

    **缺失的引用要报出来**：引用的节点/子流程在本机已经不存在了（比如
    被人删了），导出的包会残缺 —— 导入到别的机器上得到一个「空壳」流程，
    能创建、能跑，但什么都不产出。宁可导出时就说清楚。
    """
    templates: dict[str, dict] = {}
    flows: dict[str, dict] = {}
    missing: list[str] = []
    await _collect(db, flow, templates, flows, missing, depth=0, seen_flows={flow.id})
    return templates, flows, missing


async def _collect(
    db: AsyncSession,
    flow: Flow,
    templates: dict[str, dict],
    flows: dict[str, dict],
    missing: list[str],
    depth: int,
    seen_flows: set[str],
) -> None:
    """递归收集一条流程引用的节点和子流程。"""
    if depth > _MAX_EXPORT_DEPTH:
        return

    for step in list(flow.steps or []):
        template_id = str(step.get("template_id") or "")
        if template_id and template_id not in templates:
            tpl = await db.get(Template, template_id)
            if tpl is not None:
                templates[template_id] = _template_snapshot(tpl)
            elif template_id not in missing:
                missing.append(f"节点 {template_id}")

        sub_id = str(step.get("flow_id") or "")
        if sub_id and sub_id not in flows and sub_id not in seen_flows:
            sub = await db.get(Flow, sub_id)
            if sub is None:
                if sub_id not in missing:
                    missing.append(f"子流程 {sub_id}")
                continue
            flows[sub_id] = _flow_snapshot(sub)
            # 递归：子流程里还有节点，还可能还有子流程
            seen_flows.add(sub_id)
            await _collect(db, sub, templates, flows, missing, depth + 1, seen_flows)


def _template_snapshot(tpl: Template) -> dict:
    """节点快照 —— 够在别的机器上重建一个能用的节点。

    **含 id**：导入时按「本机有同 id 的就用本机的」这条规则解析。
    （不是要保留 id，而是要拿它当匹配键。）

    **内嵌工作流 JSON**：ComfyUI 节点的「配置」有一半在磁盘上的工作流文件里
    —— 不带过去，导到别的机器上就是一个跑不了的空壳
    （`ComfyUI engine requires a workflow file`）。
    所以这里把工作流内容读出来塞进快照，导入时再写回磁盘。
    """
    from app.api.templates import _read_workflow_for_export
    from app.schemas.schemas import normalize_input_schema

    snapshot = {
        "id": tpl.id,
        "name": tpl.name,
        "description": tpl.description,
        "category": tpl.category,
        "icon": tpl.icon,
        "engine": tpl.engine,
        "capability": tpl.capability,
        "input_schema": normalize_input_schema(tpl.input_schema),
        "param_mapping": tpl.param_mapping,
        "output_config": tpl.output_config,
        "is_active": tpl.is_active,
        "sort_order": tpl.sort_order,
        "workflow_file": tpl.workflow_file,
    }
    workflow = _read_workflow_for_export(tpl.workflow_file)
    if workflow is not None:
        snapshot["workflow"] = workflow
    return snapshot


def _flow_snapshot(flow: Flow) -> dict:
    """子流程快照。"""
    from app.schemas.schemas import normalize_input_schema

    return {
        "id": flow.id,
        "name": flow.name,
        "description": flow.description,
        "category": flow.category,
        "icon": flow.icon,
        "input_schema": normalize_input_schema(flow.input_schema),
        "outputs": list(flow.outputs or []),
        "steps": list(flow.steps or []),
        "status": flow.status,
    }


def build_export_doc(
    flow: Flow,
    templates: dict[str, dict],
    flows: dict[str, dict],
    exported_at: str,
) -> dict:
    """组装导出文件。"""
    from app.schemas.schemas import normalize_input_schema

    return {
        "format": FLOW_EXPORT_FORMAT,
        "version": FLOW_EXPORT_VERSION,
        "exported_at": exported_at,
        "source": {
            "flow_id": flow.id,
            "updated_at": flow.updated_at.isoformat(timespec="seconds") if flow.updated_at else "",
        },
        "flow": {
            "name": flow.name,
            "description": flow.description,
            "category": flow.category,
            "icon": flow.icon,
            "input_schema": normalize_input_schema(flow.input_schema),
            "outputs": list(flow.outputs or []),
            "steps": list(flow.steps or []),
            "status": flow.status,
        },
        # 引用的节点与子流程快照 —— 导入时用来重建引用
        "templates": list(templates.values()),
        "flows": list(flows.values()),
    }


async def resolve_references(
    db: AsyncSession,
    doc: dict,
    created_by: str,
) -> tuple[dict, dict[str, str], list[str]]:
    """把导出包里的引用解析成本机可用的 id。

    返回 (引用映射 {快照id: 本机id}, 警告列表)。

    规则：
    - 本机有**同 id** 的 → 用本机的（对方可能改过，本机的才是当前真相）
    - 本机没有 → 按快照创建，拿到新 id
    """
    warnings: list[str] = []
    id_map: dict[str, str] = {}

    # ── 节点 ──
    for snap in doc.get("templates") or []:
        if not isinstance(snap, dict):
            continue
        old_id = str(snap.get("id") or "")
        name = str(snap.get("name") or "")
        if not name:
            warnings.append("有个节点快照没有名字，跳过了")
            continue

        existing = await db.get(Template, old_id) if old_id else None
        if existing is not None:
            id_map[old_id] = existing.id
            continue

        # 本机有同名的吗？—— 有就复用（避免导入出一堆重名节点）
        same_name = (
            await db.execute(select(Template).where(Template.name == name))
        ).scalars().first()
        if same_name is not None:
            id_map[old_id] = same_name.id
            warnings.append(f"节点「{name}」本机已有同名版本，用的是本机的")
            continue

        # 内嵌的工作流写回磁盘 —— ComfyUI 节点的工作流是文件，不在数据库里
        workflow_file = str(snap.get("workflow_file") or "")
        if snap.get("workflow"):
            from app.api.templates import _save_workflow_dedup

            stem = Path(workflow_file or "workflow").stem or "workflow"
            workflow_file = _save_workflow_dedup(
                snap["workflow"], stem, prefer=workflow_file
            )
        elif not workflow_file:
            # 快照里没有工作流 —— 试试本机磁盘上有没有同名的。
            # （导出的节点本身就没配工作流，但它可能在本机有同名的文件）
            found = _find_workflow_by_param_mapping(snap)
            if found:
                workflow_file = found
                warnings.append(f"节点「{name}」用的是本机已有的工作流文件 {found}")
        tpl = Template(
            name=name,
            description=str(snap.get("description") or ""),
            category=str(snap.get("category") or "image"),
            icon=str(snap.get("icon") or ""),
            engine=str(snap.get("engine") or ""),
            capability=str(snap.get("capability") or ""),
            workflow_file=workflow_file,
            input_schema=snap.get("input_schema") or {},
            param_mapping=snap.get("param_mapping") or {},
            output_config=snap.get("output_config") or {},
            is_active=bool(snap.get("is_active", True)),
            sort_order=int(snap.get("sort_order") or 0),
        )
        db.add(tpl)
        await db.flush()
        id_map[old_id] = tpl.id
        warnings.append(f"节点「{name}」本机没有，按导出包创建了一个")

    # ── 子流程 ──
    # 可能有多层（子流程里还有子流程），而且后一层的 steps 要用前一层的映射重写，
    # 所以按层多轮解析：每一轮把「引用还没解析完」的那些留到下一轮。
    pending = [s for s in (doc.get("flows") or []) if isinstance(s, dict)]
    for _round in range(_MAX_EXPORT_DEPTH + 1):
        if not pending:
            break
        remaining: list[dict] = []
        for snap in pending:
            old_id = str(snap.get("id") or "")
            name = str(snap.get("name") or "")
            if not name:
                warnings.append("有个子流程快照没有名字，跳过了")
                continue

            existing = await db.get(Flow, old_id) if old_id else None
            if existing is not None:
                id_map[old_id] = existing.id
                continue

            same_name = (
                await db.execute(select(Flow).where(Flow.name == name))
            ).scalars().first()
            if same_name is not None:
                id_map[old_id] = same_name.id
                warnings.append(f"子流程「{name}」本机已有同名版本，用的是本机的")
                continue

            # 这一层的 steps 里若还有没解析的引用，先放着，下一轮再创建
            steps_snap = list(snap.get("steps") or [])
            if _has_unresolved(steps_snap, id_map):
                remaining.append(snap)
                continue

            sub = Flow(
                name=name,
                description=str(snap.get("description") or ""),
                category=str(snap.get("category") or "image"),
                icon=str(snap.get("icon") or ""),
                input_schema=snap.get("input_schema") or {},
                outputs=list(snap.get("outputs") or []),
                # 子流程的步骤也要重写引用 —— 它内部同样指向节点
                steps=rewrite_references(steps_snap, id_map),
                status=str(snap.get("status") or "active"),
                created_by=created_by,
            )
            db.add(sub)
            await db.flush()
            id_map[old_id] = sub.id
            warnings.append(f"子流程「{name}」本机没有，按导出包创建了一个")

        if len(remaining) == len(pending):
            # 一轮下来没有任何进展 —— 剩下的引用是环或缺失，硬创建会留坏配置
            for snap in remaining:
                warnings.append(
                    f"子流程「{snap.get('name')}」的引用没解析完，跳过了"
                    "（可能是循环引用或引用了不存在的节点）"
                )
            break
        pending = remaining

    return id_map, warnings


def _find_workflow_by_param_mapping(snap: dict) -> str:
    """按 param_mapping 里的节点路径，在工作流目录里找匹配的工作流文件。

    **为什么需要**：ComfyUI 节点的工作流是磁盘上的文件，不在数据库里。
    导出包没带工作流（导出时那个节点就没配），但本机可能留着同名的文件 ——
    找到就能用上，比建一个跑不了的空壳节点强。

    匹配依据：param_mapping 里的节点路径（如 "76.inputs.value"）在候选工作流
    里也要存在。取匹配最多的那个。
    """
    from app.config import settings

    mapping = snap.get("param_mapping") or {}
    if not isinstance(mapping, dict) or not mapping:
        return ""
    paths = {
        str(v).split(".")[0]
        for v in mapping.values()
        if isinstance(v, str) and "." in v
    }
    if not paths:
        return ""

    best_name = ""
    best_score = 0
    try:
        candidates = list(settings.WORKFLOW_DIR.glob("*.json"))
    except OSError:
        return ""
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        node_ids = {str(k) for k in data.keys()}
        score = len(paths & node_ids)
        if score > best_score:
            best_score = score
            best_name = path.name
    # 至少要匹配上一半，否则宁可不认（认错了更糟）
    if best_score < max(1, len(paths) // 2):
        return ""
    return best_name


def _has_unresolved(steps: list, id_map: dict[str, str]) -> bool:
    """这批步骤里还有引用没被解析吗（引用的 id 不在映射里）。"""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        tid = str(step.get("template_id") or "")
        fid = str(step.get("flow_id") or "")
        if tid and tid not in id_map:
            return True
        if fid and fid not in id_map:
            return True
    return False


def rewrite_references(steps: list, id_map: dict[str, str]) -> list:
    """把步骤里的引用 id 换成本机的 id。

    只改 template_id / flow_id —— 步骤 id 是流程内部的，不跨机器。

    **子流程的步骤也要重写**：子流程里同样指向节点，不改的话导入出来的
    子流程是「空壳」—— 能创建、能跑，但什么都不产出。
    """
    rewritten = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        item = dict(step)
        tid = str(item.get("template_id") or "")
        if tid and tid in id_map:
            item["template_id"] = id_map[tid]
        fid = str(item.get("flow_id") or "")
        if fid and fid in id_map:
            item["flow_id"] = id_map[fid]
        rewritten.append(item)
    return rewritten


def dumps_doc(doc: dict) -> bytes:
    """导出文件的内容。"""
    return json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
