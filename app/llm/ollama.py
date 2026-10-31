"""The Ollama server (architecture.md, "Environments and LLM"): one chat request whose answer is constrained to a JSON
schema. Every call runs inside LlmCallLimiter.slot() (app/llm/limiter.py)."""
from dataclasses import dataclass

import httpx2

# Answering takes the GPU a while: the whole answer comes at once, nothing is sent before. About 2.5 times the longest
# answer expected on the GTX 1080 of stage 1; a slower one points to a problem (a model not in the GPU's memory). The
# worker's stop_grace_period (docker-compose.yml) outlasts it, so a deploy lets a running call finish.
ANSWER_TIMEOUT_SECONDS = 300
CONNECT_TIMEOUT_SECONDS = 10
# The longest answer; a model that runs on is cut here, or at the context's end before, and its JSON is incomplete.
# Long articles took more than 3000 (measured 2026-10-31: up to 4500, about 50 s on the 4070 Ti); 6000 stays within
# ANSWER_TIMEOUT_SECONDS at about 30 tokens a second.
MAX_ANSWER_TOKENS = 6000
# The room for the answer a text to extract leaves at least (extraction_service.fit_text): a shorter text leaves more,
# up to MAX_ANSWER_TOKENS, so a higher cap takes no text away.
MIN_ANSWER_ROOM_TOKENS = 3000


class LlmError(Exception):
    """A call that brought no answer."""


class LlmUnavailable(LlmError):
    """The server could not be reached or failed (timeout, connection, HTTP 5xx): a later attempt may succeed."""


class LlmUnreachable(LlmUnavailable):
    """No connection, HTTP 5xx or the model not pulled: the server is away, not the article's fault. An LLM job waits for
    it without counting the attempt; a timeout is no such sign - it may be the article's."""


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

    def serves_model(self, timeout: float = CONNECT_TIMEOUT_SECONDS) -> bool:
        """Whether the server answers and has the model pulled: a model still being pulled is not there yet."""
        try:
            response = self._client.get("/api/tags", timeout=timeout)
            names = {model["name"] for model in response.json().get("models", [])} if response.status_code == 200 else set()
        except (httpx2.HTTPError, ValueError):
            return False
        # A model named without a tag is the one tagged "latest".
        return self.model in names or f"{self.model}:latest" in names

    def chat_json(self, messages: list[dict], schema: dict) -> LlmAnswer:
        """The model's answer to the messages (system, user, and an exchange before), constrained to `schema`."""
        request = {
            "model": self.model,
            "messages": messages,
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
            raise LlmUnreachable(f"Ollama nicht erreichbar: {type(error).__name__}") from None
        if response.status_code >= 500:
            raise LlmUnreachable(f"Ollama: HTTP {response.status_code}")
        if response.status_code == 404:
            # The model is not pulled (yet): a setting of the server, not a fault of the article.
            raise LlmUnreachable(f"Ollama: HTTP 404: {_error_of(response)}")
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
