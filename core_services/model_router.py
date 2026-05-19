"""
Titan-1 Model Router
Routes inference requests to Ollama or llama.cpp based on task_type, latency, and cost.
"""

import os
import time
import logging
from typing import Optional, Literal
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("model_router")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
LLAMA_CPP_HOST = os.getenv("LLAMA_CPP_HOST", "http://127.0.0.1:8083")

#  Routing table 
# task_type  preferred backend and model name
ROUTING_TABLE = {
    "chat":        {"backend": "ollama",    "model": "llama3",     "priority": 1},
    "code":        {"backend": "ollama",    "model": "codellama",  "priority": 1},
    "summary":     {"backend": "llama_cpp", "model": "default",    "priority": 2},
    "embedding":   {"backend": "ollama",    "model": "nomic-embed-text", "priority": 1},
    "reasoning":   {"backend": "llama_cpp", "model": "default",    "priority": 2},
    "default":     {"backend": "ollama",    "model": "llama3",     "priority": 1},
}

COST_TABLE = {
    "ollama":    0.0,   # free / local
    "llama_cpp": 0.0,   # free / local
}

LATENCY_TABLE = {
    "ollama":    "medium",
    "llama_cpp": "low",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Model Router starting up")
    yield
    logger.info("Model Router shutting down")


app = FastAPI(
    title="Titan-1 Model Router",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


#  Schemas 

class RouterRequest(BaseModel):
    task_type: str = Field(default="default", description="Type of task: chat, code, summary, embedding, reasoning")
    latency: Optional[Literal["low", "medium", "high"]] = Field(default=None, description="Preferred latency tier")
    cost: Optional[Literal["free", "low", "high"]] = Field(default="free", description="Cost constraint")

class RouterResponse(BaseModel):
    backend: str
    model: str
    config: dict

class InferenceRequest(BaseModel):
    model: str = Field(default="llama3")
    prompt: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=8192)
    backend: Optional[Literal["ollama", "llama_cpp"]] = Field(default=None)

class InferenceResponse(BaseModel):
    text: str
    backend_used: str
    model_used: str
    timing_ms: float
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None


#  Helpers 

def resolve_backend(task_type: str, latency_pref: Optional[str], cost_pref: Optional[str]) -> dict:
    """Pick the best backend given task type and preferences."""
    route = ROUTING_TABLE.get(task_type, ROUTING_TABLE["default"])
    backend = route["backend"]
    model = route["model"]

    # If user explicitly wants low latency, prefer llama_cpp
    if latency_pref == "low":
        backend = "llama_cpp"
        model = "default"

    # Both backends are free; cost doesn't affect routing here
    return {
        "backend": backend,
        "model": model,
        "config": {
            "latency_tier": LATENCY_TABLE[backend],
            "cost_per_token": COST_TABLE[backend],
            "host": OLLAMA_HOST if backend == "ollama" else LLAMA_CPP_HOST,
        }
    }


async def call_ollama(client: httpx.AsyncClient, model: str, prompt: str,
                      temperature: float, max_tokens: int) -> tuple[str, Optional[int], Optional[int]]:
    """Call Ollama /api/generate and return (text, prompt_tokens, completion_tokens)."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        }
    }
    try:
        r = await client.post(f"{OLLAMA_HOST}/api/generate", json=payload, timeout=120.0)
        r.raise_for_status()
        data = r.json()
        text = data.get("response", "")
        p_tok = data.get("prompt_eval_count")
        c_tok = data.get("eval_count")
        return text, p_tok, c_tok
    except httpx.HTTPStatusError as e:
        logger.error("Ollama error: %s", e)
        raise HTTPException(status_code=502, detail=f"Ollama backend error: {e.response.text}")
    except httpx.RequestError as e:
        logger.error("Ollama connection error: %s", e)
        raise HTTPException(status_code=503, detail=f"Ollama unreachable: {str(e)}")


async def call_llama_cpp(client: httpx.AsyncClient, model: str, prompt: str,
                         temperature: float, max_tokens: int) -> tuple[str, Optional[int], Optional[int]]:
    """Call llama.cpp server /v1/completions and return (text, prompt_tokens, completion_tokens)."""
    payload = {
        "prompt": prompt,
        "temperature": temperature,
        "n_predict": max_tokens,
        "stop": [],
    }
    try:
        r = await client.post(f"{LLAMA_CPP_HOST}/v1/completions", json=payload, timeout=120.0)
        r.raise_for_status()
        data = r.json()
        choices = data.get("choices", [{}])
        text = choices[0].get("text", "") if choices else ""
        usage = data.get("usage", {})
        p_tok = usage.get("prompt_tokens")
        c_tok = usage.get("completion_tokens")
        return text, p_tok, c_tok
    except httpx.HTTPStatusError as e:
        logger.error("llama.cpp error: %s", e)
        raise HTTPException(status_code=502, detail=f"llama.cpp backend error: {e.response.text}")
    except httpx.RequestError as e:
        logger.error("llama.cpp connection error: %s", e)
        raise HTTPException(status_code=503, detail=f"llama.cpp unreachable: {str(e)}")


#  Routes 

@app.get("/health")
async def health():
    return {"status": "ok", "service": "model_router"}


@app.post("/models/router", response_model=RouterResponse)
async def route_model(req: RouterRequest):
    """Determine which backend and model to use for a given task."""
    result = resolve_backend(req.task_type, req.latency, req.cost)
    return RouterResponse(
        backend=result["backend"],
        model=result["model"],
        config=result["config"],
    )


@app.post("/models/inference", response_model=InferenceResponse)
async def inference(req: InferenceRequest):
    """Run inference on the selected or auto-routed backend."""
    # Determine backend if not specified
    if req.backend:
        backend = req.backend
        model = req.model
    else:
        route = resolve_backend("default", None, "free")
        backend = route["backend"]
        model = req.model  # caller still controls the model name

    t0 = time.perf_counter()

    async with httpx.AsyncClient() as client:
        if backend == "ollama":
            text, p_tok, c_tok = await call_ollama(client, model, req.prompt, req.temperature, req.max_tokens)
        elif backend == "llama_cpp":
            text, p_tok, c_tok = await call_llama_cpp(client, model, req.prompt, req.temperature, req.max_tokens)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown backend: {backend}")

    timing_ms = (time.perf_counter() - t0) * 1000

    logger.info("Inference via %s model=%s tokens=%s timing=%.1fms", backend, model, c_tok, timing_ms)

    return InferenceResponse(
        text=text,
        backend_used=backend,
        model_used=model,
        timing_ms=round(timing_ms, 2),
        prompt_tokens=p_tok,
        completion_tokens=c_tok,
    )


@app.get("/models/backends")
async def list_backends():
    """Return available backends and their routing config."""
    return {
        "backends": [
            {"name": "ollama",    "host": OLLAMA_HOST,    "latency": "medium", "cost": 0.0},
            {"name": "llama_cpp", "host": LLAMA_CPP_HOST, "latency": "low",    "cost": 0.0},
        ],
        "routing_table": ROUTING_TABLE,
    }
