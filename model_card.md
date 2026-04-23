# BugHound Mini Model Card (Reflection)

Completed after running BugHound in both Heuristic and Gemini modes, with a custom probe suite covering clean, dirty, and adversarial inputs.

---

## 1) What is this system?

**Name:** BugHound

**Core purpose:** A small agentic workflow that takes a short Python snippet, detects potential reliability/quality/security issues, proposes a minimal-diff fix, scores the risk of applying that fix, and decides whether the change is safe to auto-apply or should be deferred to human review.

**Intended users:** Students of agentic workflows and AI-system reliability. Not intended for production code review — the heuristics are intentionally narrow and the LLM gate is bound by free-tier quota.

---

## 2) How does it work?

The agent runs a five-step loop in [BugHoundAgent.run](bughound_agent.py#L36-L93):

1. **PLAN** — set the intent (single pass, or iterative re-analysis up to `max_iterations`) and log the planned scope.
2. **ANALYZE** — always start with the cheap heuristic triage (regex pass + AST pass merged). If a **High-severity** issue is found *and* a Gemini client is configured, escalate to the LLM analyzer for a second-opinion pass; merge results. If the heuristic finds only Low/Medium (or nothing), the LLM is **skipped to save quota**.
3. **ACT** — propose a fix. If the surfaced issues include a **High-severity** one and Gemini is available, the LLM rewrites the code under a "preserve behavior, minimal change" prompt. Otherwise the heuristic fixer runs (token-aware `print → logging.info`, bare `except` rewrite, `== None → is None`, marker comments for cases too risky to auto-rewrite).
4. **TEST** — `assess_risk` scores the diff (severity weights, length checks, return preservation, function-count change) and labels it `low | medium | high`.
5. **REFLECT** — `should_autofix` requires `level == "low"`. If iterating, the agent feeds the fixed code back into ANALYZE for the next pass and incorporates any human reviewer feedback into the next ACT prompt. Stops on `should_autofix=True`, no remaining issues, no change in fix output, or max iterations.

**Routing rule (the key behavior):** Gemini is only consulted when heuristics flag a High-severity issue. This keeps free-tier quota for cases where heuristics are most likely to be wrong or incomplete.

---

## 3) Inputs and outputs

**Inputs tested:**

| Snippet | Shape | Why included |
|---------|-------|--------------|
| `sample_code/cleanish.py` | Module with `import logging` and a 2-line function | Should be left alone |
| `sample_code/mixed_issues.py` | Function with `print`, bare `except:`, and a `TODO` | Multi-issue golden path |
| `sample_code/print_spam.py` | Function with several `print()` calls | Volume of low-severity issues |
| `sample_code/flagIT.py` | One-liner `def calculate(expression): return eval(expression)` | High-severity security trigger |
| `''` (empty) | Zero bytes | Boundary case |
| Comment-only file | Just `#` lines | Boundary case |
| `msg = "TODO: ..."` (string literal) | TODO inside string, not comment | Adversarial — tests regex precision |
| `"""Use print() responsibly."""` docstring | `print()` inside a docstring | Adversarial — tests fix safety |

**Outputs observed:**

- **Issue types**: Code Quality (print, `== None`, builtin shadowing), Reliability (bare except, mutable defaults, `open` without `with`, missing `requests` timeout), Maintainability (TODO, wildcard imports), Security (eval/exec, `subprocess(shell=True)`).
- **Fixes**: heuristic fixer produced minimal diffs (rewrote bare `except`, swapped `print` for `logging.info` at real call sites, prepended `# [BugHound]` marker comments for cases like `eval` that are unsafe to auto-rewrite). LLM fixer produced larger rewrites — sometimes restructuring code beyond the targeted issue.
- **Risk reports**: `cleanish.py` → low/100/autofix=YES. `mixed_issues.py` → high/30/autofix=NO (3 issues, one High-severity). `flagIT.py` → medium/60/autofix=NO (single High issue, −40). Empty file → high/0/autofix=NO.

---

## 4) Reliability and safety rules

Two rules from [reliability/risk_assessor.py](reliability/risk_assessor.py):

### Rule A — Empty-fix lockout ([:23-29](reliability/risk_assessor.py#L24-L30))

- **Checks**: if `fixed_code.strip() == ""`, return `score=0`, `level=high`, `should_autofix=False` immediately.
- **Why it matters**: an empty fix means the LLM refused, returned only a code fence, or hit a content filter. Without this lockout, downstream code would compute risk on an empty diff and could conceivably reach a "safe" verdict on what is effectively *deleting the user's code*.
- **False positive**: an input that genuinely should be empty (e.g., user pastes whitespace and expects "nothing to do") gets flagged as high-risk. Low impact in practice.
- **False negative**: a fix that returns a single space, `pass`, or `# noqa` would slip past this check while still being effectively empty — the rule only matches the exact `""` case after `.strip()`.

### Rule B — Function-count change penalty ([:62-69](reliability/risk_assessor.py#L62-L69), added in this session)

- **Checks**: parses `def NAME(` patterns from original and fixed; if the sets differ, deducts 30 points and lists the before/after sets in the reasons.
- **Why it matters**: silently dropping, renaming, or wrapping a function is a behaviour-altering change that diff-skimming reviewers commonly miss. The standard "preserve behavior" prompt does not stop the LLM from "helpfully" inlining or merging functions.
- **False positive**: legitimate refactors that intentionally rename or split a function (e.g., extracting a private helper) get penalized even though they are correct. The penalty is per-set-difference, so a rename is double-counted (one removed + one added).
- **False negative**: in-place body changes that preserve the signature go undetected — the function count and names stay identical even if the function now does something completely different.

---

## 5) Observed failure modes

### Failure 1 — Over-edit + unsafe confidence on a docstring (FIXED in this session)

**Snippet:**
```python
def f():
    """Use print() responsibly."""
    return 1
```

**What went wrong (pre-fix):** the regex heuristic flagged "print statements" because the substring `print(` appears in the docstring. The fixer then ran `code.replace("print(", "logging.info(")` blindly, producing `"""Use logging.info() responsibly."""` — silently mutating documentation. The risk score was 95 with `should_autofix=True`, so the agent recommended auto-applying a destructive edit to a perfectly correct file.

**What this revealed:** three of the four canonical reliability problems stacked on one input — false positive (regex doesn't respect string boundaries), over-editing (touched non-code bytes), unsafe confidence (autofix=True on a no-op-worthy file).

**Resolution:** replaced `str.replace` with a `tokenize`-based rewrite in [_rewrite_print_calls](bughound_agent.py#L494-L523) that only substitutes at real `NAME print` + `OP (` token pairs. Test [test_heuristic_fix_does_not_rewrite_print_inside_docstring](tests/test_agent_workflow.py#L41-L57) locks in the behaviour.

### Failure 2 — TODO inside a string literal triggers a "TODO comment" issue

**Snippet:**
```python
def f():
    msg = "TODO: this is in a string, not a comment"
    return msg
```

**What went wrong:** the heuristic matches `"TODO" in code` without distinguishing comments from string literals, so legitimate string content (e.g., a constant message in a UI prompt) is reported as an unfinished-logic warning. The fixer doesn't auto-rewrite TODOs, so impact is limited to noise — but it would surface during review and waste a reviewer's attention.

**What this revealed:** the same tokenization gap as Failure 1, just on a different rule. Same class of bug; the docstring fix did not generalize.

### Failure 3 — Format-fragility in LLM analyzer parsing

**What went wrong:** the agent's [_parse_json_array_of_issues](bughound_agent.py#L405-L416) tries `json.loads` then a bracket-extraction fallback, but [_normalize_issues](bughound_agent.py#L418-L431) silently coerces unknown keys (e.g., model returns `"sev"` instead of `"severity"`) to `"Unknown"`. A High-severity finding from Gemini with a renamed field would be downgraded to Unknown, which then fails the High-severity gate that decides whether to call the LLM fixer at all. The error is invisible — there is no log entry warning that fields were dropped.

---

## 6) Heuristic vs Gemini comparison

| Dimension | Heuristic | Gemini |
|-----------|-----------|--------|
| **Coverage** | Catches the specific patterns it was written for (print, bare except, TODO, eval, mutable defaults, etc.). Misses anything outside that list — e.g., a subtle off-by-one or a misused mutex. | Broader semantic coverage; can spot logic issues no regex/AST rule was written for. |
| **Precision** | High *for its targets*; low *across syntactic boundaries* (the docstring failure). | Variable. Depends on prompt and temperature; observed to occasionally restructure code beyond the listed issues. |
| **Determinism** | Same input → same output. | Even at temperature 0.2, output can shift run-to-run. |
| **Cost** | Free, instant. | ~1 LLM call per analyze + 1 per fix per iteration; free-tier capped ~20/day. |
| **Failure mode** | False positives from textual matching. | Format drift (missing JSON keys, code fences, extra commentary) and over-eager refactors. |
| **Fix style** | Surgical: rewrites only the matched pattern. | Often returns a fully rewritten module; the function-count signal in the risk assessor exists specifically to flag this. |
| **Risk-score agreement** | Agreed with intuition on the clean and obvious cases. Disagreed on the docstring case before the guardrail (it scored 95 a fix that should never have been proposed). | Tended to produce changes that the risk scorer correctly flagged as medium — the routing of "High severity → LLM" + "score < 75 → human review" works well together. |

---

## 7) Human-in-the-loop decision

**Scenario where the agent should refuse to auto-apply:** any fix that **changes the function inventory** of the file (renames, deletes, or adds a `def`). This is the canonical "subtle but high-impact" change — the diff often looks small (one function disappears or merges into another) but the public surface of the module changes, breaking imports and call sites elsewhere in the codebase that BugHound never sees.

**Trigger:** function-count change detected by the rule already added in [risk_assessor.py:62-69](reliability/risk_assessor.py#L62-L69). Currently it deducts 30 points; combined with even one Medium-severity issue (−20) the score drops to 50 (medium → no autofix). To make it a hard gate rather than a soft penalty, add an explicit override at the bottom of `assess_risk`:

```python
if original_defs != fixed_defs:
    should_autofix = False  # never auto-apply when function inventory changed
```

**Where it lives:** in `assess_risk` (risk-assessment layer), not the agent workflow. The agent's job is to produce candidate fixes; the assessor's job is to gate the auto-apply decision. Putting it in the assessor keeps the rule discoverable next to the other risk rules and means it applies regardless of whether the fix came from heuristics or the LLM.

**User-facing message:** "Fix changed the function inventory of the file (`{added}` added, `{removed}` removed). This may affect callers BugHound cannot see — review manually before applying."

---

## 8) Improvement idea

**Add an output-format validator with structured re-prompting for the LLM analyzer.**

Today, when Gemini returns malformed JSON or uses different keys (`sev` vs `severity`), the agent silently falls back to heuristics or coerces fields to `"Unknown"`. Both paths quietly drop information without surfacing the format failure.

**Proposal — three small additions:**

1. After parsing in [_parse_json_array_of_issues](bughound_agent.py#L405-L416), validate each issue against a strict schema: `severity` must be one of `{"Low", "Medium", "High"}` and `type` must be a non-empty string. Anything that fails is logged as a parsing failure rather than coerced to `"Unknown"`.
2. On the first format failure, **re-prompt once** with the model's previous output included as context: *"Your previous response was not valid JSON. Here is what you returned: `{raw}`. Return only a JSON array, no markdown."* Cap at one retry to keep quota predictable.
3. Add a test using `MockClient` that returns malformed JSON, asserting the agent (a) logs the format failure visibly and (b) does not silently coerce severity to `"Unknown"`.

**Why this is low complexity and high impact:** it's ~25 lines of code, no new dependencies, and closes the silent-failure path that today lets a real High-severity finding be downgraded to Unknown — which then misses the gate that would have escalated to the LLM fixer in the first place. Measurable as: count of "format failure" log entries vs. count of LLM analyzer calls. Today that ratio is unobservable; after this change it becomes a metric.
