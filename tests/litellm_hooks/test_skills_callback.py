"""litellm/skills_callback.py: which requests get the skills tool and index, and the later chat tool rounds.

The hook runs inside LiteLLM; these tests load it with small stand-ins for
the litellm modules it imports and for the MCP gateway helpers its tool
rounds call, and check that it reads SKILL.md frontmatter the way
container-scripts/skill_packages.py does.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import importlib.util
import pathlib
import sys
import types
from typing import Any

import pytest

import skill_packages
from fakes import write_skill

REPO = pathlib.Path(__file__).resolve().parents[2]
HOOK_FILE = REPO / "litellm" / "skills_callback.py"
GATEWAY_TOOL = {
    "type": "mcp",
    "server_label": "skills",
    "server_url": "litellm_proxy/mcp/skills",
    "require_approval": "never",
}
BRAVE_TOOL = {
    "type": "mcp",
    "server_label": "brave",
    "server_url": "litellm_proxy/mcp/brave_search",
    "require_approval": "never",
}
DEFAULT_PLAN = "LiteLLM's default plan: run nothing"
LONG_DESCRIPTION = "Builds things. " * 30


class StubCustomLogger:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    # LiteLLM's own signature: an extra keyword would raise TypeError here.
    async def async_build_agentic_loop_plan(
        self,
        tools,
        model,
        messages,
        response,
        anthropic_messages_provider_config,
        anthropic_messages_optional_request_params,
        logging_obj,
        stream,
        kwargs,
    ):
        return DEFAULT_PLAN


@dataclasses.dataclass
class AgenticLoopRequestPatch:
    messages: list[Any] | None = None
    optional_params: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class AgenticLoopPlan:
    run_agentic_loop: bool = False
    request_patch: AgenticLoopRequestPatch | None = None
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


class FakeGateway:
    """LiteLLM's MCP gateway helpers and server registry, as the hook calls them."""

    SERVED = {
        "skills-get": "skills",
        "skills-list": "skills",
        "skills-search": "skills",
        "brave_search-brave_web_search": "brave_search",
    }

    def __init__(self) -> None:
        self.registered = {"skills"}
        self.registry = types.SimpleNamespace(get_mcp_server_by_name=self._lookup)
        self.resolved: list[list[str]] = []
        self.executed: list[str] = []
        self.unanswered: set[str] = set()
        self.fail = False

    def _lookup(self, name: str, client_ip: str | None = None) -> object | None:
        return object() if name in self.registered else None

    def resolve_context(self, kwargs: Any, tools: Any) -> Any:  # MCPRequestContext.resolve
        return types.SimpleNamespace(
            request_tags=("chat",),
            user_api_key_auth="caller",
            litellm_trace_id="trace",
            mcp_auth_header=None,
            mcp_server_auth_headers=None,
            raw_headers={},
            oauth2_headers=None,
            litellm_call_id="call",
            guardrail_context=None,
        )

    async def split_mcp_tools(self, tools: list[dict[str, Any]]) -> tuple[list[Any], list[Any]]:
        return [t for t in tools if t.get("type") == "mcp"], [t for t in tools if t.get("type") != "mcp"]

    async def process_mcp_tools_without_openai_transform(
        self, user_api_key_auth: Any, mcp_tools_with_litellm_proxy: list[dict[str, Any]], **kwargs: Any
    ) -> tuple[list[Any], dict[str, str]]:
        if self.fail:
            raise RuntimeError("the gateway cannot list its tools")
        servers = sorted(ref["server_url"].rsplit("/", 1)[-1] for ref in mcp_tools_with_litellm_proxy)
        self.resolved.append(servers)
        served = {name: server for name, server in self.SERVED.items() if server in servers}
        return [types.SimpleNamespace(name=name) for name in served], served

    @staticmethod
    def extract_tool_calls_from_chat_response(response: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in response.choices[0].message.tool_calls
        ]

    @staticmethod
    def extract_tool_call_details(call: dict[str, Any]) -> tuple[str, str, str]:
        return call["function"]["name"], call["function"]["arguments"], call["id"]

    async def execute_tool_calls(self, tool_server_map: dict[str, str], tool_calls: list[Any], **kwargs: Any):
        results = []
        for call in tool_calls:
            name = call["function"]["name"]
            assert name in tool_server_map
            self.executed.append(name)
            if call["id"] not in self.unanswered:
                results.append({"tool_call_id": call["id"], "result": f"{name} result", "name": name})
        return results

    @classmethod
    def create_follow_up_messages_for_chat(cls, original_messages, response, tool_results):
        calls = cls.extract_tool_calls_from_chat_response(response)
        return [
            *original_messages,
            {"role": "assistant", "content": None, "tool_calls": calls},
            *({"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["result"]} for r in tool_results),
        ]


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def hook_module(monkeypatch: pytest.MonkeyPatch, gateway: FakeGateway):
    litellm = types.ModuleType("litellm")
    litellm.callbacks = []
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = StubCustomLogger
    handler = types.ModuleType("litellm.responses.mcp.litellm_proxy_mcp_handler")
    handler.LiteLLM_Proxy_MCP_Handler = gateway
    request_context = types.ModuleType("litellm.responses.mcp.request_context")
    request_context.MCPRequestContext = types.SimpleNamespace(resolve=gateway.resolve_context)
    plan_types = types.ModuleType("litellm.types.integrations.custom_logger")
    plan_types.AgenticLoopPlan = AgenticLoopPlan
    plan_types.AgenticLoopRequestPatch = AgenticLoopRequestPatch
    manager = types.ModuleType("litellm.proxy.mcp_server.mcp_server_manager")
    manager.global_mcp_server_manager = gateway.registry
    stubs = {
        "litellm": litellm,
        "litellm.integrations": types.ModuleType("litellm.integrations"),
        "litellm.integrations.custom_logger": custom_logger,
        "litellm.responses": types.ModuleType("litellm.responses"),
        "litellm.responses.mcp": types.ModuleType("litellm.responses.mcp"),
        "litellm.responses.mcp.litellm_proxy_mcp_handler": handler,
        "litellm.responses.mcp.request_context": request_context,
        "litellm.types": types.ModuleType("litellm.types"),
        "litellm.types.integrations": types.ModuleType("litellm.types.integrations"),
        "litellm.types.integrations.custom_logger": plan_types,
        "litellm.proxy": types.ModuleType("litellm.proxy"),
        "litellm.proxy.mcp_server": types.ModuleType("litellm.proxy.mcp_server"),
        "litellm.proxy.mcp_server.mcp_server_manager": manager,
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("skills_callback", HOOK_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert litellm.callbacks == [module.proxy_handler_instance]
    return module


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def skills_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "skills"
    write_skill(root, "beta-two", "description: >-\n  Does the beta thing,\n  over two lines.\nlicense: MIT")
    write_skill(root, "alpha", "description: Does the alpha thing.")
    write_skill(root, "long-one", f"description: {LONG_DESCRIPTION}")
    write_skill(root, "misnamed", "description: Says it is someone else.", name="other")
    write_skill(root, ".hidden", "description: Not a package.")
    (root / "no-skill-file").mkdir()
    return root


@pytest.fixture
def hook(hook_module, skills_root: pathlib.Path, clock: Clock):
    return hook_module.SkillsForEveryModel(skills_dir=skills_root, clock=clock)


def chat(
    tools: list[dict[str, Any]] | None = None,
    *,
    stream: bool = False,
    headers: dict[str, str] | None = None,
    system: Any = "You are helpful.",
    model: str = "gemma-4-26b-a4b",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Write me a report."}]
    if system is not None:
        messages.insert(0, {"role": "system", "content": system})
    data: dict[str, Any] = {"model": model, "messages": messages, "proxy_server_request": {"headers": headers or {}}}
    if tools is not None:
        data["tools"] = tools
    if stream:
        data["stream"] = True
    if metadata is not None:
        data["metadata"] = metadata
    return data


def anthropic(
    tools: list[dict[str, Any]] | None = None,
    *,
    stream: bool = False,
    system: Any = "You are helpful.",
    model: str = "gemma-4-26b-a4b",
    **extra: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": "Write me a report."}],
        "proxy_server_request": {"headers": {}},
        **extra,
    }
    if system is not None:
        data["system"] = system
    if tools is not None:
        data["tools"] = tools
    if stream:
        data["stream"] = True
    return data


def function(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def run(hook, data: dict[str, Any], call_type: str = "acompletion") -> dict[str, Any]:
    return asyncio.run(hook.async_pre_call_hook(user_api_key_dict=None, cache=None, data=data, call_type=call_type))


def system_of(data: dict[str, Any]) -> Any:
    return data["messages"][0]["content"]


def unchanged(hook, data: dict[str, Any], call_type: str = "acompletion") -> bool:
    before = copy.deepcopy(data)
    return run(hook, data, call_type) == before


# ---------------------------------------------------------------------------
# Which requests get what
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("call_type", ["acompletion", "completion"])
def test_a_tool_less_chat_request_gets_the_gateway_tool_and_the_index(hook, call_type: str) -> None:
    data = run(hook, chat(), call_type)
    assert data["tools"] == [GATEWAY_TOOL]
    assert system_of(data).startswith("You are helpful.\n\n<gateway_skills>\n")
    assert "call skills-get with its name" in system_of(data)
    assert "- alpha: Does the alpha thing.\n" in system_of(data)
    assert data["metadata"] == {
        "skills_mcp_auto_execute": True,
        "skills_mcp_gateway_servers": ["litellm_proxy/mcp/skills"],
    }


@pytest.mark.parametrize("stream", [False, True])
def test_a_tool_less_messages_request_gets_them_streaming_or_not(hook, stream: bool) -> None:
    data = run(hook, anthropic(stream=stream), "anthropic_messages")
    assert data["tools"] == [GATEWAY_TOOL]
    assert data["system"].startswith("You are helpful.\n\n<gateway_skills>\n")
    assert data["messages"] == [{"role": "user", "content": "Write me a report."}]
    assert "metadata" not in data, "LiteLLM's /v1/messages gateway runs every round; no flag for the chat rounds"


def test_the_index_fits_every_system_prompt_shape(hook) -> None:
    blocks = [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}]
    data = run(hook, anthropic(system=blocks), "anthropic_messages")
    assert data["system"][0] == blocks[0] and data["system"][1]["text"].startswith("<gateway_skills>")
    assert run(hook, anthropic(system=None), "anthropic_messages")["system"].startswith("<gateway_skills>")
    assert run(hook, anthropic(system="   "), "anthropic_messages")["system"].startswith("<gateway_skills>")
    opened = run(hook, chat(system=None))
    assert opened["messages"][0]["role"] == "system" and system_of(opened).startswith("<gateway_skills>")
    parts = run(hook, chat(system=[{"type": "text", "text": "Hi"}]))
    assert system_of(parts)[0] == {"type": "text", "text": "Hi"} and len(system_of(parts)) == 2
    developer = chat(system=None)
    developer["messages"].insert(0, {"role": "developer", "content": "Rules."})
    assert system_of(run(hook, developer)).startswith("Rules.\n\n<gateway_skills>")


def test_running_the_hook_again_changes_nothing(hook) -> None:
    once = run(hook, chat())
    again = run(hook, copy.deepcopy(once))
    assert again == once
    blocks = run(hook, anthropic(system=[{"type": "text", "text": "Hi"}]), "anthropic_messages")
    assert run(hook, copy.deepcopy(blocks), "anthropic_messages") == blocks


def test_a_streaming_chat_request_without_the_skills_tools_is_left_alone(hook) -> None:
    assert unchanged(hook, chat(stream=True))
    assert unchanged(hook, chat([BRAVE_TOOL], stream=True))


@pytest.mark.parametrize(
    ("tools", "call_type"),
    [
        ([function("get_weather")], "acompletion"),
        ([BRAVE_TOOL, function("blender_blender-execute_blender_code")], "acompletion"),
        ([{"name": "Bash", "description": "Run a command", "input_schema": {"type": "object"}}], "anthropic_messages"),
        ([{"type": "web_search_20250305", "name": "web_search"}], "anthropic_messages"),
        ([{"type": "mcp", "server_url": "https://example.test/mcp", "require_approval": "never"}], "acompletion"),
        ([{**BRAVE_TOOL, "require_approval": "always"}], "acompletion"),
        ([{**BRAVE_TOOL, "require_approval": None}], "anthropic_messages"),
        (["not a tool"], "acompletion"),
    ],
)
def test_requests_with_tools_of_their_own_are_left_alone(hook, tools, call_type: str) -> None:
    data = chat(tools) if call_type == "acompletion" else anthropic(tools)
    assert unchanged(hook, data, call_type)


def test_tools_that_are_not_a_list_are_left_alone(hook) -> None:
    assert unchanged(hook, {**chat(), "tools": "skills-get"})


@pytest.mark.parametrize(
    ("names", "call_type", "getter"),
    [
        (["skills_skills-search", "skills_skills-get", "read_file"], "acompletion", "skills_skills-get"),
        (
            ["mcp__skills__skills-list", "mcp__skills__skills-get", "Bash"],
            "anthropic_messages",
            "mcp__skills__skills-get",
        ),
        (["skills-search"], "acompletion", "skills-search"),
    ],
)
def test_requests_that_carry_the_skills_tools_get_the_index_only(hook, names, call_type: str, getter: str) -> None:
    if call_type == "acompletion":
        tools = [function(name) for name in names]
        data = run(hook, chat(tools, stream=True), call_type)
        index = system_of(data)
    else:
        tools = [{"name": name, "input_schema": {"type": "object"}} for name in names]
        data = run(hook, anthropic(tools), call_type)
        index = data["system"]
    assert data["tools"] == tools, "no gateway tool next to the client's own"
    assert f"call {getter} with its name" in index
    assert "metadata" not in data


@pytest.mark.parametrize(
    "reference",
    [GATEWAY_TOOL, {"type": "mcp", "server_url": "litellm_proxy", "require_approval": "never"}],
)
def test_a_caller_that_names_the_skills_server_gets_the_index_only(hook, reference) -> None:
    data = run(hook, chat([reference]))
    assert data["tools"] == [reference]
    assert "call skills-get with its name" in system_of(data)
    assert "metadata" not in data


def test_the_tool_joins_the_brave_injectors(hook) -> None:
    data = run(hook, chat([dict(BRAVE_TOOL)]))
    assert data["tools"] == [BRAVE_TOOL, GATEWAY_TOOL]
    assert data["metadata"]["skills_mcp_gateway_servers"] == [
        "litellm_proxy/mcp/brave_search",
        "litellm_proxy/mcp/skills",
    ]


def test_existing_metadata_is_kept(hook) -> None:
    data = run(hook, chat(metadata={"tags": ["eval"]}))
    assert data["metadata"]["tags"] == ["eval"] and data["metadata"]["skills_mcp_auto_execute"] is True


@pytest.mark.parametrize(
    ("data", "call_type"),
    [
        (chat(headers={"X-OpenWebUI-User-Email": "alice@example.com"}), "acompletion"),
        (chat([function("skills_skills-get")], headers={"x-openwebui-chat-id": "c1"}), "acompletion"),
        (
            {**chat(), "proxy_server_request": None, "metadata": {"headers": {"X-OpenWebUI-User-Id": "u1"}}},
            "acompletion",
        ),
        (chat(model="claude-sonnet-5-5"), "acompletion"),
        (anthropic(model="Claude-Haiku-5-5"), "anthropic_messages"),
        (anthropic(model="anthropic/claude-opus-5-5", stream=True), "anthropic_messages"),
        (chat(metadata={"skills_index": False}), "acompletion"),
        (anthropic(litellm_metadata={"skills_index": False}), "anthropic_messages"),
        (chat(), "call_mcp_tool"),
        (chat(), "aresponses"),
        ({"model": "local-embedding", "input": ["hi"]}, "aembedding"),
    ],
)
def test_open_webui_claude_opted_out_and_other_calls_are_left_alone(hook, data, call_type: str) -> None:
    assert unchanged(hook, data, call_type)


def test_only_an_explicit_false_opts_out(hook) -> None:
    assert run(hook, chat(metadata={"skills_index": True}))["tools"] == [GATEWAY_TOOL]


def test_without_the_server_no_tool_but_the_index_for_its_tools(hook_module, skills_root, clock, gateway, caplog):
    gateway.registered = set()
    hook = hook_module.SkillsForEveryModel(skills_dir=skills_root, clock=clock)
    assert unchanged(hook, chat())
    assert unchanged(hook, anthropic(), "anthropic_messages")
    assert unchanged(hook, chat())
    assert caplog.text.count("is not registered with LiteLLM") == 1, "warned once"
    data = run(hook, chat([function("skills_skills-get")]))
    assert "call skills_skills-get" in system_of(data)


def test_a_failing_server_lookup_adds_nothing(hook_module, skills_root, clock) -> None:
    def lookup(name: str) -> bool:
        raise RuntimeError("registry moved")

    hook = hook_module.SkillsForEveryModel(skills_dir=skills_root, clock=clock, server_registered=lookup)
    assert unchanged(hook, chat())


def test_no_skills_means_no_tool_and_no_index(hook_module, tmp_path, clock) -> None:
    for root in (tmp_path / "missing", tmp_path):
        hook = hook_module.SkillsForEveryModel(skills_dir=root, clock=clock)
        assert unchanged(hook, chat())
        assert unchanged(hook, chat([function("skills-get")]))


def test_a_failure_leaves_the_request_unchanged(hook, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(hook.index, "entries", broken)
    assert unchanged(hook, chat())


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


def test_the_index_lists_valid_packages_by_name_with_short_descriptions(hook, caplog) -> None:
    index = system_of(run(hook, chat()))
    lines = index.splitlines()
    assert lines[0] == "You are helpful." and lines[2] == "<gateway_skills>" and lines[-1] == "</gateway_skills>"
    assert lines[4:7] == [
        "- alpha: Does the alpha thing.",
        "- beta-two: Does the beta thing, over two lines.",
        lines[6],
    ]
    long_line = lines[6]
    assert long_line.startswith("- long-one: Builds things. Builds things.") and long_line.endswith("things...")
    assert len(long_line) <= len("- long-one: ") + 200
    assert "misnamed" not in index and "- other:" not in index and "hidden" not in index
    assert "skills/misnamed/SKILL.md its frontmatter name must equal the folder name" in caplog.text


def test_an_index_past_its_size_names_the_rest(hook_module) -> None:
    entries = [(f"skill-{number}", "Does a job that needs a fairly long description.") for number in range(10)]
    text = hook_module.index_text(entries, "skills-get", 200, 600)
    assert len(text) < 700
    assert "- skill-0: Does a job" in text
    assert text.splitlines()[-2].startswith("- Also available: ")
    assert text.splitlines()[-2].endswith("skill-9.")
    assert all(f"skill-{number}" in text for number in range(10))


@pytest.mark.parametrize(
    ("text", "limit", "clipped"),
    [
        ("Short.", 200, "Short."),
        ("one two three four", 12, "one two..."),
        ("one, two, three", 12, "one, two..."),
        ("unbreakablewordthatgoeson", 10, "unbreak..."),
        ("  spaced\n out  ", 200, "spaced out"),
    ],
)
def test_clip(hook_module, text: str, limit: int, clipped: str) -> None:
    assert hook_module.clip(text, limit) == clipped
    assert len(hook_module.clip(text, limit)) <= max(limit, len(clipped))


def test_a_changed_skill_shows_after_the_scan_interval(hook, skills_root, clock: Clock) -> None:
    assert ("alpha", "Does the alpha thing.") in hook.index.entries()
    write_skill(skills_root, "alpha", "description: Does the alpha thing, now better.")
    clock.now += 10
    assert ("alpha", "Does the alpha thing.") in hook.index.entries(), "read at most every 30 seconds"
    clock.now += 21
    assert ("alpha", "Does the alpha thing, now better.") in hook.index.entries()
    write_skill(skills_root, "gamma", "description: New.")
    clock.now += 31
    assert ("gamma", "New.") in hook.index.entries()


def test_frontmatter_is_read_like_the_publisher_reads_it(hook_module, skills_root) -> None:
    packages = sorted((REPO / "skills").glob("*/SKILL.md")) + sorted(skills_root.glob("[!.]*/SKILL.md"))
    assert len(packages) >= 6
    for path in packages:
        text = path.read_text(encoding="utf-8")
        assert hook_module.parse_frontmatter(text) == skill_packages.parse_frontmatter(text), path
    assert hook_module.parse_frontmatter("# No frontmatter\n") == ("", "")
    assert hook_module.parse_frontmatter("---\nname: 'quoted'\n---\n") == ("quoted", "")


def test_tool_name_shapes(hook_module) -> None:
    pattern = hook_module.skills_tool_pattern("skills")
    for name in ("skills-get", "skills_skills-list", "mcp__skills__skills-search", "my_skills-get"):
        assert pattern.search(name), name
    for name in ("skills-getter", "myskills-get", "skillsget", "skills-get-all", "brave_search-skills", "skills-save"):
        assert not pattern.search(name), name


# ---------------------------------------------------------------------------
# Later tool rounds on /v1/chat/completions
# ---------------------------------------------------------------------------


def response_calling(*calls: tuple[str, str]) -> Any:
    tool_calls = [
        types.SimpleNamespace(id=f"call-{number}", function=types.SimpleNamespace(name=name, arguments=arguments))
        for number, (name, arguments) in enumerate(calls)
    ]
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(tool_calls=tool_calls))])


ROUND_TWO = [
    {"role": "user", "content": "Write me a report."},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "first", "function": {"name": "skills-list"}}]},
    {"role": "tool", "tool_call_id": "first", "content": "alpha, beta-two"},
]
OFFERED = [function("skills-get"), function("skills-list"), function("brave_search-brave_web_search")]


