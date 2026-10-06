"""
Car insurance quote service (mock) for the FDE challenge.

Vendored from github.com/namastexlabs/namastex-fde-challenge. Only comments and
docstrings were translated; the HTTP contract and behaviour are unchanged.

POST /quote  -> computes a quote from lead/vehicle data.
GET  /health -> health check (always stable).
GET  /planos -> plan table and rules (read-only).

Operational note: this service simulates a real legacy system. It does NOT always
answer on the first try -- some calls fail or are slow. Handle it in your agent.
Failure rate and latency are configurable through environment variables:
    QUOTE_FAILURE_RATE   (default 0.20)  -> fraction of calls that fail
    QUOTE_SLOW_RATE      (default 0.10)  -> fraction of slow calls
    QUOTE_SLOW_SECONDS   (default 8)     -> duration of a slow call (simulates timeout)
    QUOTE_SEED           (optional)      -> seeds the RNG for reproducible failures
"""
from __future__ import annotations
import os, time, random
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from .quote_logic import cotar, load_plans, CotacaoRecusada

FAILURE_RATE = float(os.getenv("QUOTE_FAILURE_RATE", "0.20"))
SLOW_RATE = float(os.getenv("QUOTE_SLOW_RATE", "0.10"))
SLOW_SECONDS = float(os.getenv("QUOTE_SLOW_SECONDS", "8"))
_seed = os.getenv("QUOTE_SEED")
_rng = random.Random(int(_seed)) if _seed else random.Random()

app = FastAPI(title="AutoSeguro Quote API (mock)", version="1.0.0")


class QuoteRequest(BaseModel):
    plano_id: str = Field("essencial", description="essencial | completo | premium")
    idade: int = Field(..., ge=0, le=200)
    veiculo_ano: int = Field(..., ge=1950, le=2100)
    cep: str | None = None
    data_inicio: str | None = Field(None, description="YYYY-MM-DD (optional)")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/planos")
def planos():
    return load_plans()


@app.post("/quote")
def quote(req: QuoteRequest):
    # --- simulated instability (on purpose) ---
    roll = _rng.random()
    if roll < FAILURE_RATE:
        kind = _rng.choice(["500", "502", "503"])
        return JSONResponse(status_code=int(kind),
                            content={"error": "upstream_unavailable",
                                     "message": "Servico de cotacao temporariamente indisponivel. Tente novamente."})
    if roll < FAILURE_RATE + SLOW_RATE:
        time.sleep(SLOW_SECONDS)  # simulates timeout / slowness

    # --- quote ---
    try:
        return cotar(req.model_dump())
    except CotacaoRecusada as e:
        return JSONResponse(status_code=422, content={"error": "cotacao_recusada", "motivo": e.motivo})
    except (KeyError, ValueError, TypeError) as e:
        return JSONResponse(status_code=400, content={"error": "payload_invalido", "detalhe": str(e)})
