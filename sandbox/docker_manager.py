"""
    加固容器的创建/seed/销毁

    runtime 参数是隔离级别开关：runc(加固容器) / runsc(gVisor) / kata(microVM)。
    换 runtime 就换隔离级别，DockerSandbox 代码不变。
    这里用 asyncio.create_subprocess_exec 管容器生命周期(应用自己的异步逻辑)。
"""
from __future__ import annotations

import time
import asyncio
import uuid
from pathlib import Path

from infra.settings import get_settings
from infra.logging import get_logger
from sandbox.docker_sandbox import DockerSandbox
from obs.metrics import SANDBOX_CREATE_DURATION

logger = get_logger()


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return (proc.returncode if proc.returncode is not None else -1,
            out.decode("utf-8", errors="replace"))


async def create_one_sandbox() -> DockerSandbox:
    """以加固参数起一个常驻容器，返回 DockerSandbox。"""

    start = time.perf_counter()
    s = get_settings()
    name = f"yukiclaw-sbx-{uuid.uuid4().hex[:12]}"
    workdir = s.sandbox_workdir

    # tmpfs 工作目录：可写 + 可执行 + 限大小 + 属主对齐非 root 用户(uid=1000)
    tmpfs_opt = f"{workdir}:rw,exec,size=512m,uid=1000"

    code, out = await _run([
        "docker", "run", "-d", "--name", name,
        "--runtime", s.sandbox_runtime,             # ← 换它即换隔离级别
        "--network", "none",                        # 断网
        "--cap-drop", "ALL",                        # 丢能力
        "--security-opt", "no-new-privileges",      # 禁提权
        "--read-only",                              # 只读根
        "--tmpfs", tmpfs_opt,                       # 唯一可写区
        "--tmpfs", "/tmp:rw,exec,size=128m,uid=1000",  # 额外给 /tmp 可写(部分工具需要)
        "--memory", s.sandbox_mem_limit,
        "--memory-swap", s.sandbox_mem_limit,
        "--pids-limit", str(s.sandbox_pids_limit),
        "--cpus", s.sandbox_cpus,
        "--user", "1000:1000",                      # 非 root
        "-w", workdir,
        s.sandbox_image, "sleep", "infinity",       # 常驻，等 docker exec
    ])
    if code != 0:
        raise RuntimeError(f"起沙箱容器失败：{out}")
    logger.info("加固容器已起：{}（runtime={}）", name, s.sandbox_runtime)

    SANDBOX_CREATE_DURATION.observe(time.perf_counter() - start)   # 埋点
    
    return DockerSandbox(container_id=name, workdir=workdir)


# （reviewer 跑在沙箱里）下，FilesystemPermission 基本是摆设（execute 能绕过），只读必须靠容器只读挂载。
async def create_readonly_sandbox(source_sandbox: DockerSandbox) -> DockerSandbox:
    """给 reviewer 起一个只读容器：把 source_sandbox 的工作目录内容复制进来后设为只读。

    实现：① 起一个新容器；② 从源容器把代码 cp 出来再 cp 进新容器；
         ③ 在新容器内 chmod -R a-w 工作目录,使 execute 跑 echo>file 也写不进。
    这样 reviewer 即使用 execute 也改不了代码——靠文件系统权限,不靠 prompt。
    """
    import tempfile
    s = get_settings()
    name = f"yukiclaw-ro-{uuid.uuid4().hex[:12]}"
    workdir = s.sandbox_workdir
    code, out = await _run([
        "docker", "run", "-d", "--name", name,
        "--runtime", s.sandbox_runtime,
        "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--read-only",
        "--tmpfs", f"{workdir}:rw,exec,size=512m,uid=1000",
        "--memory", s.sandbox_mem_limit, "--memory-swap", s.sandbox_mem_limit,
        "--pids-limit", str(s.sandbox_pids_limit), "--cpus", s.sandbox_cpus,
        "--user", "1000:1000", "-w", workdir,
        s.sandbox_image, "sleep", "infinity",
    ])
    if code != 0:
        raise RuntimeError(f"起只读沙箱失败：{out}")
    ro = DockerSandbox(container_id=name, workdir=workdir)
    # 把源容器代码搬进来(经宿主中转),然后把工作目录设为不可写
    with tempfile.TemporaryDirectory() as tmpd:
        await _run(["docker", "cp", f"{source_sandbox.container_id}:{workdir}/.", tmpd])
        await _run(["docker", "cp", f"{tmpd}/.", f"{name}:{workdir}"])
    # chmod 去掉写位(用 root 改,因为非 root 改不动自己没权限的位)
    await _run(["docker", "exec", "-u", "0", name, "chmod", "-R", "a-w", workdir])
    logger.info("只读 reviewer 沙箱已起：{}", name)
    return ro



async def seed_project(
    sandbox: DockerSandbox,
    root: Path | None = None,
    subdirs: tuple[str, ...] = ("app", "tests", "skills"),
    extra_files: tuple[str, ...] = ("AGENTS.md",),
) -> None:
    """把宿主项目 seed 进容器工作目录（docker cp）。

    不 seed 的话沙箱是空的，coder 写不进文件、skills 报 path_not_found。
    """
    root = root or Path(__file__).resolve().parent.parent
    cid = sandbox.container_id
    workdir = sandbox.workdir
    for sub in subdirs:
        p = root / sub
        if p.exists():
            await _run(["docker", "cp", str(p), f"{cid}:{workdir}/{sub}"])
    for f in extra_files:
        fp = root / f
        if fp.is_file():
            await _run(["docker", "cp", str(fp), f"{cid}:{workdir}/{f}"])
    # seed 进来的文件属主可能是 root，统一改成 agent(1000)，否则非 root 进程改不动
    await _run(["docker", "exec", "-u", "0", cid, "chown", "-R", "1000:1000", workdir])
    logger.info("项目已 seed 进容器 {}", cid)


async def destroy_sandbox(sandbox: DockerSandbox) -> None:
    """销毁容器（用完即弃）。tmpfs 工作目录随容器一起消失，不残留。"""
    await _run(["docker", "rm", "-f", sandbox.container_id])
    logger.info("容器已销毁：{}", sandbox.container_id)