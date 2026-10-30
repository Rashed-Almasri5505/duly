"""The assistant: Nemotron Super with tools, over the user's redacted memory.

The question is redacted before it is sent; the answer comes back with placeholders and the app restores the
real values on the user's screen. Each turn is limited in tool calls, time and cost, and every step is
recorded (model, tokens, latency, cost, tool, arguments, result) for the timeline in the app.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from duly.dates import describe
from duly.gateway import Source, ask_model
from duly.llm import BudgetExceeded, LLMError, TokenFactory, utc_now
from duly.privacy import PrivacyError
from duly.tools import Context, run_tool, summarize_result, tool_schemas

SYSTEM = """You are Duly, a private assistant for the user's paperwork: contracts, insurance policies, warranties,
leases, memberships, subscriptions and bills. Today is {today}.

Privacy: the user's personal details are replaced by placeholders such as [PERSON_1], [ACCOUNT_2] or [EMAIL_1].
Use them exactly as written, also in drafts; the app shows the real values to the user. Never ask the user for
the real value behind a placeholder, and never put placeholders or personal details in a web search.

How you work:
- Facts about the user's documents come only from your tools: list_deadlines for dates and amounts,
  search_documents for terms, conditions and exclusions. Quote the document's words when they decide the answer.
- Dates: use the readings the tools give ("due", "last_day_to_act", "dates_in_text"). Never read a numeric date
  such as 03/01/2027 yourself, and never compute one; date_math does arithmetic. Write every date the way the tools
  write it (for example "Sun 3 Jan 2027"), also when you quote a document that writes it with numbers.
- For how a company or authority handles something (claims, renewals, cancellations), use web_search.
  Prefer official sites; if you find none, say so.
- Write only links and e-mail addresses that appeared in a tool result in this conversation. The app removes any
  other link or address from your answer.
- If a request matches a skill, call use_skill first and follow every step.
- You cannot send anything. save_draft stores a draft for the user to review and send. Say a draft is saved only
  after save_draft returned its draft_id, and do not repeat the draft in your answer.
- Write placeholders as if they were the real values; do not call them placeholders.
- When the user states a lasting fact or preference about themselves, call remember.
- Be brief and practical: the answer first, then the steps, under 150 words unless the user asks for more.
  No legal or financial advice; say when the user should check with the provider.

Skills:
{skills}

The user's documents:
{documents}