def loop_kwargs(*, depth: int = 0, servers: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "_agentic_loop_api_surface": "chat_completions",
        "_agentic_loop_depth": depth,
        "custom_llm_provider": "hosted_vllm",
        "metadata": {
            "skills_mcp_auto_execute": True,
            "skills_mcp_gateway_servers": servers or ["litellm_proxy/mcp/skills"],
        },
        **extra,
    }


def gate(hook, response=None, messages=None, tools=None, stream=False, kwargs=None):
    return asyncio.run(
        hook.async_should_run_agentic_loop(
            response=response or response_calling(("skills-get", '{"name": "alpha"}')),
            model="gemma-4-26b-a4b",
            messages=ROUND_TWO if messages is None else messages,
            tools=OFFERED if tools is None else tools,
            stream=stream,
            custom_llm_provider="hosted_vllm",
            kwargs=loop_kwargs() if kwargs is None else kwargs,
        )
    )


def build(hook, response, kwargs, model: str = "gemma-4-26b-a4b"):
    return asyncio.run(
        hook.async_build_agentic_loop_plan(
            tools={"tool_calls": [], "tool_type": "skills_mcp"},
            model=model,
            messages=ROUND_TWO,
            response=response,
            anthropic_messages_provider_config=None,
            anthropic_messages_optional_request_params={},
            logging_obj=None,
            stream=False,
            kwargs=kwargs,
        )
    )


