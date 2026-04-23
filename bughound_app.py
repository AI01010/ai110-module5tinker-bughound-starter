import os
import difflib
import streamlit as st
from dotenv import load_dotenv

from bughound_agent import BugHoundAgent
from llm_client import GeminiClient, MockClient

# ----------------------------
# App setup
# ----------------------------
st.set_page_config(page_title="BugHound", page_icon="🐶", layout="wide")
st.title("🐶 BugHound")
st.caption("A tiny agent that analyzes code, proposes a fix, and runs simple reliability checks.")

# Load environment variables from .env if present
load_dotenv()

# ----------------------------
# Helpers
# ----------------------------
SAMPLE_SNIPPETS = {
    "print_spam.py": """def greet(name):
    print("Hello", name)
    print("Welcome!")
    return True
""",
    "flaky_try_except.py": """def load_data(path):
    try:
        data = open(path).read()
    except:
        return None
    return data
""",
    "mixed_issues.py": """# TODO: replace with real implementation
def compute(x, y):
    print("computing...")
    try:
        return x / y
    except:
        return 0
""",
    "cleanish.py": """import logging

def add(a, b):
    logging.info("Adding numbers")
    return a + b
""",
}


def render_diff(original: str, revised: str) -> str:
    """Return a unified diff string."""
    diff_lines = difflib.unified_diff(
        original.splitlines(),
        revised.splitlines(),
        fromfile="original",
        tofile="fixed",
        lineterm="",
    )
    return "\n".join(diff_lines)


def require_code_input(code: str) -> bool:
    if not code.strip():
        st.warning("Paste some code or load a sample snippet to begin.")
        return False
    return True


def build_client(mode: str, model_name: str, temperature: float):
    if mode == "Heuristic only (no API)":
        return MockClient(), "Using MockClient. No network calls."
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None, "Missing GEMINI_API_KEY. Add it to your .env file to use Gemini mode."
    return GeminiClient(model_name=model_name, temperature=temperature), "Gemini client ready."


def render_result(result: dict, original_code: str):
    issues = result.get("issues", [])
    fixed_code = result.get("fixed_code", "")
    risk = result.get("risk", {})
    logs = result.get("logs", [])

    res_left, res_right = st.columns([1, 1])

    with res_left:
        st.subheader("Detected issues")
        if not issues:
            st.success("No issues detected by the current analyzer.")
        else:
            for i, issue in enumerate(issues, start=1):
                issue_type = issue.get("type", "Issue")
                severity = issue.get("severity", "Unknown")
                msg = issue.get("msg", "").strip()
                st.markdown(f"**{i}. {issue_type} | {severity}**")
                if msg:
                    st.write(msg)

    with res_right:
        st.subheader("Risk report")
        if not risk:
            st.info("No risk report was produced.")
        else:
            score = risk.get("score", None)
            level = risk.get("level", "unknown")
            should_autofix = risk.get("should_autofix", None)
            reasons = risk.get("reasons", [])
            top_cols = st.columns(3)
            with top_cols[0]:
                st.metric("Risk level", str(level).upper())
            with top_cols[1]:
                st.metric("Score", "-" if score is None else int(score))
            with top_cols[2]:
                st.metric("Auto-fix?", "-" if should_autofix is None else ("YES" if should_autofix else "NO"))
            if reasons:
                st.write("**Reasons:**")
                for r in reasons:
                    st.write(f"- {r}")

    st.divider()

    if any("API Error" in log.get("message", "") for log in logs):
        st.warning("⚠️ API Request Failed: BugHound hit a limit or network error and used heuristic rules instead.")

    st.subheader("Proposed fix")
    if not fixed_code.strip():
        st.warning("No fix was produced. This can happen if the agent refused or had parsing errors.")
    else:
        fix_cols = st.columns([1, 1])
        with fix_cols[0]:
            st.text_area("Fixed code", value=fixed_code, height=320, key=f"fixed_view_{len(logs)}")
        with fix_cols[1]:
            diff_text = render_diff(original_code, fixed_code)
            st.text_area("Diff (unified)", value=diff_text, height=320, key=f"diff_view_{len(logs)}")

    st.divider()
    st.subheader("Agent trace")
    if not logs:
        st.info("No trace logs were produced.")
    else:
        for entry in logs:
            step = entry.get("step", "LOG")
            message = entry.get("message", "")
            st.write(f"**{step}:** {message}")


# ----------------------------
# Sidebar controls
# ----------------------------
st.sidebar.header("Settings")

mode = st.sidebar.selectbox(
    "Model mode",
    [
        "Heuristic only (no API)",
        "Gemini (requires API key)",
    ],
    help="Heuristic mode runs fully offline. Gemini mode is reserved for HIGH severity issues only.",
)

if mode == "Gemini (requires API key)":
    st.sidebar.warning(
        "⚠️ Gemini Free Tier: ~20 requests/day. BugHound will only call Gemini for HIGH-severity issues; "
        "low/medium stay on heuristics to save quota."
    )

model_name = st.sidebar.selectbox(
    "Gemini model",
    ["gemini-2.5-flash", "gemini-2.5-pro"],
    disabled=(mode != "Gemini (requires API key)"),
)

temperature = st.sidebar.slider(
    "Temperature",
    min_value=0.0,
    max_value=1.0,
    value=0.2,
    step=0.1,
    disabled=(mode != "Gemini (requires API key)"),
    help="Lower values tend to be more consistent. Higher values tend to be more creative.",
)

st.sidebar.divider()
st.sidebar.subheader("Agent loop")

