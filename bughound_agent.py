import json
import re
from typing import Any, Dict, List, Optional

from reliability.risk_assessor import assess_risk


class BugHoundAgent:
    """
    BugHound runs a small agentic workflow:

    1) PLAN: decide what to look for
    2) ANALYZE: detect issues (heuristic triage, LLM only on High severity)
    3) ACT: propose a fix (heuristic for low/medium, LLM for high)
    4) TEST: run simple reliability checks
    5) REFLECT: decide whether to apply, iterate, or hand off to a human

    Iteration:
        Pass max_iterations > 1 to have the agent re-analyze its own fix and
        try again if the risk assessment is still unsafe. Pass human_feedback
        to inject a comment from a reviewer into the next fix attempt.
    """

    def __init__(self, client: Optional[Any] = None):
        # client should implement: complete(system_prompt: str, user_prompt: str) -> str
        self.client = client
        self.logs: List[Dict[str, str]] = []

    # ----------------------------
    # Public API
    # ----------------------------
    def run(
        self,
        code_snippet: str,
        max_iterations: int = 1,
        human_feedback: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.logs = []
        self._log("PLAN", f"Planning scan + fix workflow (max_iterations={max_iterations}).")

        original_code = code_snippet
        current_code = code_snippet
        iterations: List[Dict[str, Any]] = []

        last_issues: List[Dict[str, str]] = []
        last_fix: str = current_code
        last_risk: Dict[str, Any] = {}

        for i in range(1, max(1, max_iterations) + 1):
            if max_iterations > 1:
                self._log("PLAN", f"--- Iteration {i}/{max_iterations} ---")

            issues = self.analyze(current_code)
            self._log("ANALYZE", f"Found {len(issues)} issue(s).")

            # Only attach feedback to the first iteration's fix attempt.
            feedback_for_this = human_feedback if i == 1 else None

            fixed_code = self.propose_fix(current_code, issues, human_feedback=feedback_for_this)
            if fixed_code.strip() == "":
                self._log("ACT", "No fix produced (refused, error, or empty output).")

            risk = assess_risk(original_code=original_code, fixed_code=fixed_code, issues=issues)
            self._log("TEST", f"Risk assessed as {risk.get('level', 'unknown')} (score={risk.get('score', '-')}).")

            iterations.append(
                {
                    "iteration": i,
                    "input_code": current_code,
                    "issues": issues,
                    "fixed_code": fixed_code,
                    "risk": risk,
                }
            )
            last_issues, last_fix, last_risk = issues, fixed_code, risk

            if not issues:
                self._log("REFLECT", "No issues found. Stopping early.")
                break

            if risk.get("should_autofix"):
                self._log("REFLECT", "Fix appears safe enough to auto-apply under current policy.")
                break

            if i < max_iterations and fixed_code.strip() and fixed_code.strip() != current_code.strip():
                self._log("REFLECT", "Risk still elevated — re-analyzing the fixed code for further passes.")
                current_code = fixed_code
            else:
                self._log("REFLECT", "Stopping iteration. Human review recommended.")
                break

        return {
            "issues": last_issues,
            "fixed_code": last_fix,
            "risk": last_risk,
            "logs": self.logs,
            "iterations": iterations,
        }

    # ----------------------------
    # Workflow steps
    # ----------------------------
    def analyze(self, code_snippet: str) -> List[Dict[str, str]]:
        # Always triage with the cheap heuristic first.
        heuristic_issues = self._heuristic_analyze(code_snippet)
        has_high = self._has_high_severity(heuristic_issues)

        if not self._can_call_llm():
            self._log("ANALYZE", "Using heuristic analyzer (offline mode).")
            return heuristic_issues

        if not has_high:
            if heuristic_issues:
                self._log(
                    "ANALYZE",
                    f"Heuristic found {len(heuristic_issues)} low/medium issue(s). Skipping LLM to save quota.",
                )
            else:
                self._log("ANALYZE", "Heuristic found no issues. Skipping LLM to save quota.")
            return heuristic_issues

        self._log("ANALYZE", "High-severity heuristic hit. Escalating to LLM analyzer.")
        system_prompt = (
            "You are BugHound, a code review assistant. "
            "Return ONLY valid JSON. No markdown, no backticks."
        )
        user_prompt = (
            "Analyze this Python code for potential issues. "
            "Return a JSON array of issue objects with keys: type, severity, msg.\n\n"
            f"CODE:\n{code_snippet}"
        )

        try:
            raw = self.client.complete(system_prompt=system_prompt, user_prompt=user_prompt)
        except Exception as e:
            self._log("ANALYZE", f"API Error: {str(e)}. Falling back to heuristics.")
            return heuristic_issues

        llm_issues = self._parse_json_array_of_issues(raw)
        if llm_issues is None:
            self._log("ANALYZE", "LLM output was not parseable JSON. Falling back to heuristics.")
            return heuristic_issues

        # Merge: keep the heuristic high-severity hits and add anything new the LLM found.
        return self._merge_issues(heuristic_issues, llm_issues)

    def propose_fix(
        self,
        code_snippet: str,
        issues: List[Dict[str, str]],
        human_feedback: Optional[str] = None,
    ) -> str:
        if not issues:
            self._log("ACT", "No issues, returning original code unchanged.")
            return code_snippet

        has_high = self._has_high_severity(issues)

        if not self._can_call_llm() or not has_high:
            if self._can_call_llm() and not has_high:
                self._log("ACT", "Only low/medium severity. Using heuristic fixer to save LLM quota.")
            else:
                self._log("ACT", "Using heuristic fixer (offline mode).")
            return self._heuristic_fix(code_snippet, issues)

        self._log("ACT", "High severity present. Using LLM fixer.")
        system_prompt = (
            "You are BugHound, a careful refactoring assistant. "
            "Return ONLY the full rewritten Python code. No markdown, no backticks."
        )
        user_prompt = (
            "Rewrite the code to address the issues listed. "
            "Preserve behavior when possible. Keep changes minimal.\n\n"
            f"ISSUES (JSON):\n{json.dumps(issues)}\n\n"
            f"CODE:\n{code_snippet}"
        )
        if human_feedback:
            user_prompt += (
                "\n\nHUMAN REVIEWER FEEDBACK on the previous attempt — "
                "incorporate this guidance:\n"
                f"{human_feedback.strip()}"
            )

        try:
            raw = self.client.complete(system_prompt=system_prompt, user_prompt=user_prompt)
        except Exception as e:
            self._log("ACT", f"API Error: {str(e)}. Falling back to heuristic fixer.")
            return self._heuristic_fix(code_snippet, issues)

        cleaned = self._strip_code_fences(raw).strip()

        if not cleaned:
            self._log("ACT", "LLM returned empty output. Falling back to heuristic fixer.")
            return self._heuristic_fix(code_snippet, issues)

        return cleaned

    # ----------------------------
    # Heuristic analyzer/fixer
    # ----------------------------
    def _heuristic_analyze(self, code: str) -> List[Dict[str, str]]:
        issues: List[Dict[str, str]] = []

        if "print(" in code:
            issues.append(
                {
                    "type": "Code Quality",
                    "severity": "Low",
                    "msg": "Found print statements. Consider using logging for non-toy code.",
                }
            )

        if re.search(r"\bexcept\s*:\s*(\n|#|$)", code):
            issues.append(
                {
                    "type": "Reliability",
                    "severity": "High",
                    "msg": "Found a bare `except:`. Catch a specific exception or use `except Exception as e:`.",
                }
            )

        if "TODO" in code:
            issues.append(
                {
                    "type": "Maintainability",
                    "severity": "Medium",
                    "msg": "Found TODO comments. Unfinished logic can hide bugs or missing cases.",
                }
            )

        # ----- New heuristics -----

        if re.search(r"\b(eval|exec)\s*\(", code):
            issues.append(
                {
                    "type": "Security",
                    "severity": "High",
                    "msg": "Found `eval()` or `exec()`. These execute arbitrary code and are a major security risk.",
                }
            )

        if re.search(r"\bdef\s+\w+\s*\([^)]*=\s*(\[\]|\{\}|set\(\))", code):
            issues.append(
                {
                    "type": "Reliability",
                    "severity": "High",
                    "msg": "Mutable default argument (e.g. `def f(x=[])`). Defaults are shared across calls — use `None` and assign inside the function.",
                }
            )

        if re.search(r"(==|!=)\s*None\b", code):
            issues.append(
                {
                    "type": "Code Quality",
                    "severity": "Low",
                    "msg": "Comparing to `None` with `==`/`!=`. Use `is None` / `is not None`.",
                }
            )

        if re.search(r"^\s*from\s+\S+\s+import\s+\*", code, flags=re.MULTILINE):
            issues.append(
                {
                    "type": "Maintainability",
                    "severity": "Medium",
                    "msg": "Wildcard import (`from x import *`). Pollutes namespace and hides where names come from.",
                }
            )

        if re.search(r"\b(pdb\.set_trace\s*\(|breakpoint\s*\()", code) or re.search(
            r"^\s*import\s+pdb\b", code, flags=re.MULTILINE
        ):
            issues.append(
                {
                    "type": "Code Quality",
                    "severity": "High",
                    "msg": "Debugger breakpoint (`pdb.set_trace()` / `breakpoint()`) left in code.",
                }
            )

        # `open(...)` not used inside a `with` block — naive but useful signal.
        for m in re.finditer(r"\bopen\s*\(", code):
            preceding = code[max(0, m.start() - 60) : m.start()]
            if "with " not in preceding:
                issues.append(
                    {
                        "type": "Reliability",
                        "severity": "Medium",
                        "msg": "`open(...)` without a `with` block. File handle may leak on exception.",
                    }
                )
                break

        return issues

    def _heuristic_fix(self, code: str, issues: List[Dict[str, str]]) -> str:
        fixed = code
        types = {i.get("type") for i in issues}
        messages = " ".join(i.get("msg", "") for i in issues)

        if any(i.get("type") == "Reliability" and "bare" in i.get("msg", "").lower() for i in issues):
            fixed = re.sub(
                r"\bexcept\s*:\s*",
                "except Exception as e:\n        # [BugHound] log or handle the error\n        ",
                fixed,
            )

        if "Code Quality" in types and "print(" in fixed:
            if "import logging" not in fixed:
                fixed = "import logging\n\n" + fixed
            fixed = fixed.replace("print(", "logging.info(")

        # `== None` / `!= None` -> `is None` / `is not None`
        if "None" in messages:
            fixed = re.sub(r"==\s*None\b", "is None", fixed)
            fixed = re.sub(r"!=\s*None\b", "is not None", fixed)

        # Leave a marker for the harder cases the heuristic refuses to auto-rewrite.
        flagged_markers = []
        if any("eval" in i.get("msg", "").lower() or "exec" in i.get("msg", "").lower() for i in issues):
            flagged_markers.append("# [BugHound] SECURITY: replace eval()/exec() with a safe parser.")
        if any("mutable default" in i.get("msg", "").lower() for i in issues):
            flagged_markers.append("# [BugHound] Replace mutable default arg with `None` and assign inside the body.")
        if any("wildcard" in i.get("msg", "").lower() for i in issues):
            flagged_markers.append("# [BugHound] Replace `from x import *` with explicit imports.")
        if any("breakpoint" in i.get("msg", "").lower() or "pdb" in i.get("msg", "").lower() for i in issues):
            fixed = re.sub(r"^\s*import\s+pdb\s*\n", "", fixed, flags=re.MULTILINE)
            fixed = re.sub(r"\bpdb\.set_trace\s*\(\s*\)\s*", "", fixed)
            fixed = re.sub(r"\bbreakpoint\s*\(\s*\)\s*", "", fixed)

        if flagged_markers:
            fixed = "\n".join(flagged_markers) + "\n" + fixed

        return fixed

    # ----------------------------
    # Parsing + utilities
    # ----------------------------
    def _has_high_severity(self, issues: List[Dict[str, str]]) -> bool:
        return any(str(i.get("severity", "")).lower() == "high" for i in issues)

    def _merge_issues(
        self,
        primary: List[Dict[str, str]],
        secondary: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        seen = {(i.get("type"), i.get("msg")) for i in primary}
        merged = list(primary)
        for item in secondary:
            key = (item.get("type"), item.get("msg"))
            if key not in seen:
                merged.append(item)
                seen.add(key)
        return merged

    def _parse_json_array_of_issues(self, text: str) -> Optional[List[Dict[str, str]]]:
        text = text.strip()
        parsed = self._try_json_loads(text)
        if isinstance(parsed, list):
            return self._normalize_issues(parsed)

        array_str = self._extract_first_json_array(text)
        if array_str:
            parsed2 = self._try_json_loads(array_str)
            if isinstance(parsed2, list):
                return self._normalize_issues(parsed2)

        return None

    def _normalize_issues(self, arr: List[Any]) -> List[Dict[str, str]]:
        issues: List[Dict[str, str]] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            issues.append(
                {
                    "type": str(item.get("type", "Issue")),
                    "severity": str(item.get("severity", "Unknown")),
                    "msg": str(item.get("msg", "")).strip(),
                }
            )
        return issues

    def _try_json_loads(self, s: str) -> Any:
        try:
            return json.loads(s)
        except Exception:
            return None

    def _extract_first_json_array(self, s: str) -> Optional[str]:
        start = s.find("[")
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "[":
                depth += 1
            elif s[i] == "]":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
        return None

    def _strip_code_fences(self, text: str) -> str:
        text = text.strip()
        match = re.search(r"```(?:python)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1)
        return text

    def _can_call_llm(self) -> bool:
        return self.client is not None and hasattr(self.client, "complete")

    def _log(self, step: str, message: str) -> None:
        self.logs.append({"step": step, "message": message})