def test_the_gate_claims_later_rounds_of_chat_requests_it_gave_the_tool_to(hook) -> None:
    claimed, context = gate(hook)
    assert claimed is True
    assert context == {
        "tool_calls": [{"id": "call-0", "name": "skills-get", "arguments": '{"name": "alpha"}'}],
        "tool_type": "skills_mcp",
    }
    in_litellm_metadata = loop_kwargs()
    in_litellm_metadata["litellm_metadata"] = in_litellm_metadata.pop("metadata")
    assert gate(hook, kwargs=in_litellm_metadata)[0] is True


@pytest.mark.parametrize(
    "case",
    [
        {"stream": True},
        {"kwargs": {**loop_kwargs(), "_agentic_loop_api_surface": "responses"}},
        {"kwargs": {key: value for key, value in loop_kwargs().items() if key != "_agentic_loop_api_surface"}},
        {"kwargs": {**loop_kwargs(), "metadata": {}}},
        {"kwargs": {**loop_kwargs(), "max_agentic_loops": 0}},
        {"messages": ROUND_TWO[:1]},
        {"response": response_calling(("get_weather", "{}"))},
        {"response": response_calling(("skills-get", "{}"), ("get_weather", "{}"))},
        {"response": response_calling()},
        {"kwargs": "not a mapping"},
    ],
)
def test_the_gate_leaves_everything_else_alone(hook, case: dict[str, Any]) -> None:
    assert gate(hook, **case) == (False, {})


