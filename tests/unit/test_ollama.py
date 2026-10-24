"""The Ollama client against simulated answers (httpx2.MockTransport)."""
import json

import httpx2
import pytest

from app.llm.ollama import LlmRejected, LlmUnavailable, OllamaClient


def client(handler):
    return OllamaClient("http://ollama:11434", "qwen3.5:9b", 8192, transport=httpx2.MockTransport(handler))


def test_one_chat_constrained_to_the_schema_without_thinking():
    seen = {}

    def handler(request):
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx2.Response(200, json={"model": "qwen3.5:9b", "message": {"role": "assistant", "content": '{"events": []}'},
                                          "done_reason": "stop", "prompt_eval_count": 812, "eval_count": 9})

    with client(handler) as llm:
        answer = llm.chat_json("system text", "user text", {"type": "object"})

    assert (answer.content, answer.model, answer.prompt_tokens, answer.answer_tokens, answer.complete) == ('{"events": []}', "qwen3.5:9b", 812, 9, True)
    body = seen["body"]
    assert seen["url"] == "http://ollama:11434/api/chat"
    assert body["messages"] == [{"role": "system", "content": "system text"}, {"role": "user", "content": "user text"}]
    assert (body["format"], body["stream"], body["think"]) == ({"type": "object"}, False, False)
    assert body["options"] == {"num_ctx": 8192, "num_predict": 3000, "temperature": 0}


def test_an_answer_cut_at_its_length_is_incomplete():
    answer = {"model": "m", "message": {"content": '{"events": ['}, "done_reason": "length"}

    with client(lambda _request: httpx2.Response(200, json=answer)) as llm:
        assert llm.chat_json("s", "u", {}).complete is False


def test_a_missing_model_or_a_failing_server_is_unavailable_another_refusal_is_final():
    # Not pulled yet: a state of the server, which a later attempt may find changed.
    with client(lambda _request: httpx2.Response(404, json={"error": "model 'qwen3.5:9b' not found"})) as llm:
        with pytest.raises(LlmUnavailable, match=r"^Ollama: HTTP 404: model 'qwen3.5:9b' not found$"):
            llm.chat_json("s", "u", {})
    with client(lambda _request: httpx2.Response(503)) as llm, pytest.raises(LlmUnavailable, match="^Ollama: HTTP 503$"):
        llm.chat_json("s", "u", {})
    with client(lambda _request: httpx2.Response(400, json={"error": "invalid format"})) as llm:
        with pytest.raises(LlmRejected, match=r"^Ollama: HTTP 400: invalid format$"):
            llm.chat_json("s", "u", {})


@pytest.mark.parametrize(
    ("answer", "served"),
    [
        (httpx2.Response(200, json={"models": [{"name": "qwen3.5:9b"}, {"name": "llama3.1:8b"}]}), True),
        (httpx2.Response(200, json={"models": [{"name": "llama3.1:8b"}]}), False),
        (httpx2.Response(500), False),
        (httpx2.Response(200, text="kein JSON"), False),
    ],
    ids=["pulled", "not pulled", "failing", "no JSON"],
)
def test_whether_the_server_serves_the_model(answer, served):
    with client(lambda _request: answer) as llm:
        assert llm.serves_model() is served


def test_a_model_named_without_a_tag_is_the_latest_and_an_unreachable_server_serves_none():
    tagged = httpx2.Response(200, json={"models": [{"name": "mistral:latest"}]})
    with OllamaClient("http://ollama:11434", "mistral", 8192, transport=httpx2.MockTransport(lambda _request: tagged)) as llm:
        assert llm.serves_model()

    def refused(request):
        raise httpx2.ConnectError("refused", request=request)

    with client(refused) as llm:
        assert not llm.serves_model()


def test_an_unreachable_or_silent_server_is_unavailable():
    def refused(request):
        raise httpx2.ConnectError("refused", request=request)

    def silent(request):
        raise httpx2.ReadTimeout("slow", request=request)

    with client(refused) as llm, pytest.raises(LlmUnavailable, match="^Ollama nicht erreichbar: ConnectError$"):
        llm.chat_json("s", "u", {})
    with client(silent) as llm, pytest.raises(LlmUnavailable, match="^Keine Antwort innerhalb von 600 s$"):
        llm.chat_json("s", "u", {})
