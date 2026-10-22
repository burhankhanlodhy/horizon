"""Flash Observations for OpenAI formats: next-turn stubbing."""

from __future__ import annotations

import copy

import pytest

from horizon.transforms.flash_observations import FlashHorizons, FlashPolicy
from horizon.transforms.flash_openai import (
    DEFAULT_OPENAI_TOOLS,
    NOTICE,
    apply_chat,
    apply_responses,
    chat_originals,
    chat_tail_start,
    conversation_key,
    responses_originals,
    responses_tail_start,
    upstream_supports_flash_openai,
)

POLICY = FlashPolicy(tools=frozenset(DEFAULT_OPENAI_TOOLS), min_chars=1000)
LOG = "\n".join(f"tests/test_io.py::test_{i} PASSED" for i in range(200))


def _code_mode_items(script: str) -> list[dict]:
    return [
        {"type": "message", "role": "user", "content": "run tests"},
        {"type": "custom_tool_call", "name": "exec", "call_id": "code", "input": script},
        {"type": "custom_tool_call_output", "call_id": "code", "output": LOG},
        {"type": "reasoning", "id": "rs", "encrypted_content": "keep-exact"},
        {"type": "message", "role": "assistant", "content": "tests passed"},
    ]


@pytest.mark.parametrize(
    "script",
    [
        'text(await tools.exec_command({cmd: "pytest"}));',
        'const r=await tools.exec_command({"cmd":"pytest"}); text(r.output);',
        'text(await Promise.all([tools.exec_command({cmd:"pytest"}), tools.exec_command({cmd:"ruff check"})]));',
    ],
)
def test_codex_shell_wrapper_is_shown_once_then_stubbed(script) -> None:
    items = _code_mode_items(script)
    original = copy.deepcopy(items)
    first = apply_responses(items[:3], horizon=1, policy=POLICY)
    assert first.flashed == 1 and first.stubbed == 0
    assert first.messages[2]["output"] == LOG + NOTICE
    later = apply_responses(items, horizon=1, policy=POLICY)
    assert later.stubbed == 1 and later.flashed == 0
    assert later.messages[2]["output"].startswith("[Horizon flash:")
    assert len(later.messages[2]["output"]) < len(LOG) // 2
    assert later.messages[:2] == first.messages[:2]
    assert later.messages[3:] == items[3:]
    assert items == original


@pytest.mark.parametrize(
    "script",
    [
        "text(await tools.web__run({search_query:[]}));",
        'text(await tools.exec_command({cmd:"pytest"})); text(await tools.web__run({}));',
        'text(await tools.exec_command({cmd:"pytest"})); image(await tools.view_image({path:"x"}));',
        'const other=tools.web__run; text(await tools.exec_command({cmd:"pytest"})); text(await other({}));',
        'text(await tools.exec_command({cmd:"pytest"})); text(await tools[name]({}));',
        'const {other}=tools; text(await tools.exec_command({cmd:"pytest"}));',
        'text(await tools.exec_command_extra({cmd:"pytest"}));',
    ],
)
def test_codex_generic_or_mixed_tool_wrapper_stays_verbatim(script) -> None:
    items = _code_mode_items(script)
    result = apply_responses(items, horizon=0, policy=POLICY)
    assert result.messages == items and not result.changed and result.flashed == 0


def test_codex_shell_wrapper_respects_horizon_policy_and_nontext_outputs() -> None:
    items = _code_mode_items('text(await tools.exec_command({cmd:"pytest"}));')
    assert apply_responses(items, horizon=3, policy=POLICY).messages == items
    no_shell = FlashPolicy(tools=frozenset({"shell"}), min_chars=1000)
    assert apply_responses(items, horizon=0, policy=no_shell).messages == items
    items[2]["output"] = [{"type": "input_image", "image_url": "data:image/png;base64,fake"}]
    assert apply_responses(items, horizon=0, policy=POLICY).messages == items


def test_codex_shell_wrapper_rerun_keeps_repeated_output_visible() -> None:
    items = _code_mode_items('text(await tools.exec_command({cmd:"pytest"}));')
    items += [
        {**items[1], "call_id": "again"},
        {**items[2], "call_id": "again"},
    ]
    result = apply_responses(items, horizon=0, policy=POLICY)
    assert result.paused and result.messages[-1] == items[-1]


def _responses(n: int, *, answered_last: bool = False, name: str = "shell") -> list[dict]:
    items: list[dict] = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix"}]}
    ]
    for t in range(n):
        items += [
            {"type": "reasoning", "id": f"rs{t}", "summary": [], "encrypted_content": f"enc{t}"},
            {"type": "function_call", "call_id": f"c{t}", "name": name, "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"c{t}", "output": LOG + f"\n#{t}"},
        ]
    if answered_last:
        items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            }
        )
    return items


def _chat(n: int, name: str = "bash") -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "fix"}]
    for t in range(n):
        messages += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{t}",
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": f"c{t}", "content": LOG + f"\n#{t}"},
        ]
    return messages


