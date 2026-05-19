"""
Titan-1 TitanSand VM
Creates and manages isolated Docker containers as sandboxes.
Supports air-gapped, proxy-only, and full network modes.
"""

import os
import uuid
import logging
import textwrap
from typing import Optional, Literal
from contextlib import asynccontextmanager
from datetime import datetime

import docker
from docker.errors import DockerException, NotFound, APIError
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("titansand_vm")

DOCKER_API_VERSION = os.getenv("DOCKER_API_VERSION", "1.44")

# Resource limits
DEFAULT_CPU_PERIOD  = 100_000   # 100 ms
DEFAULT_CPU_QUOTA   = 50_000    # 50% of one core
DEFAULT_MEM_LIMIT   = "512m"
DEFAULT_MEM_SWAP    = "512m"    # no swap beyond mem
DEFAULT_TIMEOUT_SEC = 30

# Sandboxes in-process registry: sandbox_id -> container_id
_SANDBOX_REGISTRY: dict[str, dict] = {}


#  Inline Dockerfiles for sandbox images 

DOCKERFILE_CODE = textwrap.dedent("""\
    FROM python:3.11-slim
    RUN apt-get update && apt-get install -y nodejs npm curl && rm -rf /var/lib/apt/lists/*
    RUN pip install --no-cache-dir requests numpy
    WORKDIR /workspace
    CMD ["sleep", "infinity"]
""")

DOCKERFILE_RESEARCH = textwrap.dedent("""\
    FROM python:3.11-slim
    RUN apt-get update && apt-get install -y wget gnupg curl && rm -rf /var/lib/apt/lists/*
    RUN pip install --no-cache-dir playwright requests
    RUN playwright install-deps chromium || true
    WORKDIR /workspace
    CMD ["sleep", "infinity"]
""")

SANDBOX_IMAGES = {
    "code":     ("titan_sandbox_code:latest",     DOCKERFILE_CODE),
    "research": ("titan_sandbox_research:latest", DOCKERFILE_RESEARCH),
}


def get_docker_client() -> docker.DockerClient:
    try:
        return docker.DockerClient(base_url="npipe:////./pipe/docker_engine", version=DOCKER_API_VERSION) # Adjusted for Windows standard
    except DockerException as e:
        logger.error("Docker connection failed: %s", e)
        raise HTTPException(status_code=503, detail=f"Docker daemon unreachable: {str(e)}")


def ensure_image(client: docker.DockerClient, sandbox_type: str) -> str:
    """Build the sandbox image if it doesn't exist yet."""
    if sandbox_type not in SANDBOX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Unknown sandbox_type '{sandbox_type}'. Choose: {list(SANDBOX_IMAGES)}")

    image_tag, dockerfile_content = SANDBOX_IMAGES[sandbox_type]

    try:
        client.images.get(image_tag)
        logger.info("Image %s already exists", image_tag)
        return image_tag
    except docker.errors.ImageNotFound:
        pass

    logger.info("Building image %s ...", image_tag)
    import io, tarfile

    # Build image from inline Dockerfile using BytesIO tar stream
    dockerfile_bytes = dockerfile_content.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="Dockerfile")
        info.size = len(dockerfile_bytes)
        tar.addfile(info, io.BytesIO(dockerfile_bytes))
    buf.seek(0)

    image, _ = client.images.build(fileobj=buf, tag=image_tag, custom_context=True, rm=True)
    logger.info("Built image %s", image_tag)
    return image_tag


