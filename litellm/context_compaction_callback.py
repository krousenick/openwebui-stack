import os
from typing import Optional

import litellm
from litellm.integrations.custom_logger import CustomLogger

# Fallback when the model has no registered context window. Generous, not
# infinite: the compactor still protects 8K edge models, but requests that
# cannot fit at all fail at the provider with a clear 400 instead of here.
DEFAULT_INPUT_TOKENS = 8192
# Headroom for the completion: the deployment must hold prompt + output.
OUTPUT_HEADROOM_TOKENS = 2048
# Every model_info field that can declare an input window, in lookup order.
MAX_INPUT_KEYS = ("max_input_tokens", "max_tokens")
# One env var per model, e.g. CONTEXT_COMPACTION_MODEL_gemini_3_8_flash.
# Useful for models served without a registered window (edge nodes).
ENV_PREFIX = "CONTEXT_COMPACTION_MODEL_"
# Minimum size for a compaction note describing what was dropped.
NOTE_PREFIX = "[Earlier conversation was compacted to fit the model's context window. "
NOTE_SUFFIX = "]"
# Claude models need the conversation to open with a user turn: the
# Anthropic Messages API answers 400 ("first message must use the user
# role") when it opens with an assistant turn, and LiteLLM does not repair
# that. For these models the compaction note is sent as a user turn, which
# LiteLLM merges with the user turn that follows it. Matched against the
# deployment's litellm_params.model, so it covers Claude on the Anthropic
# API, Bedrock, and Vertex AI. Every other provider keeps the assistant note.
USER_FIRST_MODEL_MARKERS = ("anthropic", "claude")


def _env_key(model_name: str) -> str:
    sanitized = "".join(c if c.isalnum() else "_" for c in model_name)
    return f"{ENV_PREFIX}{sanitized}"


def _window_for(model_name: str) -> Optional[int]:
    """Resolve the model's input limit: proxy router, then cost map, then env."""
    # 1. The live proxy router holds every registered deployment's
    #    model_info (config.yaml model_info and the vllm_compute role's
    #    LiteLLM registration both set max_input_tokens there).
    try:
        from litellm.proxy import proxy_server

        router = getattr(proxy_server, "llm_router", None)
        if router is not None:
            for deployment in router.get_model_list() or []:
                if str(deployment.get("model_name", "")) == model_name:
                    model_info = deployment.get("model_info") or {}
                    for key in MAX_INPUT_KEYS:
                        value = model_info.get(key)
                        if isinstance(value, int) and value > 0:
                            return value
                    break
    except Exception:
        pass
    # 2. LiteLLM's known cost map (Gemini, Bedrock, etc.).
    try:
        info = litellm.get_model_info(model=model_name)
    except Exception:
        info = None
    if isinstance(info, dict):
        for key in MAX_INPUT_KEYS:
            value = info.get(key)
            if isinstance(value, int) and value > 0:
                return value
    # 3. Per-model env override.
    value = os.environ.get(_env_key(model_name))
    if value:
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _provider_model_for(model_name: str) -> str:
    """Return the deployment's provider model id, e.g. anthropic/claude-opus-5-5."""
    try:
        from litellm.proxy import proxy_server

        router = getattr(proxy_server, "llm_router", None)
        if router is not None:
            for deployment in router.get_model_list() or []:
                if str(deployment.get("model_name", "")) == model_name:
                    params = deployment.get("litellm_params") or {}
                    return str(params.get("model") or model_name)
    except Exception:
        pass
    return model_name


def _note_role_for(model_name: str) -> str:
    """Role of the compaction note: user where the provider needs a user-first history."""
    provider_model = _provider_model_for(model_name).lower()
    if any(marker in provider_model for marker in USER_FIRST_MODEL_MARKERS):
        return "user"
    return "assistant"


