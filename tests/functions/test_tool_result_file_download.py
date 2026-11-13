"""tool_result_file_download.py: the Open WebUI filter that turns chat-created files into downloads.

These tests cover the parts that run without Open WebUI: the inlet's
system-prompt guides and the outlet's choice of which files to attach.
The roots (/models, /outputs) point at temporary directories.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import re
import time

import pytest

FILTER_FILE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "ansible/roles/openwebui_stack/files/functions/tool_result_file_download.py"
)
_spec = importlib.util.spec_from_file_location("tool_result_file_download", FILTER_FILE)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

CHAT = "0b6f1c8e-7d0a-4c55-9b1e-2f3a4b5c6d7e"
OTHER_CHAT = "9f8e7d6c-5b4a-4321-8fed-cba987654321"


@pytest.fixture
def roots(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, pathlib.Path]:
    models, outputs = tmp_path / "models", tmp_path / "outputs"
    models.mkdir()
    outputs.mkdir()
    monkeypatch.setattr(module.Filter, "MODELS_DIR", str(models))
    monkeypatch.setattr(module.Filter, "OUTPUTS_DIR", str(outputs))
    monkeypatch.setattr(
        module.Filter, "PATH_RE", re.compile(re.escape(str(models)) + r"/[\w.\- /]+?\.(?:stl|3mf|step|stp)\b")
    )
    monkeypatch.setattr(
        module.Filter, "DOC_PATH_RE", re.compile(re.escape(str(outputs)) + r"/[\w.\- /]+?\.(?:docx|xlsx|pptx|pdf)\b")
    )
    return {"models": models, "outputs": outputs}


def write(path: pathlib.Path, age_seconds: float = 0.0) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"content")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def turn(*texts: str, started_at: float | None = None) -> dict:
    """An assistant message whose execute_code call printed the given texts."""
    output = []
    if started_at is not None:
        output = [
            {"type": "function_call", "name": "execute_code", "arguments": "{}", "started_at": started_at},
            {"type": "function_call_output", "output": [{"type": "input_text", "text": "\n".join(texts)}]},
        ]
    return {"role": "assistant", "id": "m1", "content": "Done.", "output": output}


def run_inlet(messages: list, metadata: dict | None = None) -> list:
    body = {"messages": messages}
    return asyncio.run(module.Filter().inlet(body, __metadata__=metadata))["messages"]


def test_inlet_names_the_chat_directory_when_code_interpreter_is_on(roots):
    messages = run_inlet(
        [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Make me a budget spreadsheet"}],
        {"chat_id": CHAT, "features": {"code_interpreter": True}},
    )
    system = messages[0]["content"]
    assert system.startswith("Be brief.")
    assert f"{roots['outputs']}/{CHAT}/quarterly-report.docx" in system
    assert system.count(module.Filter.OUTPUTS_GUIDE_MARKER) == 1


def test_inlet_leaves_the_prompt_alone_without_code_interpreter(roots):
    messages = run_inlet([{"role": "user", "content": "Hello"}], {"chat_id": CHAT, "features": {}})
    assert messages == [{"role": "user", "content": "Hello"}]


def test_inlet_still_adds_the_cad_guide_for_3d_chats(roots):
    messages = run_inlet([{"role": "user", "content": "Design an STL bracket"}], {"chat_id": CHAT})
    assert messages[0]["role"] == "system"
    assert module.Filter.CAD_GUIDE_MARKER in messages[0]["content"]
    assert module.Filter.OUTPUTS_GUIDE_MARKER not in messages[0]["content"]


def test_chat_ids_become_safe_directory_names():
    flt = module.Filter()
    assert flt._outputs_dir("local:abc/../x").endswith("/local_abc____x")
    assert flt._outputs_dir("") is None
    assert flt._outputs_dir(":/") is None


def test_documents_written_this_turn_are_attached(roots):
    chat_dir = roots["outputs"] / CHAT
    report = write(chat_dir / "report.docx")
    unmentioned = write(chat_dir / "budget.xlsx")
    write(chat_dir / "notes.txt")
    found = module.Filter()._detect_paths(turn(f"SAVED {report} 7", started_at=time.time() - 5), CHAT)
    assert sorted(found) == sorted([str(report.resolve()), str(unmentioned.resolve())])


def test_older_documents_are_not_attached_again_even_when_mentioned(roots):
    old = write(roots["outputs"] / CHAT / "report.docx", age_seconds=3600)
    message = turn(f"See {old}", started_at=time.time() - 5)
    assert module.Filter()._detect_paths(message, CHAT) == []


def test_documents_need_a_turn_that_ran_code(roots):
    report = write(roots["outputs"] / CHAT / "report.docx")
    message = {"role": "assistant", "content": f"I saved {report}", "output": []}
    assert module.Filter()._detect_paths(message, CHAT) == []


def test_another_chats_documents_are_never_attached(roots):
    theirs = write(roots["outputs"] / OTHER_CHAT / "salaries.xlsx")
    link = roots["outputs"] / CHAT / "copy.xlsx"
    link.parent.mkdir(parents=True)
    link.symlink_to(theirs)
    message = turn(f"SAVED {theirs} 7", f"SAVED {link} 7", started_at=time.time() - 5)
    assert module.Filter()._detect_paths(message, CHAT) == []


def test_legacy_code_interpreter_blocks_count_as_a_turn_that_ran_code(roots):
    report = write(roots["outputs"] / CHAT / "deck.pptx")
    message = {
        "role": "assistant",
        "content": "Here is the deck.",
        "output": [
            {
                "type": "open_webui:code_interpreter",
                "code": "prs.save(path)",
                "output": {"stdout": f"SAVED {report} 7"},
                "started_at": time.time() - 5,
            }
        ],
    }
    assert module.Filter()._detect_paths(message, CHAT) == [str(report.resolve())]


def test_cad_exports_keep_working_from_a_mention(roots):
    stl = write(roots["models"] / "bracket.stl", age_seconds=3600)
    message = {"role": "assistant", "content": f"Exported {stl}.", "output": []}
    assert module.Filter()._detect_paths(message, CHAT) == [str(stl.resolve())]
