"""Duly: a private assistant for your paperwork (Streamlit app).

Run locally with `streamlit run app.py`. On the hosted demo (DULY_DEMO_MODE=1) every visitor gets a private,
temporary copy of the fictional sample documents.
"""

from __future__ import annotations

import codecs
import html
import io
import json
import re
import shutil
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from duly.agent import Turn, run_turn
from duly.brief import Brief, morning_check
from duly.config import REPO_ROOT, default_data_dir, demo_mode, load_config
from duly.dates import describe
from duly.gateway import TavilySearch
from duly.ics import to_ics
from duly.llm import BudgetExceeded, LLMError, SpendLedger, TokenFactory, badge_of, in_judging_period
from duly.privacy import KINDS, PLACEHOLDER_RE, PrivacyError, Vault
from duly.reader import read_document
from duly.samples import SAMPLE_PERSON, install_samples, load_samples
from duly.skills import load_skills
from duly.store import KIND_LABELS, Draft, Profile, Store
from duly.tools import Context

st.set_page_config(page_title="Duly · private paperwork assistant", page_icon="🗂️", layout="wide")

CFG = load_config()
DEMO = demo_mode()
SESSIONS_DIR = REPO_ROOT / "data" / "sessions"
REPLAYS_DIR = REPO_ROOT / "data" / "replays"
SUGGESTIONS = [
    "My laptop screen started flickering. Am I still covered?",
    "I don't want to renew the gym. What do I do, and by when?",
    "What do I need to do in the next 30 days?",
    "How do I renew my car insurance without a gap?",
]
MAX_CHARS = int(CFG["reader"]["max_document_chars"])    # longer documents are cut, and the cut is shown
PAUSE_S = 300                    # how long live AI rests after Token Factory stopped answering
# Token Factory errors that mean it is not answering (the SDK has already retried them). Budget and privacy stops,
# and requests it refused, are not among them.
SERVER_ERRORS = ("APIConnectionError", "APITimeoutError", "InternalServerError")
READ_ERRORS = (LLMError, BudgetExceeded, PrivacyError, ValueError, TypeError, KeyError, AttributeError)