def test_newest_output_gets_notice_and_answered_ones_become_stubs() -> None:
    items = _responses(3)
    result = apply_responses(items, horizon=0, policy=POLICY)
    out = result.messages
    assert len(out) == len(items)
    assert result.stubbed == 2 and result.flashed == 1
    for i in (3, 6):
        assert out[i]["output"].startswith("[Horizon flash") and LOG not in out[i]["output"]
        assert "hash=" not in out[i]["output"]
    assert out[9]["output"] == LOG + "\n#2" + NOTICE
    assert items == _responses(3)  # input untouched
    # Reasoning and calls pass through byte-for-byte.
    assert [o for o in out if o["type"] != "function_call_output"] == [
        i for i in items if i["type"] != "function_call_output"
    ]


def test_append_only_across_turns() -> None:
    forwarded = [
        apply_responses(_responses(n), horizon=0, policy=POLICY).messages for n in (1, 2, 3)
    ]
    for earlier, later in zip(forwarded, forwarded[1:], strict=False):
        # Everything before the earlier turn's newest output is unchanged.
        assert later[: len(earlier) - 1] == earlier[:-1]


def test_stub_comes_from_the_client_original_not_compressed_text() -> None:
    items = _responses(2)
    originals = responses_originals(items)
    compressed = copy.deepcopy(items)
    compressed[3]["output"] = "[compressed: 200 lines] hash=deadbeef"
    replayed = copy.deepcopy(items)
    replayed[3]["output"] = LOG + "\n#0" + NOTICE  # last turn's forwarded bytes
    stubs = {
        apply_responses(variant, horizon=0, policy=POLICY, originals=originals).messages[3][
            "output"
        ]
        for variant in (items, compressed, replayed)
    }
    assert len(stubs) == 1


def test_assistant_message_answers_the_newest_output() -> None:
    result = apply_responses(_responses(1, answered_last=True), horizon=0, policy=POLICY)
    assert result.stubbed == 1 and result.flashed == 0


def test_horizon_keeps_earlier_outputs_raw() -> None:
    items = _responses(3)
    result = apply_responses(items, horizon=6, policy=POLICY)
    assert result.messages[3] == items[3]  # before the horizon
    assert result.stubbed == 1 and result.flashed == 1
    assert responses_tail_start(items) == 9


def test_only_allow_listed_tools_and_large_text_outputs() -> None:
    items = _responses(2, name="apply_patch")
    assert apply_responses(items, horizon=0, policy=POLICY).changed is False
    small = _responses(2)
    small[3]["output"] = "ok"
    small[6]["output"] = "ok"
    assert apply_responses(small, horizon=0, policy=POLICY).messages == small
    image = _responses(1)
    image[3]["output"] = [{"type": "input_image", "image_url": "data:x"}]
    assert apply_responses(image, horizon=0, policy=POLICY).messages == image


def test_mcp_and_web_tools_are_never_flashed() -> None:
    policy = FlashPolicy(tools=frozenset({"mcp__x__y", "web_search", "shell"}), min_chars=1000)
    items = _responses(2, name="mcp__x__y")
    assert apply_responses(items, horizon=0, policy=policy).changed is False


def test_text_part_outputs_keep_their_shape() -> None:
    items = _responses(2)
    for i in (3, 6):
        items[i]["output"] = [{"type": "input_text", "text": items[i]["output"]}]
    out = apply_responses(items, horizon=0, policy=POLICY).messages
    assert isinstance(out[3]["output"], str) and out[3]["output"].startswith("[Horizon flash")
    assert out[6]["output"][-1] == {"type": "input_text", "text": NOTICE.strip()}


def test_custom_and_local_shell_outputs() -> None:
    items = [
        {"type": "message", "role": "user", "content": "go"},
        {"type": "local_shell_call", "call_id": "s1", "action": {"command": ["ls"]}},
        {"type": "local_shell_call_output", "id": "s1", "output": LOG},
        {"type": "custom_tool_call", "call_id": "x1", "name": "exec_command", "input": "ls"},
        {"type": "custom_tool_call_output", "call_id": "x1", "output": LOG},
    ]
    out = apply_responses(items, horizon=0, policy=POLICY).messages
    assert out[2]["output"].startswith("[Horizon flash")  # answered by the custom call
    assert out[4]["output"].endswith(NOTICE)


def test_chat_completions() -> None:
    messages = _chat(3)
    result = apply_chat(messages, horizon=0, policy=POLICY, originals=chat_originals(messages))
    out = result.messages
    assert result.stubbed == 2 and result.flashed == 1
    assert out[2]["content"].startswith("[Horizon flash") and out[4]["content"].startswith(
        "[Horizon flash"
    )
    assert out[6]["content"].endswith(NOTICE)
    assert chat_tail_start(messages) == 6
    forwarded = [apply_chat(_chat(n), horizon=0, policy=POLICY).messages for n in (1, 2, 3)]
    for earlier, later in zip(forwarded, forwarded[1:], strict=False):
        assert later[: len(earlier) - 1] == earlier[:-1]


