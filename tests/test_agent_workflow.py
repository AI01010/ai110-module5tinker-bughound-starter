from bughound_agent import BugHoundAgent
from llm_client import MockClient


def test_workflow_runs_in_offline_mode_and_returns_shape():
    agent = BugHoundAgent(client=None)  # heuristic-only
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert isinstance(result, dict)
    assert "issues" in result
    assert "fixed_code" in result
    assert "risk" in result
    assert "logs" in result

    assert isinstance(result["issues"], list)
    assert isinstance(result["fixed_code"], str)
    assert isinstance(result["risk"], dict)
    assert isinstance(result["logs"], list)
    assert len(result["logs"]) > 0


def test_offline_mode_detects_print_issue():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Code Quality" for issue in result["issues"])


def test_offline_mode_proposes_logging_fix_for_print():
    agent = BugHoundAgent(client=None)
    code = "def f():\n    print('hi')\n    return True\n"
    result = agent.run(code)

    fixed = result["fixed_code"]
    assert "logging" in fixed
    assert "logging.info(" in fixed


def test_heuristic_fix_does_not_rewrite_print_inside_docstring():
    """
    Guardrail: blind str.replace would rewrite the literal `print()` inside a
    docstring (silently mutating documentation and triggering autofix=True on a
    non-issue). The tokenize-based rewrite should leave string content alone.
    """
    agent = BugHoundAgent(client=None)
    code = 'def f():\n    """Use print() responsibly."""\n    return 1\n'
    result = agent.run(code)
    fixed = result["fixed_code"]

    # The docstring text must be preserved verbatim.
    assert 'print()' in fixed, "docstring containing print() should not be rewritten"
    assert 'logging.info()' not in fixed, "should not inject a logging call from docstring text"
    # And we should not have added an `import logging` for a non-existent call site.
    assert 'import logging' not in fixed, "should not add import logging when no real print call exists"


def test_mock_client_forces_llm_fallback_to_heuristics_for_analysis():
    # MockClient returns non-JSON for analyzer prompts, so agent should fall back.
    # Use a HIGH-severity snippet so the new routing actually escalates to the LLM
    # (low/medium issues short-circuit and skip the LLM to save quota).
    agent = BugHoundAgent(client=MockClient())
    code = "def f():\n    try:\n        return 1\n    except:\n        return 0\n"
    result = agent.run(code)

    assert any(issue.get("type") == "Reliability" for issue in result["issues"])
    # Ensure we logged the fallback path
    assert any("Falling back to heuristics" in entry.get("message", "") for entry in result["logs"])
