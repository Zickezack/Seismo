"""The Ollama server (architecture.md, "Environments and LLM"): one chat request whose answer is constrained to a JSON
schema. Every call runs inside LlmCallLimiter.slot() (app/llm/limiter.py)."""
from dataclasses import dataclass

import httpx2

# Answering takes the GPU a while: the whole answer comes at once, nothing is sent before.
ANSWER_TIMEOUT_SECONDS = 600
CONNECT_TIMEOUT_SECONDS = 10
# The longest answer; a model that runs on is cut here and its JSON is incomplete.
MAX_ANSWER_TOKENS = 3000


class LlmError(Exception):
    """A call that brought no answer."""


class LlmUnavailable(LlmError):
    """The server could not be reached or failed (timeout, connection, HTTP 5xx): a later attempt may succeed."""


class LlmRejected(LlmError):
    """The server refused the request (HTTP 4xx other than a missing model): retrying cannot help."""


@dataclass(frozen=True)
class LlmAnswer:
    content: str
    model: str  # as the server names the model that answered
    prompt_tokens: int
    answer_tokens: int
    complete: bool  # False when the answer was cut at MAX_ANSWER_TOKENS or the context's end


class OllamaClient:
    """Use as a context manager; it keeps its connection open until the end of the block."""

    def __init__(self, base_url: str, model: str, num_ctx: int, transport: httpx2.BaseTransport | None = None) -> None:
        self.model = model
        self.num_ctx = num_ctx
        timeout = httpx2.Timeout(ANSWER_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        self._client = httpx2.Client(base_url=base_url, timeout=timeout, transport=transport)

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._client.close()

    def serves_model(self) -> bool:
        """Whether the server answers and has the model pulled: a model still being pulled is not there yet."""
        try:
            response = self._client.get("/api/tags", timeout=CONNECT_TIMEOUT_SECONDS)
            names = {model["name"] for model in response.json().get("models", [])} if response.status_code == 200 else set()
        except (httpx2.HTTPError, ValueError):
            return False
        # A model named without a tag is the one tagged "latest".
        return self.model in names or f"{self.model}:latest" in names

    def chat_json(self, system: str, user: str, schema: dict) -> LlmAnswer:
        """The model's answer to one system and one user message, constrained to `schema`."""
        request = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": schema,
            "stream": False,
            # A thinking model writes its reasoning first: hundreds of tokens this answer does not need.
            "think": False,
            "options": {"num_ctx": self.num_ctx, "num_predict": MAX_ANSWER_TOKENS, "temperature": 0},
        }
        try:
            response = self._client.post("/api/chat", json=request)
        except httpx2.TimeoutException:
            raise LlmUnavailable(f"Keine Antwort innerhalb von {ANSWER_TIMEOUT_SECONDS} s") from None
        except httpx2.TransportError as error:
            raise LlmUnavailable(f"Ollama nicht erreichbar: {type(error).__name__}") from None
        if response.status_code >= 500:
            raise LlmUnavailable(f"Ollama: HTTP {response.status_code}")
        if response.status_code == 404:
            # The model is not pulled (yet): a setting of the server, not a fault of the article.
            raise LlmUnavailable(f"Ollama: HTTP 404: {_error_of(response)}")
        if response.status_code >= 400:
            raise LlmRejected(f"Ollama: HTTP {response.status_code}: {_error_of(response)}")
        body = response.json()
        return LlmAnswer(
            content=body["message"]["content"],
            model=body.get("model", self.model),
            prompt_tokens=body.get("prompt_eval_count", 0),
            answer_tokens=body.get("eval_count", 0),
            complete=body.get("done_reason", "stop") == "stop",
        )


def _error_of(response: httpx2.Response) -> str:
    """Ollama's own error text, e.g. "model 'x' not found"; it names only the request, never article text."""
    try:
        return str(response.json().get("error", ""))[:200]
    except ValueError:
        return ""