def test_no_later_rounds_when_turned_off(hook_module, skills_root, clock) -> None:
    hook = hook_module.SkillsForEveryModel(skills_dir=skills_root, clock=clock, max_extra_rounds=0)
    assert gate(hook) == (False, {})


def test_a_round_runs_through_every_gateway_server_the_request_carried(hook, gateway: FakeGateway) -> None:
    response = response_calling(("skills-get", '{"name": "alpha"}'), ("brave_search-brave_web_search", '{"q": "x"}'))
    servers = ["litellm_proxy/mcp/brave_search", "litellm_proxy/mcp/skills"]
    plan = build(hook, response, loop_kwargs(servers=servers, max_agentic_loops=3))
    assert plan.run_agentic_loop is True and plan.metadata == {"tool_type": "skills_mcp", "round": 1}
    assert gateway.resolved == [["brave_search", "skills"]]
    assert gateway.executed == ["skills-get", "brave_search-brave_web_search"]
    messages = plan.request_patch.messages
    assert messages[: len(ROUND_TWO)] == ROUND_TWO
    assert [message["role"] for message in messages[len(ROUND_TWO) :]] == ["assistant", "tool", "tool"]
    assert plan.request_patch.optional_params == {}


def test_the_skills_server_is_always_resolved(hook, gateway: FakeGateway) -> None:
    kwargs = loop_kwargs()
    kwargs["metadata"]["skills_mcp_gateway_servers"] = ["https://elsewhere.test/mcp/x", 7]
    assert build(hook, response_calling(("skills-get", "{}")), kwargs).run_agentic_loop is True
    assert gateway.resolved == [["skills"]]


