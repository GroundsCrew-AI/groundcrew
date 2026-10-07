"""Evidence ranking for Medic: embedding cosine by default (no rerank model on Token Factory)."""

from typing import Any

import pytest

from app.config import Settings
from app.llm.budget import MemoryUsageStore, RunBudget
from app.llm.evidence import Finding, chunk, rank
from tests.fakes import FakeLLM

pytestmark = pytest.mark.anyio


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"model_nano": "m/nano", "embed_dim": 2, **overrides}
    return Settings(_env_file=None, **values)


def _finding(summary: str, severity: int) -> Finding:
    return Finding(
        source="cloudwatch:/ecs/worker",
        time_range="10:00-10:05",
        kind="resource",
        summary=summary,
        quote=summary,
        severity=severity,
    )


FINDINGS = [_finding("disk 40% full", 1), _finding("OOMKilled exit 137", 3), _finding("cpu ok", 0)]


def _budget(settings: Settings) -> RunBudget:
    return RunBudget(usage=MemoryUsageStore(), settings=settings)


async def test_rank_by_embedding_similarity() -> None:
    settings = _settings(price_embed_per_m=0.01)
    # query, then one vector per finding: the second finding is closest to the symptom.
    vectors = [[1.0, 0.0], [0.0, 1.0], [0.9, 0.1], [0.5, 0.5]]
    llm = FakeLLM(settings=settings, embeddings=[vectors])
    ranked = await rank(llm, _budget(settings), "container OOM after deploy", FINDINGS, top_k=2)
    assert [f.summary for f in ranked] == ["OOMKilled exit 137", "cpu ok"]
    [request] = llm.calls
    assert request["input"][0] == "container OOM after deploy"  # one call for everything


async def test_rank_falls_back_to_severity_without_embeddings() -> None:
    settings = _settings()  # PRICE_EMBED_PER_M unset: embeddings are refused
    llm = FakeLLM(settings=settings)
    ranked = await rank(llm, _budget(settings), "symptom", FINDINGS, top_k=2)
    assert [f.severity for f in ranked] == [3, 1]
    assert llm.calls == []


async def test_rank_uses_rerank_model_when_configured() -> None:
    settings = _settings(model_rerank="r/model", price_rerank_per_m=0.01)
    llm = FakeLLM(
        settings=settings,
        rerank_results=[
            {
                "results": [
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ]
            }
        ],
    )
    ranked = await rank(llm, _budget(settings), "symptom", FINDINGS, top_k=2)
    assert [f.summary for f in ranked] == ["cpu ok", "disk 40% full"]


def test_chunk_splits_on_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(100))
    parts = chunk(text, max_tokens=50)  # about 200 characters each
    assert len(parts) > 1 and "\n".join(parts) == text
    assert all(len(p) <= 200 + 10 for p in parts)