def test_horizon_store_and_conversation_key(tmp_path) -> None:
    store = FlashHorizons(tmp_path / "h.json")
    key = conversation_key(_responses(1))
    assert key and key == conversation_key(_responses(4))
    assert store.horizon_for(key, 3) == 3
    assert store.horizon_for(key, 9) == 3  # first sight wins
    assert FlashHorizons(tmp_path / "h.json").horizon_for(key, 9) == 3  # survives a restart


def test_upstream_gate(monkeypatch) -> None:
    monkeypatch.delenv("HORIZON_FLASH_ANY_UPSTREAM", raising=False)
    assert upstream_supports_flash_openai("https://api.openai.com/v1/responses")
    assert upstream_supports_flash_openai("https://chatgpt.com/backend-api/codex/responses")
    assert not upstream_supports_flash_openai("https://gateway.example/v1/responses")
    monkeypatch.setenv("HORIZON_FLASH_ANY_UPSTREAM", "1")
    assert upstream_supports_flash_openai("https://gateway.example/v1/responses")


def test_per_host_allow_list_is_openai_only(monkeypatch) -> None:
    from horizon.transforms.flash_observations import upstream_supports_flash

    monkeypatch.delenv("HORIZON_FLASH_ANY_UPSTREAM", raising=False)
    monkeypatch.setenv("HORIZON_FLASH_OPENAI_UPSTREAMS", " modelflare.dev , other.example")
    assert upstream_supports_flash_openai("https://modelflare.dev/v1/responses")
    assert upstream_supports_flash_openai("wss://api.modelflare.dev/v1/responses")
    assert not upstream_supports_flash_openai("https://notmodelflare.dev/v1/responses")
    assert not upstream_supports_flash_openai("https://gateway.example/v1/responses")
    # The Claude version is not unlocked by it (ModelFlare failed that preflight).
    assert not upstream_supports_flash("https://modelflare.dev")


def test_never_raises_on_garbage() -> None:
    garbage = [None, 3, "x", {"type": "function_call_output"}, {"role": "tool"}]
    assert apply_responses(garbage, horizon=0, policy=POLICY).messages == garbage
    assert apply_chat(garbage, horizon=0, policy=POLICY).messages == garbage


def _clear_decisions() -> None:
    from horizon.transforms import flash_openai

    flash_openai._DECISIONS.clear()


def test_per_model_decision_from_list_prices(monkeypatch) -> None:
    from horizon.transforms.flash_openai import flash_pays_for

    monkeypatch.delenv("HORIZON_FLASH_MIN_READ_RATIO", raising=False)
    _clear_decisions()
    # Catalog rates: DeepSeek V4 Pro reads at ~0.033x input, GPT-6.1 Sol at 0.05x.
    pays, reason = flash_pays_for("deepseek-v4-pro")
    assert pays is False and "nearly free" in reason
    assert flash_pays_for("gpt-6.1-sol")[0] is True
    # A model Horizon cannot price is flashed (gated by host already).
    assert flash_pays_for("some-unknown-model-xyz") == (True, "price unknown")


def test_per_model_decision_threshold_is_configurable(monkeypatch) -> None:
    from horizon.transforms.flash_openai import flash_pays_for

    _clear_decisions()
    monkeypatch.setenv("HORIZON_FLASH_MIN_READ_RATIO", "0")
    assert flash_pays_for("deepseek-v4-pro") == (True, "price check off")
    # Gemini 3.1 Pro's catalog row: reads at 0.10x input, no write premium.
    monkeypatch.setenv("HORIZON_FLASH_MIN_READ_RATIO", "0.2")
    assert flash_pays_for("gemini/gemini-3.1-pro-preview")[0] is False
    monkeypatch.setenv("HORIZON_FLASH_MIN_READ_RATIO", "0.05")
    assert flash_pays_for("gemini/gemini-3.1-pro-preview")[0] is True


def test_rerun_with_the_same_output_pauses_flash_after_it() -> None:
    items = _responses(2)
    # The model runs the first command again and gets the same log back.
    items += [
        {"type": "function_call", "call_id": "again", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "again", "output": LOG + "\n#0"},
    ]
    result = apply_responses(items, horizon=0, policy=POLICY)
    assert result.paused and result.paused_at == 7
    assert result.messages[3]["output"].startswith("[Horizon flash")  # earlier stub kept
    assert result.messages[8] == items[8]  # nothing after the re-run is touched


def test_rerun_with_different_output_is_ordinary_work() -> None:
    items = _responses(2)
    items += [
        {"type": "function_call", "call_id": "again", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "again", "output": LOG + "\n#0 FIXED"},
    ]
    result = apply_responses(items, horizon=0, policy=POLICY)
    assert not result.paused
    assert result.messages[8]["output"].endswith(NOTICE)


def test_rerun_detection_ignores_timings_in_chat() -> None:
    messages = _chat(1)
    messages[2]["content"] = LOG + "\n5 passed in 3.21s"
    messages += [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "again", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "again", "content": LOG + "\n5 passed in 4.02s"},
    ]
    result = apply_chat(messages, horizon=0, policy=POLICY)
    assert result.paused and result.messages[4] == messages[4]
