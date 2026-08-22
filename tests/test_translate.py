import json

from mirofish.translate import (OpenAIStreamTranslator, anthropic_to_openai_response,
                                openai_to_anthropic)


def test_system_and_params():
    out = openai_to_anthropic({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 128,
        "temperature": 0.4,
        "stop": ["END"],
        "messages": [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "hi"},
        ],
    })
    assert out["system"] == "You are terse."
    assert out["max_tokens"] == 128
    assert out["temperature"] == 0.4
    assert out["stop_sequences"] == ["END"]
    assert out["messages"] == [{"role": "user", "content": "hi"}]


def test_tools_and_tool_results():
    out = openai_to_anthropic({
        "model": "m",
        "tools": [{"type": "function", "function": {
            "name": "get_weather", "description": "d",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }}],
        "tool_choice": "required",
        "messages": [
            {"role": "user", "content": "weather in SF?"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "get_weather", "arguments": "{\"city\": \"SF\"}"},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ],
    })
    assert out["tools"][0]["name"] == "get_weather"
    assert out["tools"][0]["input_schema"]["properties"]["city"]["type"] == "string"
    assert out["tool_choice"] == {"type": "any"}
    assistant = out["messages"][1]
    assert assistant["content"][0] == {"type": "tool_use", "id": "call_1",
                                       "name": "get_weather", "input": {"city": "SF"}}
    tool_turn = out["messages"][2]
    assert tool_turn["role"] == "user"
    assert tool_turn["content"][0]["type"] == "tool_result"
    assert tool_turn["content"][0]["tool_use_id"] == "call_1"


def test_image_data_url():
    pixel = "iVBORw0KGgoAAAANSUhEUg=="
    out = openai_to_anthropic({
        "model": "m",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{pixel}"}},
        ]}],
    })
    blocks = out["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["media_type"] == "image/png"


def test_response_with_tool_use():
    resp = anthropic_to_openai_response({
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
             "input": {"city": "SF"}},
        ],
    }, "gpt-model")
    choice = resp["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "checking"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "SF"}
    assert resp["usage"]["total_tokens"] == 15


def test_stream_translator_text_and_tools():
    tr = OpenAIStreamTranslator("m")
    chunks = []
    events = [
        ("message_start", {"type": "message_start",
                           "message": {"usage": {"input_tokens": 7}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "Hel"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "lo"}}),
        ("content_block_start", {"type": "content_block_start", "index": 1,
                                 "content_block": {"type": "tool_use", "id": "toolu_9",
                                                   "name": "f"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                 "delta": {"type": "input_json_delta",
                                           "partial_json": "{\"a\":1}"}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                           "usage": {"output_tokens": 3}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    for event, data in events:
        chunks.extend(tr.feed(event, data))
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == "Hello"
    tool_start = next(c for c in chunks
                      if c["choices"][0]["delta"].get("tool_calls"))
    assert tool_start["choices"][0]["delta"]["tool_calls"][0]["id"] == "toolu_9"
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    assert final["usage"] == {"prompt_tokens": 7, "completion_tokens": 3,
                              "total_tokens": 10}