def _message_text(message: dict) -> str:
    """Concatenate all text content in one message (str or content-part list)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "".join(parts)


def _estimate_tokens(messages: list) -> int:
    """Cheap character-based estimate; LiteLLM token counters need a client."""
    chars = sum(len(_message_text(m)) for m in messages if isinstance(m, dict))
    # ~4 characters per token for English/code, minus 5% safety margin
    # because the real tokenizer runs a few percent above this floor.
    return int(chars * 1.05) // 4


def _trim_to_budget(
    messages: list, budget: int, keep_last_n: int
) -> tuple[list, int]:
    """Drop the oldest non-system messages until the rest fits the budget.

    Always keeps the system prompts. Retention is maximized: it protects up
    to keep_last_n newest messages, shrinks that tail toward 1 only when the
    budget forces it, and starts the retained window at a user turn so
    assistant tool calls and their tool responses are never split apart.
    Returns (kept_messages, dropped_count).
    """
    system = [m for m in messages if isinstance(m, dict) and m.get("role") in ("system", "developer")]
    conversation = [
        m for m in messages if not (isinstance(m, dict) and m.get("role") in ("system", "developer"))
    ]
    if not conversation or _estimate_tokens(system + conversation) <= budget:
        return messages, 0
    keep = max(1, min(keep_last_n, len(conversation)))
    split = len(conversation) - keep
    while split > 0 and _estimate_tokens(system + conversation[split:]) > budget:
        split -= 1
    # Align the retained window to a user turn, never consuming the last
    # message, so orphaned assistant replies or tool results are not kept.
    while split > 0 and split < len(conversation) - 1 and conversation[split].get("role") != "user":
        split += 1
    dropped = split if split > 0 else 0
    if dropped == 0:
        return messages, 0
    return system + conversation[split:], dropped


class ContextCompaction(CustomLogger):
    """Pre-call hook that trims oversized conversation histories.

    Runs on every /v1/chat/completions request (stream and non-stream).
    Open WebUI resends the full history each turn, so a long chat grows
    past a small-window model and vLLM rejects it with a 400. This drops
    the oldest non-system turns until the prompt fits the deployment's
    declared window, inserting a short note so the model knows older
    turns were removed. It never modifies requests that already fit.

    Disabled per-request with metadata {"context_compaction": false}.
    """

    def _resolve(self, data: dict) -> tuple[Optional[int], int]:
        model_name = str(data.get("model", ""))
        window = _window_for(model_name)
        if window is None:
            return None, 0
        headroom = OUTPUT_HEADROOM_TOKENS
        requested_max_tokens = data.get("max_tokens") or data.get("max_completion_tokens")
        if isinstance(requested_max_tokens, int) and requested_max_tokens > 0:
            headroom = requested_max_tokens
        return window, window - headroom

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type: str,
    ) -> Optional[dict]:
        if call_type not in ("completion", "acompletion"):
            return data
        # The client owns compaction when it asks for it explicitly.
        metadata = data.get("litellm_metadata") or data.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("context_compaction") is False:
            return data
        messages = data.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            return data
        window, budget = self._resolve(data)
        if window is None or budget <= 0:
            return data
        if _estimate_tokens(messages) <= budget:
            return data
        keep_last_n = int(os.environ.get("CONTEXT_COMPACTION_KEEP_LAST_N", "6"))
        kept, dropped = _trim_to_budget(messages, budget, keep_last_n)
        if dropped == 0:
            return data
        note = {
            "role": _note_role_for(str(data.get("model", ""))),
            "content": (
                f"{NOTE_PREFIX}{dropped} older message(s) were removed; "
                f"rely on the conversation below and ask the user if you need details."
                f"{NOTE_SUFFIX}"
            ),
        }
        # Insert the note after the system prompts, before the kept history.
        insert_at = 0
        for index, message in enumerate(kept):
            if isinstance(message, dict) and message.get("role") not in ("system", "developer"):
                insert_at = index
                break
        data["messages"] = kept[:insert_at] + [note] + kept[insert_at:]
        data.setdefault("metadata", {})["context_compaction_applied"] = {
            "dropped_messages": dropped,
            "window": window,
        }
        return data


proxy_handler_instance = ContextCompaction()


def register_callback():
    if not any(isinstance(callback, ContextCompaction) for callback in litellm.callbacks):
        litellm.callbacks.append(proxy_handler_instance)


register_callback()