def test_the_last_round_must_answer_in_text(hook) -> None:
    plan = build(hook, response_calling(("skills-get", '{"name": "alpha"}')), loop_kwargs(depth=2))
    assert plan.request_patch.optional_params == {"tools": None, "web_search_options": None}
    messages = plan.request_patch.messages
    assert [message["role"] for message in messages] == ["user"]
    assert "Tool results gathered for this request" in messages[0]["content"]
    assert "skills-get result" in messages[0]["content"] and "alpha, beta-two" in messages[0]["content"]
    assert "Answer now from the results above." in messages[0]["content"]


def test_claude_keeps_its_history_and_gets_tool_choice_none(hook) -> None:
    kwargs = loop_kwargs(depth=2, custom_llm_provider="anthropic")
    plan = build(hook, response_calling(("skills-get", "{}")), kwargs, model="claude-sonnet-5-5")
    assert plan.request_patch.optional_params == {"tool_choice": "none"}
    assert plan.request_patch.messages[-1]["role"] == "tool"
    assert plan.request_patch.messages[-1]["content"].endswith("Answer now from the results above.]")


def test_the_plan_declines_what_it_cannot_run(hook, gateway: FakeGateway, caplog) -> None:
    assert build(hook, response_calling(("skills-get", "{}")), loop_kwargs(depth=3)) == DEFAULT_PLAN
    assert build(hook, response_calling(("brave_search-brave_web_search", "{}")), loop_kwargs()) == DEFAULT_PLAN
    gateway.unanswered = {"call-0"}
    assert build(hook, response_calling(("skills-get", "{}")), loop_kwargs()) == DEFAULT_PLAN
    gateway.unanswered = set()
    gateway.fail = True
    assert build(hook, response_calling(("skills-get", "{}")), loop_kwargs()) == DEFAULT_PLAN
    assert "could not run a further tool round" in caplog.text


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_registration_happens_once(hook_module) -> None:
    hook_module.register_callback()
    assert sys.modules["litellm"].callbacks == [hook_module.proxy_handler_instance]


