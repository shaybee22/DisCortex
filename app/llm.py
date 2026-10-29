"""Ollama client (chat, tags, embed). Knows nothing about Discord."""
from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# A reply cut off mid-think leaves an unclosed tag; drop everything after it.
_OPEN_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


class LLMError(Exception):
    pass


class EmbedModelMissing(LLMError):
    pass


def model_installed(name: str, installed: list[str]) -> bool:
    """True if `name` is in Ollama's model list. A name without a tag means ':latest'."""
    def full(n: str) -> str:
        return n if ":" in n else n + ":latest"
    return full(name) in {full(m) for m in installed}


def strip_think(text: str) -> str:
    text = _THINK_RE.sub("", text)
    text = _OPEN_THINK_RE.sub("", text)
    return text.strip()


class OllamaClient:
    def __init__(self, base_url: str, chat_timeout: float = 180.0, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.chat_timeout = chat_timeout
        # Ollama itself has no auth; a key is for a reverse proxy in front of it.
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def list_models(self) -> list[str]:
        """GET /api/tags. Raises LLMError if Ollama is unreachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0, headers=self.headers) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                r.raise_for_status()
                return [m["name"] for m in r.json().get("models", [])]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise LLMError(f"Can't reach Ollama at {self.base_url}. Is it running and listening "
                           f"on the network? ({str(e) or type(e).__name__})") from e

    async def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.8,
        num_ctx: int | None = None,
        json_output: bool = False,
    ) -> str:
        """POST /api/chat (non-streaming). Returns cleaned reply text.
        json_output=True asks Ollama to constrain the reply to valid JSON."""
        options: dict = {"temperature": temperature}
        if num_ctx:
            options["num_ctx"] = num_ctx
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
            "keep_alive": "30m",
        }
        if json_output:
            body["format"] = "json"
        try:
            async with httpx.AsyncClient(timeout=self.chat_timeout, headers=self.headers) as client:
                r = await client.post(f"{self.base_url}/api/chat", json=body)
                r.raise_for_status()
                content = r.json()["message"]["content"]
        except httpx.HTTPStatusError as e:
            raise LLMError(f"Ollama HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise LLMError(f"Ollama chat failed: {e!r}") from e
        return strip_think(content)

    async def embed(self, model: str, texts: list[str], timeout: float = 60.0) -> list[list[float]]:
        """POST /api/embed. One vector per input text.
        Raises EmbedModelMissing if the model isn't pulled on this server."""
        body = {"model": model, "input": texts, "keep_alive": "30m"}
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=self.headers) as client:
                r = await client.post(f"{self.base_url}/api/embed", json=body)
                if r.status_code == 404 or (r.status_code >= 400 and "not found" in r.text.lower()):
                    raise EmbedModelMissing(f"Embed model '{model}' is not on {self.base_url}")
                r.raise_for_status()
                vectors = r.json()["embeddings"]
        except EmbedModelMissing:
            raise
        except httpx.HTTPStatusError as e:
            raise LLMError(f"Ollama embed HTTP {e.response.status_code}: {e.response.text[:200]}") from e
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise LLMError(f"Ollama embed failed: {e!r}") from e
        if len(vectors) != len(texts):
            raise LLMError(f"Ollama embed returned {len(vectors)} vectors for {len(texts)} inputs")
        return vectors
