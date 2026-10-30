"""L3 · 语义索引层的向量来源 —— Embedder。

设计依据：
- design-foundation/01-concept-model.md §2 L3「带语义索引 / 遗忘策略 / 冲突消解 / 权限分区」
- design-foundation/06-open-conclusions-r1.md C13（4 层结构里的 L4 语义索引层）

两个实现：
- `OllamaEmbedder` —— 真模型（qwen3-embedding:0.6b，本机 Ollama）
- `HashEmbedder` —— **确定性**假 embedder：字符 n-gram 哈希投影到固定维度

为什么要有 HashEmbedder：
1. 测试不该依赖模型在线 —— 单元测试用假 embedder，零网络、零模型开销
2. 模型不可用时，语义记忆仍能跑（降级成字面相似度），而不是整层瘫掉
3. **必须用 blake2b 而不是内置 hash()** —— 内置 hash 每个进程加随机盐，
   跨进程不可复现，向量索引就废了
"""
from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable

@runtime_checkable
class Embedder(Protocol):
    """向量来源。dim 是向量维度，model 是标识（写进存储层用于跨模型校验）。"""

    dim: int
    model: str

    def embed(self, text: str) -> list[float]:
        """把文本变成单位向量。"""
        ...

class HashEmbedder:
    """确定性假 embedder —— 字符 n-gram 哈希投影。

    **语义是假的**，但稳定、可复现、零依赖：
    相同文本永远得到相同向量，相似文本（共享字符）会得到一定程度的相似。

    用途：单元测试、离线降级、当召回结果需要「可解释」时做对照基线。
    """

    def __init__(self, dim: int = 256) -> None:
        if dim <= 0:
            raise ValueError(f"dim 必须为正：{dim}")
        self.dim = dim
        self.model = f"hash-ngram-{dim}"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        t = (text or "").strip().lower()
        if not t:
            return vec
        grams = [t[i : i + 2] for i in range(len(t) - 1)] or [t]
        for g in grams:
            digest = hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return _normalize(vec)

class OllamaEmbedder:
    """真模型 embedder（qwen3-embedding:0.6b）。

    三层限流，沿用 `compiler/intent_llm.py` 的既有约定：
    - `timeout`     —— 单次请求超时
    - `deadline_seconds` —— 总墙钟预算，剩余不足时收缩单请求超时
    - `max_retries` —— 默认 0：一次尝试 = 一次请求，杜绝自动重试烧配额

    用 stdlib urllib 而非 httpx：不引入新依赖（httpx 只是 langchain 的传递依赖），
    原型阶段调用量低，stdlib 的 socket 超时够用。
    """

    def __init__(
        self,
        model: str = "qwen3-embedding:0.6b",
        base_url: str = "http://localhost:11434",
        timeout: float = 30.0,
        max_retries: int = 0,
        deadline_seconds: float | None = None,
        dim: int = 1024,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.deadline_seconds = deadline_seconds
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        import time

        deadline = (
            None
            if self.deadline_seconds is None
            else time.monotonic() + self.deadline_seconds
        )
        last_err: Exception | None = None
        for _ in range(self.max_retries + 1):
            budget = self.timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("embedder 超出总预算")
                budget = min(self.timeout, remaining)
            try:
                vec = self._embed_once(text, budget)
                self.dim = len(vec)
                return vec
            except Exception as e:
                last_err = e
        raise RuntimeError(
            f"embed 失败（尝试 {self.max_retries + 1} 次）：{last_err}"
        ) from last_err

    def _embed_once(self, text: str, timeout: float) -> list[float]:
        """打一次 Ollama 嵌入接口。

        优先用新接口 `/api/embed`（批量、字段名 embeddings），
        老版本只有 `/api/embeddings`（单条、字段名 embedding）—— 两个都试。
        """
        payload = json.dumps({"model": self.model, "input": text}).encode("utf-8")
        try:
            data = self._post("/api/embed", payload, timeout)
            embeddings = data.get("embeddings")
            if isinstance(embeddings, list) and embeddings:
                vec = embeddings[0]
                if isinstance(vec, list) and vec:
                    return [float(x) for x in vec]
            raise ValueError(f"/api/embed 返回结构异常：{list(data)[:5]}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        # 老接口兜底
        payload = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
        data = self._post("/api/embeddings", payload, timeout)
        vec = data.get("embedding")
        if not isinstance(vec, list) or not vec:
            raise ValueError(f"/api/embeddings 返回结构异常：{list(data)[:5]}")
        return [float(x) for x in vec]

    def _post(self, path: str, payload: bytes, timeout: float) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

def _normalize(vec: list[float]) -> list[float]:
    """归一化到单位长度 —— 之后余弦相似度 = 点积。"""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return list(vec)
    return [v / norm for v in vec]
