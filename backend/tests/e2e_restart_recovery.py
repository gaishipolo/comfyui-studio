"""重启恢复端到端实测：跑一半把后端杀掉，看任务是被接回去跑完，还是卡死在 running。

这测的是「尽量接回去跑完」那条策略 —— 后台执行是进程内 asyncio 协程，进程一没，
没有 reap_stale_tasks 接管的话任务就永远停在 running，前端只能一直转圈。

流程：
  1. 用测试账号提交一个真实生成任务
  2. **同一个进程里**紧盯着，一等到「已拿到 prompt_id 且还在跑」就立刻杀掉后端
     （这一段必须零人工延迟：1344² 的图十几秒就跑完，中间隔一轮对话就错过窗口了）
  3. 重启后端，启动日志应当出现「启动收拾遗留任务：接回 1 条」
  4. 等任务出结果：必须走到 done，并且素材真的落了盘

用法（先起好后端与 ComfyUI，再跑本脚本；脚本会自己杀掉并重启后端）：
    python tests/e2e_restart_recovery.py [--user e2e-recover] [--pass E2ePass!2026]

脚本不负责建账号：默认账号 e2e-recover 由一次性脚本建好；传 --user/--pass 可换。
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
PYTHON = sys.executable
BACKEND_DIR = str(Path(__file__).resolve().parents[1])  # .../backend


def api(method, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_detail": e.read().decode()}
    except urllib.error.URLError as e:
        # 后端被杀掉后就是这种：连不上不等于出错，如实上报给调用方判断
        return {"_status": 0, "_detail": f"连不上后端: {e.reason}"}


def run_cmd(cmd: str) -> str:
    """跑一条命令拿文本输出。

    别用 text=True —— 中文 Windows 上 netstat/taskkill 的输出是 GBK，
    按 UTF-8 解码会在子线程里炸 UnicodeDecodeError，且 stdout 变成 None，
    等到主流程再用就是 'NoneType' has no attribute 'splitlines'。
    统一字节收、容错解码，谁的输出都不至于把脚本带崩。
    """
    raw = subprocess.run(cmd, shell=True, capture_output=True).stdout
    return raw.decode("gbk", errors="replace")


def listen_pid() -> int | None:
    """找出正在 8000 端口监听的进程号。"""
    for line in run_cmd("netstat -ano").splitlines():
        if ":8000" in line and "LISTENING" in line:
            return int(line.split()[-1])
    return None


def tid_salt() -> str:
    """每次跑给 prompt 加的盐，防 ComfyUI 命中 prompt 缓存。"""
    import uuid
    return uuid.uuid4().hex[:8]


def db_task_status(task_id: str) -> tuple[str, str]:
    """直接读库看任务状态（后端被杀后 HTTP 是查不了的，只能读库）。"""
    import sqlite3
    con = sqlite3.connect(str(Path(BACKEND_DIR) / "workbench.db"))
    try:
        row = con.execute(
            "SELECT status, provider_task_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return (row[0], row[1]) if row else ("<没有这条>", "")
    finally:
        con.close()


def start_backend(log_path: str) -> subprocess.Popen:
    """起后端，stdout/stderr 落文件，方便看启动时那句「接回 N 条」。"""
    import os
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CODEBUDDY_SAFE_DELETE") and k != "CODEBUDDY_TOOL_CALL_ID"}
    log = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=BACKEND_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def wait_healthy(timeout=30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if api("GET", "/api/health").get("status") == "ok":
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="e2e-recover")
    ap.add_argument("--pass", dest="password", default="E2ePass!2026")
    ap.add_argument("--template", default="5c39362be528449aab8e704bcb49974b")  # Flux2文生图
    ap.add_argument("--log", default="restart_recovery_backend.log")
    args = ap.parse_args()

    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))

    # ── 0. 登录 ───────────────────────────────────────────
    auth = api("POST", "/api/auth/login", body={"username": args.user, "password": args.password})
    if "access_token" not in auth:
        print(f"登录失败：{auth}")
        return 1
    token = auth["access_token"]
    print(f"已登录 {args.user}")

    # ── 1. 提交任务 ───────────────────────────────────────
    # prompt 必须每次唯一：ComfyUI 对**完全相同**的 prompt 命中缓存直接返回结果，
    # 0.6 秒就 done，根本抓不到「跑一半」的窗口（第一版就栽在这上面）。
    probe = f"restart recovery probe {time.time():.3f} {tid_salt()}"
    tasks = api("POST", "/api/tasks", token, {
        "template_id": args.template,
        "params": {"prompt": probe, "width": 1344, "height": 1344},
        "batch_count": 1,
    })
    if isinstance(tasks, dict) and tasks.get("_status"):
        print(f"提交失败：{tasks}")
        return 1
    tid = tasks[0]["id"]
    print(f"任务已提交：{tid}")

    # ── 2. 抓窗口并杀后端（必须在同一进程内，零人工延迟）──
    killed = None
    for _ in range(600):
        t = api("GET", f"/api/tasks/{tid}", token)
        if t.get("provider_task_id") and t.get("status") == "running":
            pid = listen_pid()
            if pid:
                run_cmd(f"taskkill /PID {pid} /F")
                killed = (pid, t["provider_task_id"], t["progress"])
            break
        if t.get("status") in ("done", "error", "cancelled"):
            break
        time.sleep(0.1)

    if not killed:
        print("没抓到「已提交且在跑」的窗口（生成太快或提交失败），换个慢一点的模板再试")
        return 1
    pid, prompt_id, progress = killed
    print(f"已在 progress={progress} 时杀掉后端 PID={pid}，prompt_id={prompt_id[:12]}…")

    time.sleep(1)
    # 后端已经死了，只能读库确认僵尸任务已形成（拿 HTTP 查只会得到「连不上」）
    st, db_prompt = db_task_status(tid)
    check("杀掉后端后任务卡在 running（僵尸任务已形成）", st == "running", st)
    check("库里留着它的 prompt_id（重启后靠这个接回去）",
          db_prompt.startswith(prompt_id[:12]), db_prompt[:12])

    # ── 3. 重启后端，看它认不认这条 ───────────────────────
    proc = start_backend(args.log)
    check("后端重新起来了", wait_healthy())

    # 启动日志必须说明收拾了几条 —— 打不出来就说明 reap 根本没跑
    log_text = open(args.log, encoding="utf-8", errors="replace").read()
    check("启动日志出现「启动收拾遗留任务」", "启动收拾遗留任务" in log_text)
    check("日志说接回了 1 条", "接回 1 条" in log_text)

    # ── 4. 等它跑完 ───────────────────────────────────────
    final = None
    for _ in range(240):
        t = api("GET", f"/api/tasks/{tid}", token)
        if t.get("status") in ("done", "error", "cancelled"):
            final = t
            break
        time.sleep(2)

    if not final:
        check("任务最终出了结果", False, "等了 8 分钟还在跑/卡住")
    else:
        check("接回后任务跑到 done", final["status"] == "done", final["status"])
        check("prompt_id 从头到尾是同一个（没重新提交）",
              final["provider_task_id"] == prompt_id,
              final["provider_task_id"][:12])
        outputs = (final.get("result") or {}).get("outputs") or []
        check("产物收下来了（result.outputs 非空）", len(outputs) > 0, f"{len(outputs)} 个")

    print()
    print("ALL PASS" if ok else "SOME FAILED")
    print(f"（后端保持运行：PID={proc.pid}，日志 {args.log}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