def network_config(mode: str) -> dict:
    """Return Docker network settings based on isolation mode."""
    if mode == "air_gapped":
        return {"network_mode": "none"}
    elif mode == "proxy_only":
        # Uses default bridge but with no external routing; caller manages proxy separately
        return {"network_mode": "bridge"}
    else:  # full
        return {"network_mode": "bridge"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("TitanSand VM starting up")
    yield
    # On shutdown, stop all tracked containers
    try:
        client = get_docker_client()
        for sid, info in list(_SANDBOX_REGISTRY.items()):
            try:
                c = client.containers.get(info["container_id"])
                c.stop(timeout=3)
                c.remove(force=True)
                logger.info("Cleaned up sandbox %s", sid)
            except Exception:
                pass
    except Exception:
        pass
    logger.info("TitanSand VM shut down")


app = FastAPI(title="Titan-1 TitanSand VM", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


#  Schemas 

class CreateSandboxRequest(BaseModel):
    sandbox_type: Literal["code", "research"] = Field(default="code")
    network_mode: Literal["air_gapped", "proxy_only", "full"] = Field(default="air_gapped")
    cpu_quota: Optional[int] = Field(default=DEFAULT_CPU_QUOTA, description="CPU quota in microseconds per period")
    mem_limit: Optional[str] = Field(default=DEFAULT_MEM_LIMIT, description="Memory limit e.g. '256m'")
    labels: Optional[dict] = Field(default=None)

class CreateSandboxResponse(BaseModel):
    sandbox_id: str
    container_id: str
    sandbox_type: str
    network_mode: str
    status: str
    created_at: str

class ExecuteRequest(BaseModel):
    sandbox_id: str
    command: str
    timeout: int = Field(default=DEFAULT_TIMEOUT_SEC, ge=1, le=300)
    workdir: str = Field(default="/workspace")

class ExecuteResponse(BaseModel):
    sandbox_id: str
    command: str
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_ms: float

class DestroyRequest(BaseModel):
    sandbox_id: str


#  Routes 

@app.get("/health")
def health():
    return {"status": "ok", "service": "titansand_vm", "active_sandboxes": len(_SANDBOX_REGISTRY)}


@app.post("/sandboxes/create", response_model=CreateSandboxResponse)
def create_sandbox(req: CreateSandboxRequest):
    client = get_docker_client()
    image_tag = ensure_image(client, req.sandbox_type)
    net_cfg   = network_config(req.network_mode)

    sandbox_id = str(uuid.uuid4())
    labels = {
        "titan.sandbox_id":   sandbox_id,
        "titan.sandbox_type": req.sandbox_type,
        **(req.labels or {}),
    }

    try:
        container = client.containers.run(
            image_tag,
            detach=True,
            remove=False,
            cpu_period=DEFAULT_CPU_PERIOD,
            cpu_quota=req.cpu_quota,
            mem_limit=req.mem_limit,
            memswap_limit=req.mem_limit,   # no extra swap
            read_only=False,
            labels=labels,
            **net_cfg,
        )
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Docker run failed: {e.explanation}")

    _SANDBOX_REGISTRY[sandbox_id] = {
        "container_id": container.id,
        "sandbox_type": req.sandbox_type,
        "network_mode": req.network_mode,
        "created_at":   datetime.utcnow().isoformat(),
    }

    logger.info("Created sandbox %s -> container %s", sandbox_id, container.short_id)

    return CreateSandboxResponse(
        sandbox_id=sandbox_id,
        container_id=container.id,
        sandbox_type=req.sandbox_type,
        network_mode=req.network_mode,
        status="running",
        created_at=_SANDBOX_REGISTRY[sandbox_id]["created_at"],
    )


@app.post("/sandboxes/execute", response_model=ExecuteResponse)
def execute_in_sandbox(req: ExecuteRequest):
    import time

    if req.sandbox_id not in _SANDBOX_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Sandbox '{req.sandbox_id}' not found")

    info   = _SANDBOX_REGISTRY[req.sandbox_id]
    client = get_docker_client()

    try:
        container = client.containers.get(info["container_id"])
    except NotFound:
        del _SANDBOX_REGISTRY[req.sandbox_id]
        raise HTTPException(status_code=404, detail="Container no longer exists")

    # Refresh container state
    container.reload()
    if container.status != "running":
        raise HTTPException(status_code=409, detail=f"Container status is '{container.status}', not running")

    t0 = time.perf_counter()
    timed_out = False

    try:
        exit_code, output = container.exec_run(
            cmd=["sh", "-c", req.command],
            workdir=req.workdir,
            demux=True,      # separate stdout / stderr
            stdin=False,
        )
        stdout_bytes, stderr_bytes = output if output else (b"", b"")
        stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"exec_run failed: {e.explanation}")

    duration_ms = (time.perf_counter() - t0) * 1000

    logger.info("Sandbox %s exec exit=%d %.1fms", req.sandbox_id, exit_code, duration_ms)

    return ExecuteResponse(
        sandbox_id=req.sandbox_id,
        command=req.command,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=round(duration_ms, 2),
    )


@app.post("/sandboxes/destroy")
def destroy_sandbox(req: DestroyRequest):
    if req.sandbox_id not in _SANDBOX_REGISTRY:
        raise HTTPException(status_code=404, detail=f"Sandbox '{req.sandbox_id}' not found")

    info   = _SANDBOX_REGISTRY.pop(req.sandbox_id)
    client = get_docker_client()

    try:
        container = client.containers.get(info["container_id"])
        container.stop(timeout=5)
        container.remove(force=True)
        logger.info("Destroyed sandbox %s", req.sandbox_id)
    except NotFound:
        pass  # Already gone
    except APIError as e:
        raise HTTPException(status_code=500, detail=f"Failed to destroy container: {e.explanation}")

    return {"sandbox_id": req.sandbox_id, "status": "destroyed"}


@app.get("/sandboxes")
def list_sandboxes():
    return {
        "count": len(_SANDBOX_REGISTRY),
        "sandboxes": [
            {"sandbox_id": sid, **info}
            for sid, info in _SANDBOX_REGISTRY.items()
        ],
    }
