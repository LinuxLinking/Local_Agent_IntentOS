"""模型客户端构造。"""
from langchain_ollama import ChatOllama

from pypilot.config import MODEL_NAME, NUM_CTX, OLLAMA_BASE_URL, STOP_SEQUENCES

def make_llm() -> ChatOllama:
    return ChatOllama(
        model=MODEL_NAME,
        base_url=OLLAMA_BASE_URL,
        stop=STOP_SEQUENCES,
        num_ctx=NUM_CTX,
    )

def make_summarizer_llm() -> ChatOllama:
    return ChatOllama(
        model=MODEL_NAME,
        base_url=OLLAMA_BASE_URL,
        num_ctx=NUM_CTX,
    )