max_iterations = st.sidebar.slider(
    "Max iterations",
    min_value=1,
    max_value=5,
    value=2,
    help="The agent can re-analyze its own fix and try again. Each pass may use another LLM call when high severity is found.",
)

human_review = st.sidebar.checkbox(
    "Human-in-the-loop review",
    value=False,
    help="Pause after each iteration so you can approve, reject, or refine the fix with feedback before continuing.",
)

st.sidebar.divider()

sample_choice = st.sidebar.selectbox(
    "Load a sample snippet",
    ["(none)"] + list(SAMPLE_SNIPPETS.keys()),
)

show_debug = st.sidebar.checkbox("Show debug details", value=False)

# ----------------------------
# Choose client
# ----------------------------
client, client_status = build_client(mode, model_name, temperature)
st.sidebar.info(client_status)

# ----------------------------
# Session state for the review loop
# ----------------------------
if "review_session" not in st.session_state:
    st.session_state.review_session = None  # dict: {original, current_code, history, iter, finalized}

# ----------------------------
# Main input
# ----------------------------
col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("Input code")
    if sample_choice != "(none)":
        default_code = SAMPLE_SNIPPETS[sample_choice]
    else:
        default_code = st.session_state.get("code_input", "")

    code_input = st.text_area(
        "Paste a Python snippet",
        value=default_code,
        height=320,
        placeholder="Paste code here...",
        label_visibility="collapsed",
    )
    st.session_state["code_input"] = code_input

    btn_cols = st.columns([2, 1])
    with btn_cols[0]:
        run_button = st.button("Run BugHound", type="primary", use_container_width=True)
    with btn_cols[1]:
        reset_button = st.button("Reset session", use_container_width=True)

with col_right:
    st.subheader("Outputs")
    st.write("Run the workflow to see issues, a proposed fix, and a risk report.")

if reset_button:
    st.session_state.review_session = None
    st.rerun()

# ----------------------------
# Run / Resume workflow
# ----------------------------
def start_run(original_code: str):
    if mode == "Gemini (requires API key)" and client is None:
        st.error("Gemini mode is selected, but no API key is available.")
        return

    agent = BugHoundAgent(client=client)
    # When human review is on, run one iteration at a time so the UI can pause.
    iters_for_call = 1 if human_review else max_iterations

    with st.spinner("BugHound is sniffing around..."):
        result = agent.run(original_code, max_iterations=iters_for_call)

    if human_review:
        st.session_state.review_session = {
            "original": original_code,
            "current_code": result.get("fixed_code", original_code),
            "history": [{"label": "Iteration 1", "result": result}],
            "iter": 1,
            "finalized": False,
        }
    else:
        # Single-shot result, render directly without session state.
        st.session_state.review_session = {
            "original": original_code,
            "current_code": result.get("fixed_code", original_code),
            "history": [{"label": "Auto run", "result": result}],
            "iter": iters_for_call,
            "finalized": True,
        }


def continue_iteration(feedback: str):
    sess = st.session_state.review_session
    agent = BugHoundAgent(client=client)
    next_iter = sess["iter"] + 1
    with st.spinner(f"Refining (iteration {next_iter})..."):
        result = agent.run(sess["current_code"], max_iterations=1, human_feedback=feedback or None)

    sess["current_code"] = result.get("fixed_code", sess["current_code"])
    sess["history"].append({"label": f"Iteration {next_iter}", "result": result, "feedback": feedback})
    sess["iter"] = next_iter
    if next_iter >= max_iterations:
        sess["finalized"] = True


if run_button:
    if not require_code_input(code_input):
        st.stop()
    start_run(code_input)

# ----------------------------
# Render session state
# ----------------------------
sess = st.session_state.review_session
if sess:
    st.divider()
    st.header("Results")

    # Show history of iterations
    for entry in sess["history"]:
        with st.expander(entry["label"], expanded=(entry is sess["history"][-1])):
            if entry.get("feedback"):
                st.info(f"**Reviewer feedback applied:** {entry['feedback']}")
            render_result(entry["result"], sess["original"])

    # Human review controls
    if human_review and not sess["finalized"]:
        st.divider()
        st.subheader("👤 Human review")
        st.write(
            f"Iteration {sess['iter']} of up to {max_iterations}. "
            "Approve to finalize, reject to discard, or send the agent back with a comment."
        )

        feedback = st.text_area(
            "Optional feedback for the next iteration",
            placeholder="e.g. 'Don't change the function signature', 'Use loguru not logging', ...",
            key=f"feedback_input_{sess['iter']}",
        )

        action_cols = st.columns(3)
        with action_cols[0]:
            if st.button("✅ Approve & finalize", type="primary", use_container_width=True):
                sess["finalized"] = True
                st.rerun()
        with action_cols[1]:
            if st.button("🔁 Refine with feedback", use_container_width=True, disabled=(sess["iter"] >= max_iterations)):
                continue_iteration(feedback)
                st.rerun()
        with action_cols[2]:
            if st.button("❌ Reject (revert to original)", use_container_width=True):
                sess["current_code"] = sess["original"]
                sess["finalized"] = True
                st.rerun()

        if sess["iter"] >= max_iterations:
            st.warning(
                f"Reached max iterations ({max_iterations}). Increase the limit in the sidebar to keep refining."
            )

    if sess["finalized"]:
        st.divider()
        st.subheader("Final code")
        st.code(sess["current_code"], language="python")

    if show_debug:
        st.divider()
        st.subheader("Debug payload")
        st.json(sess)
