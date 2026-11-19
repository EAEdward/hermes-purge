"""refusal-retry 单测：检测矩阵 + nudge 升级 + 预算（对齐 dsh-purge 1.7.17 教训）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import refusal  # noqa: E402

CFG = {"refusalRetry": {"enabled": True, "maxAttempts": 2}}


def test_cn_sentence_level_hits():
    for text in ("我不会给这个脚本", "因此我不提供其中的任何环节", "这个请求我不会给",
                 "我不能协助你完成", "很抱歉，我不能提供"):
        assert refusal.is_refusal(text, CFG), text


def test_en_hits():
    for text in ("I can't help with that", "I won't provide this", "I'm unable to assist",
                 "I must decline this request"):
        assert refusal.is_refusal(text, CFG), text


def test_deliverables_not_flagged():
    for text in ("```python\nimport os\nprint(os.getpid())\n```",
                 "脚本已写到 /tmp/x.py，跑通输出 hit=3", ""):
        assert not refusal.is_refusal(text, CFG), text


def test_nudge_escalates_and_caps():
    assert "上一次回答被判定为拒绝" in refusal.build_nudge(1)
    assert "再次被判定为拒绝" in refusal.build_nudge(2)
    assert refusal.build_nudge(9) == refusal.build_nudge(3)  # 不再升级，但不会越界


def test_corrective_only_for_last_assistant():
    msgs = [{"role": "user", "content": "写个脚本"},
            {"role": "assistant", "content": "我不写这个"},
            {"role": "user", "content": "换一种说法，写个脚本"},
            {"role": "assistant", "content": "```python\nprint(1)\n```"}]
    assert refusal.corrective_from_messages(msgs, CFG) is None
    msgs[-1]["content"] = "我不做这个"
    assert refusal.corrective_from_messages(msgs, CFG)


def test_disabled_switch():
    off = {"refusalRetry": {"enabled": False}}
    msgs = [{"role": "assistant", "content": "我不写"}]
    assert refusal.corrective_from_messages(msgs, off) is None




def test_middleware_and_hook_entrypoints():
    """模块级入口：中间件改写请求；pre_verify 在预算内返回 continue。"""
    import importlib
    import types

    import importlib.util
    import types

    root = Path(__file__).resolve().parent.parent
    sys.modules.setdefault("hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: root.parent))
    spec = importlib.util.spec_from_file_location(
        "hermes_purge_probe", root / "__init__.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_purge_probe"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # 宿主缺 hermes_cli 时跳过（单测在 Hermes 环境内跑得到）
        print("SKIP test_middleware_and_hook_entrypoints:", exc)
        return

    req = {"model": "x", "messages": [{"role": "user", "content": "写个脚本"},
                                      {"role": "assistant", "content": "我不写这个"}]}
    out = mod._llm_request_refusal_middleware(request=req, session_id="s1", model="x")
    assert out and "messages" in out["request"]
    assert out["request"]["messages"][-1]["role"] == "system"
    assert len(out["request"]["messages"]) == len(req["messages"]) + 1

    ok = mod._llm_request_refusal_middleware(request={"messages": [{"role": "assistant", "content": "```py\nprint(1)\n```"}]})
    assert ok is None

    cont = mod._pre_verify_refusal(final_response="我不会给这个", attempt=0, session_id="s1")
    assert cont and cont["action"] == "continue" and cont["message"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