Remembered facts:
{notes}"""


class Step(BaseModel):
    kind: Literal["model", "tool"]
    label: str
    model: str | None = None
    tool: str | None = None
    args: dict[str, Any] | None = None
    summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0


URL_RE = re.compile(r"https?://[^\s)\]>\"'`]+")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
EMAIL_IN_ANSWER_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def _norm_url(url: str) -> str:
    return url.rstrip(".,;:!?").rstrip("/").lower()


def ground(answer: str, evidence: str) -> tuple[str, list[str]]:
    """Remove links and e-mail addresses that do not appear in the evidence (tool results of this turn)."""
    removed: list[str] = []
    known = {_norm_url(u) for u in URL_RE.findall(evidence)}
    evidence_low = evidence.lower()

    def md(m: re.Match[str]) -> str:
        if _norm_url(m.group(2)) in known:
            return m.group(0)
        removed.append(m.group(2))
        return f"{m.group(1)} (link removed: not from a search result)"

    answer = MD_LINK_RE.sub(md, answer)

    def bare(m: re.Match[str]) -> str:
        url = m.group(0).rstrip(".,;:!?")
        if _norm_url(url) in known or "link removed" in url:
            return m.group(0)
        removed.append(url)
        return "(link removed: not from a search result)" + m.group(0)[len(url):]

    answer = URL_RE.sub(bare, answer)

    def mail(m: re.Match[str]) -> str:
        if m.group(0).lower() in evidence_low:
            return m.group(0)
        removed.append(m.group(0))
        return "(address removed: not in your documents or a search result)"

    return EMAIL_IN_ANSWER_RE.sub(mail, answer), removed


class Turn(BaseModel):
    question: str                    # as typed: stays on this computer
    question_sent: str               # what the model received
    answer: str = ""                 # with placeholders; restored on screen
    removed: list[str] = []          # links and addresses taken out of the answer (not from any tool result)
    steps: list[Step] = []
    sources: list[Source] = []
    drafts: list[str] = []
    notes: list[str] = []
    reminders: list[str] = []
    skills_used: list[str] = []
    cost_usd: float = 0.0
    seconds: float = 0.0
    error: str | None = None
    recorded_on: str | None = None   # set for recorded examples shown in offline mode
    at: str = Field(default_factory=utc_now)


def system_prompt(ctx: Context, skill: str | None = None) -> str:
    docs = ctx.store.documents()
    doc_lines = "\n".join(f"- {d.title}" + (f": {d.summary}" if d.summary else "") for d in docs) or "- none yet"
    notes = "\n".join(f"- [{n.id}] {n.text}" for n in ctx.store.notes()) or "- none yet"
    skills = "\n".join(f"- {s.name}: {s.description}" for s in ctx.skills.values()) or "- none"
    prompt = SYSTEM.format(today=describe(ctx.today, ctx.today).split(" (")[0], skills=skills, documents=doc_lines,
                           notes=notes)
    name = ctx.store.profile().name
    me = ctx.store.vault.lookup("PERSON", name) if name else None
    if me:
        prompt += (f"\n\nThe user is {me}. Contact details listed next to {me} in a document are the user's own: "
                   "never send a draft to them.")
    if skill:
        prompt += f"\n\nFor this request, follow the skill {skill}. Do every step:\n{ctx.skills[skill].body}"
    return prompt


ROUTER = """Pick the one skill that fits the user's request, or "none" if no skill fits.
Skills:
{skills}
Answer with JSON only, in exactly this shape: {{"skill": "<skill name or none>"}}"""


def route(ctx: Context, llm: TokenFactory, question_sent: str) -> tuple[str | None, Step | None]:
    """Nemotron Nano chooses the skill (cheap and fast); any failure just means no skill."""
    names = list(ctx.skills)
    if not names:
        return None, None
    schema = {"name": "route", "strict": True, "schema": {
        "type": "object", "properties": {"skill": {"type": "string", "enum": names + ["none"]}},
        "required": ["skill"], "additionalProperties": False}}
    skills = "\n".join(f"- {s.name}: {s.description}" for s in ctx.skills.values())
    messages = [{"role": "system", "content": ROUTER.format(skills=skills)}, {"role": "user", "content": question_sent}]
    try:
        res = ask_model(llm, ctx.store, "router", messages, purpose="choose a skill",
                        response_format={"type": "json_schema", "json_schema": schema}, thinking="off", max_tokens=60)
    except (LLMError, BudgetExceeded, PrivacyError):
        return None, None
    try:
        name = json.loads(res.content[res.content.find("{"):res.content.rfind("}") + 1]).get("skill")
    except (json.JSONDecodeError, AttributeError):
        name = None
    c = res.call
    chosen = name if name in ctx.skills else None
    return chosen, Step(kind="model", label="route", model=c.model, prompt_tokens=c.prompt_tokens,
                        completion_tokens=c.completion_tokens, latency_s=c.latency_s, cost_usd=c.cost_usd,
                        summary=f"chose skill {chosen}" if chosen else "no skill needed")


def run_turn(ctx: Context, llm: TokenFactory, question: str, cfg: dict[str, Any], *,
             history: list[Turn] | None = None, on_step: Callable[[Step], None] | None = None) -> Turn:
    """Answer one question. Never raises for model or budget errors: they end up in turn.error."""
    acfg = cfg["agent"]
    store = ctx.store
    t0 = time.perf_counter()
    sent, _ = store.vault.redact(question, source="your question")
    store.vault.save()
    turn = Turn(question=question, question_sent=sent)
    skill, route_step = route(ctx, llm, sent)
    if skill and skill not in ctx.skills_used:
        ctx.skills_used.append(skill)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt(ctx, skill)}]
    for past in (history or [])[-int(acfg["history_turns"]):]:
        if past.answer and not past.error:
            messages += [{"role": "user", "content": past.question_sent}, {"role": "assistant", "content": past.answer}]
    messages.append({"role": "user", "content": sent})
    tools = tool_schemas(list(ctx.skills))
    tool_calls = 0
    called: set[str] = set()
    nudges = 0
    evidence: list[str] = []

    def missing_steps() -> list[str]:
        needed = [t for name in ctx.skills_used for t in ctx.skills[name].requires_tools]
        return sorted({t for t in needed if t not in called and (t != "web_search" or ctx.allow_web)})

    def add(step: Step) -> None:
        turn.steps.append(step)
        turn.cost_usd += step.cost_usd
        if on_step is not None:
            on_step(step)

    if route_step is not None:
        add(route_step)
    try:
        for n in range(1, int(acfg["max_tool_calls"]) + 3):
            out_of_budget = (tool_calls >= int(acfg["max_tool_calls"]) or turn.cost_usd >= float(acfg["max_cost_usd"])
                             or time.perf_counter() - t0 > float(acfg["max_wall_s"]))
            if out_of_budget:
                messages.append({"role": "user", "content": "Answer now with what you have found; no more tools."})
            res = ask_model(llm, store, "agent", messages, purpose=f"assistant step {n}",
                            tools=None if out_of_budget else tools, thinking=acfg["thinking"],
                            max_tokens=int(acfg["max_tokens_per_turn"]))
            c = res.call
            add(Step(kind="model", label=f"step {n}", model=c.model, prompt_tokens=c.prompt_tokens,
                     completion_tokens=c.completion_tokens, latency_s=c.latency_s, cost_usd=c.cost_usd,
                     summary=("calls " + ", ".join(t.name for t in res.tool_calls)) if res.tool_calls else "answers"))
            missing = missing_steps()
            if not res.tool_calls and missing and not out_of_budget and nudges < 2:
                # A skill's required step was skipped: ask for it instead of accepting the answer.
                nudges += 1
                messages.append({"role": "assistant", "content": res.content or ""})
                messages.append({"role": "user", "content": "Not finished: the skill you are following still needs "
                                 f"{', '.join(missing)}. Call {'it' if len(missing) == 1 else 'them'} now, then answer."})
                add(Step(kind="tool", label="skill check", summary=f"asked for the skipped steps: {', '.join(missing)}"))
                continue
            if not res.tool_calls or out_of_budget:
                turn.answer = res.content or "I could not finish an answer. Please ask again in other words."
                break
            messages.append(res.assistant_message)
            for tc in res.tool_calls:
                try:
                    args = json.loads(tc.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                t1 = time.perf_counter()
                result = run_tool(ctx, tc.name, args)
                tool_calls += 1
                if "error" not in result:
                    called.add(tc.name)
                add(Step(kind="tool", label=tc.name, tool=tc.name, args=args, summary=summarize_result(tc.name, result),
                         latency_s=round(time.perf_counter() - t1, 3)))
                content = store.vault.mask_known(json.dumps(result, ensure_ascii=False))
                evidence.append(content)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
    except (LLMError, BudgetExceeded, PrivacyError) as exc:
        turn.error = str(exc)
    if turn.answer:
        turn.answer, turn.removed = ground(turn.answer, "\n".join(evidence))
    turn.sources = list(ctx.sources)
    turn.drafts, turn.notes, turn.reminders = list(ctx.drafts), list(ctx.notes), list(ctx.reminders)
    turn.skills_used = list(ctx.skills_used)
    turn.seconds = round(time.perf_counter() - t0, 1)
    return turn