st.markdown("""
<style>
.block-container {padding-top: 2.2rem; max-width: 1250px;}
.duly-sub {color:#4B5A66; font-size:1.02rem; margin:-0.4rem 0 0.6rem 0;}
.chip {display:inline-block; padding:2px 10px; margin:0 6px 6px 0; border-radius:999px; font-size:0.82rem;
       background:#EEF4F4; color:#16212B; border:1px solid #D5E3E2;}
.chip.ok {background:#E7F6EF; border-color:#B9E3CF;}
.chip.warn {background:#FFF5E5; border-color:#F3D9A4;}
.ph {background:#DDEFEE; color:#0F5F59; border-radius:4px; padding:0 3px; font-family:ui-monospace,monospace;
     font-size:0.86em;}
.doc {white-space:pre-wrap; font-size:0.86rem; line-height:1.45; background:#FAFBFB; border:1px solid #E3E8EA;
      border-radius:8px; padding:10px 12px; max-height:430px; overflow-y:auto;}
.step {font-size:0.86rem; color:#33424D;}
.badge {display:inline-block; font-size:0.75rem; padding:1px 7px; border-radius:6px; margin-right:6px;
        background:#16212B; color:white;}
.badge.nano {background:#5B6B78;} .badge.tool {background:#0F766E;} .badge.check {background:#B26B00;}
.alert-overdue {border-left:4px solid #C2410C; padding-left:8px;}
.alert-today {border-left:4px solid #C2410C; padding-left:8px;}
.alert-this-week {border-left:4px solid #D97706; padding-left:8px;}
.alert-soon {border-left:4px solid #0F766E; padding-left:8px;}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------- session setup
def _prune_old_sessions(max_age_h: float = 24.0) -> None:
    if not SESSIONS_DIR.exists():
        return
    cutoff = time.time() - max_age_h * 3600
    for d in SESSIONS_DIR.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def get_store() -> Store:
    ss = st.session_state
    if "store_dir" not in ss:
        if DEMO:
            _prune_old_sessions()
            ss.store_dir = str(SESSIONS_DIR / uuid.uuid4().hex[:12])
            store = Store(Path(ss.store_dir))
            install_samples(store, date.today())
        else:
            ss.store_dir = str(default_data_dir())
    return Store(Path(ss.store_dir))


ss = st.session_state
store = get_store()
ledger = SpendLedger.from_config(CFG)
if "llm" not in ss:
    ss.llm = TokenFactory(CFG, ledger=ledger)
    ss.web = TavilySearch(CFG, ledger=ledger)
    ss.turns = []
    ss.live_turns = 0
llm: TokenFactory = ss.llm
web: TavilySearch = ss.web
skills = load_skills()
today = date.today()
turn_cap = int(CFG["budget"]["judging"]["live_turns_per_session"] if in_judging_period(CFG)
               else CFG["budget"]["live_turns_per_session"])


def live_status() -> tuple[bool, str]:
    if not llm.available:
        return False, "no Token Factory key is configured"
    if time.time() < ss.get("paused_until", 0.0):
        return False, "Token Factory is not answering; live AI resumes in a few minutes"
    try:
        ledger.check(0.002)
    except BudgetExceeded as exc:
        return False, str(exc)
    if DEMO and ss.live_turns >= turn_cap:
        return False, f"this visit has used its {turn_cap} live questions and document reads"
    return True, "ready"


def pause_if_down(error: str | None) -> None:
    """After a connection, timeout or server error, rest live AI for a few minutes, so the visitor sees the recorded
    conversations instead of one error after another."""
    if error and error.split(":", 1)[0].strip() in SERVER_ERRORS:
        ss.paused_until = time.time() + PAUSE_S


def flash(kind: str, text: str) -> None:
    """A message for the top of the sidebar on the next run: st.rerun() drops anything shown in this one."""
    ss.flash = ss.get("flash", []) + [(kind, text)]


def md(text: str | None) -> str:
    """Text for st.markdown, captions and labels, where two '$' signs would turn the amounts between them into a
    LaTeX formula."""
    return (text or "").replace("$", "\\$")


def R(text: str | None) -> str:
    """Real values back in (only ever for this screen)."""
    return store.vault.restore(text or "")


def highlight(redacted: str) -> str:
    out, cursor = [], 0
    for m in PLACEHOLDER_RE.finditer(redacted):
        out.append(html.escape(redacted[cursor:m.start()]))
        out.append(f'<span class="ph">{html.escape(m.group(0))}</span>')
        cursor = m.end()
    out.append(html.escape(redacted[cursor:]))
    return "".join(out)


def model_badge(model: str | None) -> str:
    if not model:
        return '<span class="badge tool">code</span>'
    name = badge_of(model)
    return f'<span class="badge{" nano" if "Nano" in name else ""}">{html.escape(name)}</span>'


# ---------------------------------------------------------------- header
live, why = live_status()
docs = store.documents()
hidden_total = len(store.vault.entries)
st.markdown("## Duly")
st.markdown('<div class="duly-sub">A private assistant for your paperwork. It remembers every deadline in your '
            'documents, checks them each morning, and drafts what you need, without showing your personal '
            'details to the AI model.</div>', unsafe_allow_html=True)
chips = [f'<span class="chip {"ok" if live else "warn"}">Live AI: {"ready" if live else "paused (" + md(html.escape(why)) + ")"}</span>',
         '<span class="chip">NVIDIA Nemotron on Nebius Token Factory</span>',
         f'<span class="chip">Web search: Tavily{"" if web.available else " (not configured)"}</span>',
         f'<span class="chip ok">{hidden_total} personal details hidden from the model</span>']
if DEMO:
    chips.append(f'<span class="chip">Sample data of a fictional person ({SAMPLE_PERSON}) · your copy is private to this visit</span>')
st.markdown("".join(chips), unsafe_allow_html=True)


# ---------------------------------------------------------------- sidebar
class UnreadableFile(ValueError):
    """An upload that cannot be turned into text; the message tells the visitor why."""


def pdf_text(raw: bytes) -> str:
    """The text layer of a PDF, page by page, until there is more than the store keeps."""
    from pypdf import PasswordType, PdfReader
    from pypdf.errors import FileNotDecryptedError

    pages: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(raw))
        # pypdf has already tried the empty password, which opens PDFs that only restrict printing or copying.
        locked = reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED
        if not locked:
            length = -1                          # the newlines that join the pages: one fewer than the pages
            for page in reader.pages:
                pages.append(page.extract_text() or "")
                length += len(pages[-1]) + 1
                if length > MAX_CHARS:           # the rest would be cut anyway, and long PDFs are slow to read
                    break
    except FileNotDecryptedError:
        locked = True
    except Exception as exc:  # a damaged file can raise almost anything: PdfReadError, EmptyFileError, KeyError ...
        raise UnreadableFile(f"Duly could not read that PDF ({type(exc).__name__}): the file may be empty, damaged "
                             "or not really a PDF.") from exc
    if locked:
        raise UnreadableFile("That PDF is locked with a password, so Duly cannot read it. Save a copy without the "
                             "password and add that copy.")
    return "\n".join(pages)


GARBLED_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f\ufffd]")    # NUL and other control characters, and U+FFFD
ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
MIXED_RE = re.compile(r"[A-Za-z][\u0600-\u06ff]|[\u0600-\u06ff][A-Za-z]")


def decode_text(raw: bytes) -> str:
    """A text file's characters: by its byte-order mark, else UTF-8, else the Arabic or the Western Windows code
    page."""
    if raw.startswith(codecs.BOM_UTF8):
        text = raw.decode("utf-8-sig", errors="replace")
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = raw.decode("utf-16", errors="replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1256")          # maps every byte, so it never fails
            try:
                western = raw.decode("cp1252")
            except UnicodeDecodeError:
                western = None
            # Western text (ä, ñ, ß ...) read as Arabic puts Arabic letters inside Latin words; Arabic text rarely does.
            if western is not None and 2 * len(MIXED_RE.findall(text)) > len(ARABIC_RE.findall(text)):
                text = western
    # A binary file renamed .txt decodes too (cp1256 takes any bytes): it shows up here as NULs and other controls.
    if len(GARBLED_RE.findall(text)) > max(2, len(text) // 100):
        raise UnreadableFile("That file does not look like plain text. Save it as UTF-8 text or as a PDF, and add it "
                             "again.")
    return text


def add_uploaded(file) -> None:
    raw = file.getvalue()
    try:
        text = pdf_text(raw) if file.name.lower().endswith(".pdf") else decode_text(raw)
    except UnreadableFile as exc:
        flash("error", str(exc))
        return
    if len(text.strip()) < 40:
        flash("error", "No text found in that file. Duly reads text and PDF files with a text layer; scanned images "
                       "need OCR, which is not built in yet.")
        return
    # No title from the file name: it often holds the owner's name. The store titles it from its first line.
    doc = store.add_document("", text, filename=file.name, max_chars=MAX_CHARS)
    if doc.truncated:
        flash("warning", f"“{R(doc.title)}” is long: only the first {MAX_CHARS:,} characters were read.")
    ok, reason = live_status()
    if ok:
        ss.live_turns += 1                       # a live reading counts against the visit's limit, like a question
        try:
            with st.spinner(f"Nemotron is reading “{md(R(doc.title))}” (it sees only the redacted copy)…"):
                read_document(store, llm, doc, today, CFG)
        except READ_ERRORS as exc:
            doc.read_status, doc.read_note = "failed", f"could not read it: {exc}"
            store.update_document(doc)
            flash("error", f"Nemotron could not read “{R(doc.title)}”: {exc}")
            pause_if_down(str(exc))
    else:
        doc.read_note = f"stored, not read: live AI is paused ({reason})"
        store.update_document(doc)


def forget_upload() -> None:
    """After a delete: forget the last upload and empty the uploader, which would otherwise add its file again."""
    ss.pop("last_upload", None)
    ss.upload_round = ss.get("upload_round", 0) + 1


with st.sidebar:
    for kind, text in ss.pop("flash", []):
        {"error": st.error, "warning": st.warning, "info": st.info}[kind](md(text))
    st.markdown("### Your documents")
    if docs:
        for d in docs:
            n = sum(d.hidden.values())
            note = (d.read_note or d.read_status) + (f" · only the first {MAX_CHARS:,} characters were read"
                                                     if d.truncated else "")
            st.markdown(f"**{md(html.escape(R(d.title) or 'Untitled document'))}**  \n<span style='color:#5B6B78;font-size:0.85rem'>"
                        f"{n} personal detail{'s' if n != 1 else ''} hidden · {md(html.escape(note))}</span>",
                        unsafe_allow_html=True)
    else:
        st.caption("No documents yet. Add your own, or load the fictional samples.")
    up = st.file_uploader("Add a document (text, Markdown or PDF)", type=["txt", "md", "pdf"],
                          key=f"upload-{ss.get('upload_round', 0)}",
                          help="Personal details are hidden on this computer before Nemotron reads the document.")
    if up is None:
        ss.pop("last_upload", None)
    elif ss.get("last_upload") != up.file_id:     # each upload has its own id, even of the same file again
        ss.last_upload = up.file_id
        add_uploaded(up)
        st.rerun()
    c1, c2 = st.columns(2)
    if c1.button("Load samples", help="Four fictional documents: a laptop receipt, a car insurance policy, a gym "
                 "membership and a lease, dated relative to today.", width="stretch"):
        names = {f"{s.slug}.md" for s in load_samples(today)}
        present = {d.filename for d in docs} & names
        if present == names:
            flash("info", "The sample documents are already loaded.")
        else:
            for d in docs:                       # some were deleted: replace the rest, so none is listed twice
                if d.filename in present:
                    store.delete_document(d.id)
            install_samples(store, today)
        st.rerun()
    if c2.button("Delete all", help="Deletes every document, memory, draft, placeholder and log entry.",
                 width="stretch"):
        store.wipe()
        ss.turns = []
        ss.pop("brief", None)
        ss.pop("flash", None)
        forget_upload()
        st.rerun()

    st.markdown("### Privacy")
    ss.allow_web = st.toggle("Allow web search (Tavily)", value=ss.get("allow_web", True),
                             help="Only search queries leave this computer, after the same privacy check. "
                                  "Off: the assistant answers from your documents alone.")
    profile = store.profile()
    if not DEMO:
        with st.form("profile"):
            name = st.text_input("Your name (always hidden)", value=profile.name or "")
            people = st.text_input("Other names to hide (comma-separated)", value=", ".join(profile.people))
            lead = st.number_input("Remind me this many days ahead", 1, 60, profile.lead_days)
            if st.form_submit_button("Save"):
                store.save_profile(Profile(name=name.strip() or None, lead_days=int(lead),
                                           people=[p.strip() for p in people.split(",") if p.strip()]))
                st.rerun()

    st.markdown("### Usage")
    spent = sum(c.cost_usd for c in llm.calls)
    st.caption(md(f"This visit: {len(llm.calls)} model calls, ${spent:.4f}; {web.live_calls} live web searches."))
    if DEMO:
        st.caption(md(f"Live questions and document reads left in this visit: {max(0, turn_cap - ss.live_turns)} of "
                      f"{turn_cap}. Shared demo budget left today: ${ledger.remaining_usd():.2f}."))


tab_chat, tab_dead, tab_docs, tab_mem, tab_how = st.tabs(
    ["Assistant", "Deadlines", "Documents & privacy", "Memory & skills", "How it works"])


# ---------------------------------------------------------------- assistant
def step_html(step, vault: Vault | None = None) -> str:
    restore = (vault if vault is not None else store.vault).restore
    if step.kind == "model":
        tokens = f"{step.prompt_tokens:,}+{step.completion_tokens:,} tokens · {step.latency_s:.1f} s · ${step.cost_usd:.5f}"
        return f'<div class="step">{model_badge(step.model)}{html.escape(step.summary)} <span style="color:#7A8791">· {tokens}</span></div>'
    if step.label == "skill check":
        return f'<div class="step"><span class="badge check">skill check</span>{html.escape(step.summary)}</div>'
    args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:80]}" for k, v in (step.args or {}).items())
    return (f'<div class="step"><span class="badge tool">{html.escape(step.tool or "")}</span>'
            f'{html.escape(restore(args))} → {html.escape(restore(step.summary or ""))}</div>')


USER_AVATAR, DULY_AVATAR = ":material/person:", ":material/folder_managed:"


def render_turn(turn: Turn, vault: Vault | None = None, drafts: list[Draft] | None = None,
                recorded: bool = False) -> None:
    """One question and its answer. A recorded turn brings the vault and the drafts it was recorded with."""
    vault = vault if vault is not None else store.vault

    def restore(text: str | None) -> str:
        return vault.restore(text or "")

    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(md(turn.question))
    with st.chat_message("assistant", avatar=DULY_AVATAR):
        if turn.error and not turn.answer:
            st.warning(md(f"I could not answer this time: {turn.error}"))
        else:
            st.markdown(md(restore(turn.answer)))
        unfinished = getattr(turn, "unfinished", None) or []
        if unfinished:
            st.caption(md("Duly could not complete these steps of the skill: " + ", ".join(map(str, unfinished)) + "."))
        if turn.removed:
            st.caption(f"Duly removed {len(turn.removed)} link(s) or address(es) from this answer because they did "
                       "not come from your documents or a search result.")
        meta = f"{len([s for s in turn.steps if s.kind == 'model'])} model calls · {turn.seconds:.0f} s · ${turn.cost_usd:.4f}"
        if turn.skills_used:
            meta += " · skill: " + ", ".join(turn.skills_used)
        if recorded:
            meta = f"Recorded live on {turn.at[:10]} · " + meta
        with st.expander(md(f"How Duly answered ({meta})")):
            st.markdown("".join(step_html(s, vault) for s in turn.steps), unsafe_allow_html=True)
            st.markdown("**What the model received as your question**")
            st.html(f'<div class="doc">{highlight(turn.question_sent)}</div>')
        if turn.sources:
            with st.expander(f"Sources (via Tavily) · {len(turn.sources)}"):
                for s in turn.sources:
                    st.markdown(f"- [{md(s.title)}]({s.url}) · {s.label}  \n  <span style='color:#5B6B78;font-size:0.85rem'>"
                                f"{md(html.escape(s.snippet[:220]))}</span>", unsafe_allow_html=True)
        drafts = store.drafts() if drafts is None else drafts
        for draft_id in turn.drafts:
            d = next((x for x in drafts if x.id == draft_id), None)
            if d is None:
                continue
            with st.expander(md(f"Draft ready to review: {restore(d.title)}")):
                st.caption("Nothing was sent. Copy it into your e-mail or the provider's form.")
                text = (f"To: {restore(d.to)}\n\n" if d.to else "") + restore(d.body)
                st.text_area("Draft", text, height=220, key=f"draft-{turn.at}-{d.id}", label_visibility="collapsed")


def recorded_store(day: date) -> Store:
    """The fictional samples as they were on the day the conversations were recorded, in a throwaway folder kept for
    this visit: a recording's placeholders are restored from its vault, never from the visitor's own."""
    stores = ss.get("recorded_stores", {})
    if day not in stores:
        folder = tempfile.TemporaryDirectory(prefix="duly-recorded-", ignore_cleanup_errors=True)
        sample = Store(Path(folder.name))
        install_samples(sample, day)
        stores[day] = (folder, sample)          # the folder is removed when the visit's session state goes
        ss.recorded_stores = stores
    return stores[day][1]


def show_recordings(why: str) -> None:
    """Conversations recorded live over the samples, shown while live AI is paused. Nothing is written to the
    visitor's store."""
    replays = {p: json.loads(p.read_text(encoding="utf-8")) for p in sorted(REPLAYS_DIR.glob("*.json"))}
    turns = {p: Turn(**data["turn"]) for p, data in replays.items()}
    if not turns:
        st.warning(md(f"Live AI is paused: {why}."))
        return
    days = sorted({datetime.fromisoformat(t.at).date() for t in turns.values()})
    st.warning(md(f"Live AI is paused: {why}. Below are conversations recorded live on "
                  f"{' and '.join(describe(d, d).split(' (')[0] for d in days)} with the same sample documents, so "
                  f"their dates are as of {'that day' if len(days) == 1 else 'the day each was recorded'}."))
    pick = st.radio("Recorded questions", list(turns), format_func=lambda p: turns[p].question, horizontal=False)
    turn = turns[pick]
    sample = recorded_store(datetime.fromisoformat(turn.at).date())
    render_turn(turn, vault=sample.vault, drafts=[Draft(**d) for d in replays[pick].get("drafts", [])],
                recorded=True)


with tab_chat:
    if not docs:
        st.info("Add a document in the sidebar, or click **Load samples** to try Duly with fictional documents.")
    for t in ss.turns:
        render_turn(t)
    live, why = live_status()
    if live:
        if not ss.turns:
            st.caption("Try one of these, or ask your own question below:")
        for row in (SUGGESTIONS[:2], SUGGESTIONS[2:]):          # two per row, so each question reads in full
            for col, text in zip(st.columns(2), row):
                if col.button(text, width="stretch", disabled=not docs):
                    ss.pending = text
        question = st.chat_input("Ask about your documents, deadlines, claims or renewals", disabled=not docs)
        question = question or ss.pop("pending", None)
        last = ss.turns[-1] if ss.turns else None
        if question and last is not None and last.question == question and last.answer and not last.error and \
                (datetime.now(timezone.utc) - datetime.fromisoformat(last.at)).total_seconds() < 60:
            question = None            # the same question again within a minute, answered: a double click
        if question:
            ctx = Context(store=store, today=today, skills=skills, web=web, allow_web=ss.allow_web)
            with st.chat_message("user", avatar=USER_AVATAR):
                st.markdown(md(question))
            with st.chat_message("assistant", avatar=DULY_AVATAR):
                status = st.status("Hiding personal details, then asking Nemotron…", expanded=True)

                def on_step(step) -> None:
                    status.markdown(step_html(step), unsafe_allow_html=True)

                turn = run_turn(ctx, llm, question, CFG, history=ss.turns, on_step=on_step)
                status.update(label=f"Done in {turn.seconds:.0f} s", state="complete", expanded=False)
            ss.turns.append(turn)
            ss.live_turns += 1
            pause_if_down(turn.error)
            st.rerun()
    else:
        show_recordings(why)


# ---------------------------------------------------------------- deadlines
with tab_dead:
    left, right = st.columns([1.05, 1])
    with left:
        st.markdown("#### Morning check")
        st.caption("Runs every morning from a scheduled task on your computer. Choosing what to report is plain "
                   "code; Nemotron Nano only writes the short brief.")
        c1, c2 = st.columns([1, 1])
        as_of = c1.date_input("As of", value=today, help="Try a later date to see how the brief changes.")
        if c2.button("Run the morning check", type="primary", width="stretch"):
            with st.spinner("Checking every deadline…"):
                ss.brief = morning_check(store, llm if live_status()[0] else None, as_of, CFG)
        brief: Brief | None = ss.get("brief")
        if brief is None and store.briefs():
            last = store.briefs()[-1]
            brief = Brief(**last)
        if brief is not None:
            st.markdown(f"**{describe(brief.day, brief.day).split(' (')[0]}** · written by "
                        f"{badge_of(brief.written_by) if brief.written_by != 'template' else 'a template (offline)'}")
            st.markdown(md(R(brief.text)))
            for a in brief.alerts:
                cls = "alert-" + a.urgency.replace(" ", "-")
                st.markdown(f'<div class="{cls}"><b>{html.escape(R(a.title))}</b> · {html.escape(R(a.source))}<br>'
                            f'<span style="color:#5B6B78;font-size:0.86rem">Last day to act: {describe(a.act_by, brief.day)}'
                            f'{" · " + html.escape(a.amount) if a.amount else ""}'
                            f'{" · renews automatically" if a.auto_renews else ""}</span></div>', unsafe_allow_html=True)
        else:
            st.caption("No brief yet today.")
    with right:
        st.markdown("#### Every date Duly is tracking")
        items = sorted((o for o in store.obligations() if o.status != "done" and o.due_date >= today),
                       key=lambda o: o.act_by)
        if items:
            fig = go.Figure()
            titles = {d.id: d.title for d in docs}
            for i, o in enumerate(items):
                label = R(o.title)[:38]
                fig.add_trace(go.Scatter(x=[o.act_by, o.due_date], y=[label, label], mode="lines+markers",
                                         marker=dict(size=[11, 8], symbol=["diamond", "circle"],
                                                     color=["#C2410C" if (o.act_by - today).days <= 7 else "#0F766E", "#5B6B78"]),
                                         line=dict(color="#B8C6C9", width=3), showlegend=False,
                                         hovertemplate=f"{html.escape(label)}<br>%{{x|%a %d %b %Y}}<extra></extra>"))
            fig.add_vline(x=datetime.combine(today, datetime.min.time()).timestamp() * 1000, line_dash="dot",
                          line_color="#16212B", annotation_text="today", annotation_position="top")
            fig.update_layout(height=max(220, 46 * len(items) + 80), margin=dict(l=10, r=10, t=30, b=10),
                              xaxis=dict(title="date (◆ last day to act, ● due)"), yaxis=dict(autorange="reversed"),
                              plot_bgcolor="white")
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        else:
            st.caption("No upcoming dates.")
    if items:
        rows = []
        for o in items:
            rows.append({"What": R(o.title), "From": R(o.counterparty or titles.get(o.doc_id or "", "reminder")),
                         "Last day to act": describe(o.act_by, today), "Due": o.due_date.strftime("%d %b %Y"),
                         "Notice": f"{o.notice_days} days" if o.notice_days else "",
                         "Amount": f"{o.amount:,.0f} {o.currency or ''}" if o.amount else "",
                         "Checked": "✓ in the document" if o.verified else "⚠ " + (o.check_note or "check")})
        st.dataframe(rows, width="stretch", hide_index=True)
        with st.expander("The document's own words for each date"):
            for o in items:
                st.markdown(md(f"**{html.escape(R(o.title))}** · {KIND_LABELS.get(o.kind, '')}  \n"
                               f"“{html.escape(R(o.date_quote or ''))}”"
                               + (f"  \n“{html.escape(R(o.rule_quote))}”" if o.rule_quote else "")))
        lead = store.profile().lead_days
        st.download_button("Add these dates to my calendar (.ics)", to_ics(items, store.vault, lead, today),
                           file_name="duly-deadlines.ics", mime="text/calendar",
                           help=f"One event on each last day to act, with a reminder {lead} days before.")


# ---------------------------------------------------------------- documents & privacy
with tab_docs:
    st.markdown("#### What stays here, and what the model reads")
    st.caption("Personal details are found by code on this computer (patterns with checksums, labelled fields, the "
               "names you list) and swapped for placeholders. Nemotron only ever receives the right-hand copy.")
    for d in docs:
        n = sum(d.hidden.values())
        kinds = ", ".join(f"{v} {KINDS[k]}{'s' if v > 1 else ''}" for k, v in sorted(d.hidden.items()))
        with st.expander(md(f"{R(d.title)} · {n} details hidden" + (f" ({kinds})" if kinds else ""))):
            if d.truncated:
                st.caption(f"Only the first {MAX_CHARS:,} characters were read; the rest of the file was not kept, "
                           "so dates after that point are not tracked.")
            if d.read_by or d.read_note:
                st.caption(md(f"Read by {badge_of(d.read_by) if d.read_by else '—'} · {d.read_note or d.read_status}"
                              + (f" · {R(d.summary)}" if d.summary else "")))
            c1, c2 = st.columns(2)
            c1.markdown("**Your copy** (stays on this computer)")
            c1.html(f'<div class="doc">{html.escape(d.original)}</div>')
            c2.markdown("**What Nemotron reads**")
            c2.html(f'<div class="doc">{highlight(d.redacted)}</div>')
            b1, b2, _ = st.columns([1, 1, 2])
            if b1.button("Read again with Nemotron", key=f"read-{d.id}", disabled=not live_status()[0]):
                ss.live_turns += 1                   # a live reading counts against the visit's limit
                try:
                    with st.spinner("Reading the redacted copy…"):
                        read_document(store, llm, d, today, CFG)
                except READ_ERRORS as exc:
                    d.read_status, d.read_note = "failed", f"could not read it again: {exc}"
                    store.update_document(d)
                    flash("error", f"Nemotron could not read “{R(d.title)}” again: {exc}")
                    pause_if_down(str(exc))
                st.rerun()
            if b2.button("Delete this document", key=f"del-{d.id}"):
                store.delete_document(d.id)
                forget_upload()
                st.rerun()
    st.markdown("#### The placeholder vault")
    show = st.toggle("Show the real values", value=False)
    vault_rows = [{"Placeholder": e.placeholder, "What it is": KINDS[e.kind],
                   "Value": e.value if show else store.vault.masked(e.placeholder), "First seen in": R(e.source)}
                  for e in store.vault.entries.values()]
    if vault_rows:
        st.dataframe(vault_rows, width="stretch", hide_index=True)
    st.markdown("#### Everything that left this computer")
    log = store.outbound()
    if log:
        blocked = sum(r.check == "blocked" for r in log)
        st.caption(f"{len(log)} calls: {sum('Token Factory' in r.destination for r in log)} to Nebius Token Factory, "
                   f"{sum('Tavily' in r.destination for r in log)} to Tavily. Before each call Duly checked that no value "
                   f"from the vault was in it" + (f"; {blocked} were blocked and never sent." if blocked else ".")
                   + " The check can only stop details the detectors found, so read what was sent below.")
        for r in reversed(log[-40:]):
            when = r.at[11:19]
            with st.expander(md(f"{when} · {r.destination} · {r.purpose} · {r.chars:,} characters · "
                                f"{r.placeholders} placeholders · {'✓ no vault value in it' if r.check == 'passed' else '✗ blocked'}")):
                payload = r.payload
                instructions = ""
                if payload.startswith("[system]") and "\n\n[" in payload:
                    # Duly's own instructions come first and are the same every time: folded, the rest first.
                    instructions, _, rest = payload.partition("\n\n[")
                    payload = "[" + rest
                st.html(f'<div class="doc">{highlight(payload)}</div>' + (
                    f'<details style="margin-top:6px"><summary style="color:#5B6B78;font-size:0.85rem;cursor:pointer">'
                    f"Duly's instructions to the model ({len(instructions):,} characters, the same in every call)"
                    f'</summary><div class="doc">{highlight(instructions)}</div></details>' if instructions else ""))
    else:
        st.caption("Nothing has left this computer yet in this visit.")


# ---------------------------------------------------------------- memory & skills
with tab_mem:
    m1, m2 = st.columns(2)
    with m1:
        st.markdown("#### What Duly remembers about you")
        notes = store.notes()
        if notes:
            for n in notes:
                a, b = st.columns([5, 1])
                a.markdown(f"{md(html.escape(R(n.text)))}  \n<span style='color:#7A8791;font-size:0.8rem'>{n.source} · "
                           f"{n.created_at[:10]}</span>", unsafe_allow_html=True)
                if b.button("Forget", key=f"forget-{n.id}"):
                    store.forget_note(n.id)
                    st.rerun()
        else:
            st.caption("Nothing yet. Tell the assistant a preference (\"remind me two weeks ahead\") or add one here.")
        with st.form("add-note", clear_on_submit=True):
            fact = st.text_input("Add something to remember")
            if st.form_submit_button("Remember") and fact.strip():
                store.add_note(fact.strip(), source="you")
                st.rerun()
        st.markdown("#### Drafts")
        drafts = store.drafts()
        for d in reversed(drafts):
            with st.expander(md(R(d.title))):
                text = (f"To: {R(d.to)}\n\n" if d.to else "") + R(d.body)
                st.text_area("Draft", text, height=200, key=f"mem-draft-{d.id}", label_visibility="collapsed")
                a, b = st.columns(2)
                a.download_button("Download", text, file_name=f"{d.id}.txt", key=f"dl-{d.id}")
                if b.button("Delete", key=f"deldraft-{d.id}"):
                    store.delete_draft(d.id)
                    st.rerun()
        if not drafts:
            st.caption("No drafts yet.")
    with m2:
        st.markdown("#### Skills")
        st.caption("Reusable procedures in the open Agent Skills format (skills/<name>/SKILL.md). Nemotron Nano picks "
                   "one for each question; Duly then checks that the skill's required tools were really used.")
        for s in skills.values():
            with st.expander(f"{s.name}"):
                st.markdown(f"_{s.description}_")
                if s.requires_tools:
                    st.caption("Must use: " + ", ".join(s.requires_tools))
                st.markdown(s.body)
        reminders = [o for o in store.obligations() if o.source == "reminder"]
        if reminders:
            st.markdown("#### Reminders you or the assistant added")
            for o in reminders:
                a, b = st.columns([5, 1])
                a.markdown(md(f"{html.escape(R(o.title))} · {describe(o.due_date, today)}"))
                if b.button("Remove", key=f"rem-{o.id}"):
                    store.delete_obligation(o.id)
                    st.rerun()


# ---------------------------------------------------------------- how it works
with tab_how:
    st.markdown("#### How Duly works")
    st.graphviz_chart("""
digraph {
  rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#F3F6F6", color="#B8C6C9", fontname="Helvetica", fontsize=11];
  edge [color="#5B6B78", fontname="Helvetica", fontsize=9];
  subgraph cluster_local { label="Your computer"; style="rounded"; color="#0F766E"; fontname="Helvetica";
    docs [label="Your documents\\n(originals)"]; fw [label="Privacy firewall\\nplaceholders + vault", fillcolor="#DDEFEE"];
    mem [label="Memory\\ndeadlines, facts, drafts"]; tools [label="Tools + skills\\n(SKILL.md)"]; gate [label="Gateway\\nleak check + log", fillcolor="#DDEFEE"];
    sched [label="Morning check\\n(scheduled task)"] }
  tf [label="Nebius Token Factory\\nNemotron Nano: route, brief\\nNemotron Super: read, act"]; tav [label="Tavily\\nofficial procedures"];
  docs -> fw -> mem; mem -> tools; tools -> gate; sched -> mem; gate -> tf [label="redacted text only"]; gate -> tav [label="search queries"];
}""")
    used: dict[str, list] = {}
    for c in llm.calls:
        used.setdefault(c.model, []).append(c)
    st.markdown("""
| Model (on Nebius Token Factory) | Job | Why this model |
|---|---|---|
| Nemotron 3 Nano 30B-A3B | picks the skill for each question; writes the morning brief | small and fast: about a second, a few thousandths of a cent |
| Nemotron 3 Super 120B-A12B | reads documents (JSON schema); runs the assistant with tools | reliable tool calling and structured output |
""")
    if used:
        st.caption(md("Measured in this visit: " + "; ".join(
            f"{badge_of(m)}: {len(cs)} call{'s' if len(cs) != 1 else ''}, median "
            f"{sorted(c.latency_s for c in cs)[len(cs) // 2]:.1f} s, ${sum(c.cost_usd for c in cs):.5f}"
            for m, cs in used.items())))
    st.markdown("""
**Privacy, in five rules**
1. Code on your computer finds personal details (IBANs and cards with checksums, ID and phone numbers, e-mails, labelled
   fields such as *Policy number* or *Address*, and the names you list) and swaps them for placeholders.
2. The vault that maps placeholders to values stays in your data folder. Real values come back only on your screen.
3. Every outbound call goes through one gateway that blocks it if any vault value is in the payload, and logs the exact
   text sent (see *Documents & privacy*).
4. Model-written dates are not trusted: code finds every date in a document, Nemotron only says what each one means, and
   each deadline shows the document's own words.
5. Links and e-mail addresses in an answer must come from a document or a search result; others are removed.

**Always on.** `scripts/morning.py` runs the morning check from Windows Task Scheduler or cron. The same tools also run
as an MCP server, for agents such as Hermes Agent (tested with an MCP client). Start it with `python -m duly.mcp_server`.

**Limits.** Duly reads text and PDFs with a text layer (no OCR yet). The detectors are rules, so an unusual format can be
missed; the vault, the leak check and the log make that visible. Duly drafts but never sends, and it does not give legal or
financial advice.
""")