def test_settings_come_from_the_environment(hook_module, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path))
    monkeypatch.setenv("SKILLS_MCP_SERVER_NAME", "know-how")
    monkeypatch.setenv("SKILLS_INDEX_DESCRIPTION_CHARS", "80")
    monkeypatch.setenv("SKILLS_INDEX_MAX_CHARS", "900")
    monkeypatch.setenv("SKILLS_SKIP_MODELS", " Claude* , gpt-* ,")
    monkeypatch.setenv("SKILLS_MAX_EXTRA_ROUNDS", "1")
    hook = hook_module.SkillsForEveryModel.from_env()
    assert hook.index.root == tmp_path and hook.server_url == "litellm_proxy/mcp/know-how"
    assert (hook.description_chars, hook.index_max_chars, hook.max_extra_rounds) == (80, 900, 1)
    assert hook.skip_models == ("claude*", "gpt-*")
    assert hook.tool_pattern.search("know-how-get")
    for name in ("SKILLS_DIR", "SKILLS_MCP_SERVER_NAME", "SKILLS_MAX_EXTRA_ROUNDS", "SKILLS_SKIP_MODELS"):
        monkeypatch.delenv(name)
    defaults = hook_module.SkillsForEveryModel.from_env()
    assert defaults.index.root == pathlib.Path("/app/skills") and defaults.max_extra_rounds is None
    assert defaults.skip_models == ("claude*", "anthropic/*")


def test_the_default_server_lookup_asks_litellms_registry(hook_module, gateway: FakeGateway) -> None:
    assert hook_module._gateway_server_registered("skills") is True
    assert hook_module._gateway_server_registered("brave_search") is False
