"""
ai_analyst.py — RAG assistant over reconciliation results.

Lets a finance user ask plain-English questions such as
    "Which missing credits are above 1 lakh and what should I do about them?"
and get an answer grounded in (a) today's reconciliation exceptions and
(b) the team's reconciliation runbook (knowledge/runbook.md).

Pipeline
  1. Build documents: one per exception row, one per status (counts/totals), one per runbook section
  2. Retrieve: TF-IDF (word 1-2 grams) cosine similarity + boost for the status the question mentions
  3. Generate: any OpenAI-compatible chat model (OpenAI, Azure, Groq, Ollama, vLLM ...) answers ONLY
     from the retrieved context and cites document ids.
     Without an API key it falls back to an extractive answer, so the pipeline still works offline.

Config (env vars): LLM_API_KEY, LLM_MODEL (default gpt-4o-mini), LLM_BASE_URL (optional)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from env_loader import load_env

load_env()
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

STATUSES = ["Amount Mismatch", "Missing in Bank", "Unknown Credit", "Duplicate Credit",
            "Matched (T+1 credit)", "Matched (amount + date)", "Matched"]
SYSTEM_PROMPT = (
    "You are a reconciliation analyst for a finance operations team. Answer the question using ONLY "
    "the context provided. Quote exact amounts, transaction IDs and UTRs from the context. Cite the "
    "document ids you used in square brackets, e.g. [row-12] [runbook-2]. If the context does not "
    "contain the answer, say so plainly. Keep the answer short and actionable."
)


@dataclass
class Doc:
    id: str
    source: str               # "row" | "summary" | "runbook"
    text: str
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------- documents
def _fmt(v):
    return "" if pd.isna(v) else (f"{v:,.2f}" if isinstance(v, float) else str(v))


def build_documents(results: pd.DataFrame, runbook_path: str | Path | None) -> list[Doc]:
    docs: list[Doc] = []
    exceptions = results[~results.Status.str.startswith("Matched")
                         | results.Status.isin(["Matched (T+1 credit)", "Matched (amount + date)"])]
    for i, r in exceptions.reset_index(drop=True).iterrows():
        amount = r["Gateway Net Amount"] if pd.notna(r["Gateway Net Amount"]) else r["Bank Credit"]
        text = (f"{r.Status}. Transaction {_fmt(r['Txn ID']) or 'n/a'}, UTR {_fmt(r.UTR) or 'n/a'}, "
                f"amount {_fmt(amount)}, bank credit {_fmt(r['Bank Credit']) or 'none'}, "
                f"difference {_fmt(r.Difference) or 'n/a'}. {r.Remarks}")
        docs.append(Doc(f"row-{i}", "row", text, {"status": r.Status, "amount": float(amount or 0)}))

    for s in STATUSES:
        part = results[results.Status == s]
        if part.empty:
            continue
        amt = part["Gateway Net Amount"].fillna(part["Bank Credit"])
        text = (f"Summary for {s}: {len(part)} items totalling {amt.sum():,.2f}. "
                f"Largest item {amt.max():,.2f}. Items above 1,00,000: {(amt > 100000).sum()}.")
        docs.append(Doc(f"summary-{s.lower().replace(' ', '-')}", "summary", text, {"status": s}))

    if runbook_path and Path(runbook_path).exists():
        sections = re.split(r"\n(?=## )", Path(runbook_path).read_text(encoding="utf-8"))
        for j, sec in enumerate(s for s in sections if s.startswith("## ")):
            title = sec.splitlines()[0].lstrip("# ").strip()
            docs.append(Doc(f"runbook-{j}", "runbook", sec.strip(), {"status": title}))
    return docs


# ---------------------------------------------------------------- retrieval
class Retriever:
    def __init__(self, docs: list[Doc]):
        self.docs = docs
        self.vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english")
        self.matrix = self.vec.fit_transform([d.text for d in docs])

    def search(self, query: str, k: int = 8) -> list[tuple[Doc, float]]:
        scores = cosine_similarity(self.vec.transform([query]), self.matrix).ravel()
        q = query.lower()
        mentioned = [s for s in STATUSES if s.lower().split(" (")[0] in q]
        for i, d in enumerate(self.docs):
            if any(d.meta.get("status", "").startswith(m) for m in mentioned):
                scores[i] += 0.25
            if d.source == "summary" and any(w in q for w in ("how many", "total", "count", "sum")):
                scores[i] += 0.15
        # amount filter: "above 1 lakh", "over 50,000"
        threshold = _amount_threshold(q)
        if threshold is not None:
            for i, d in enumerate(self.docs):
                if d.source == "row" and d.meta.get("amount", 0) >= threshold:
                    scores[i] += 0.2
        # diversified top-k: best runbook section + best summary are always kept, rows fill the rest
        order = [i for i in np.argsort(scores)[::-1] if scores[i] > 0]
        picked = []
        for src in ("runbook", "summary"):
            best = next((i for i in order if self.docs[i].source == src), None)
            if best is not None:
                picked.append(best)
        picked += [i for i in order if i not in picked][: max(k - len(picked), 0)]
        picked.sort(key=lambda i: -scores[i])
        return [(self.docs[i], float(scores[i])) for i in picked]


def _amount_threshold(q: str) -> float | None:
    m = re.search(r"(?:above|over|more than|greater than|>)\s*(?:inr|rs\.?|₹)?\s*([\d,\.]+)\s*(lakh|lac|k|cr|crore)?", q)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = {"lakh": 1e5, "lac": 1e5, "k": 1e3, "cr": 1e7, "crore": 1e7}.get(m.group(2) or "", 1)
    return value * unit


# ---------------------------------------------------------------- generation
class ReconAnalyst:
    def __init__(self, results: pd.DataFrame, runbook_path="knowledge/runbook.md"):
        self.retriever = Retriever(build_documents(results, runbook_path))
        self.model = os.getenv("LLM_MODEL", "gpt-4o-mini")
        self.client = None
        if os.getenv("LLM_API_KEY"):
            from openai import OpenAI
            self.client = OpenAI(api_key=os.getenv("LLM_API_KEY"), base_url=os.getenv("LLM_BASE_URL") or None)

    def ask(self, question: str, k: int = 8) -> dict:
        hits = self.retriever.search(question, k)
        context = "\n\n".join(f"[{d.id}] {d.text}" for d, _ in hits)
        if self.client:
            resp = self.client.chat.completions.create(
                model=self.model, temperature=0,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}])
            answer, mode = resp.choices[0].message.content, "llm"
        else:
            answer, mode = self._extractive(hits), "extractive"
        return {"question": question, "answer": answer, "mode": mode,
                "sources": [{"id": d.id, "source": d.source, "score": round(s, 3)} for d, s in hits]}

    @staticmethod
    def _extractive(hits) -> str:
        if not hits:
            return "No relevant information found in today's reconciliation."
        parts = []
        guidance = next((d for d, _ in hits if d.source == "runbook"), None)
        summary = next((d for d, _ in hits if d.source == "summary"), None)
        rows = [d for d, _ in hits if d.source == "row"][:5]
        if summary:
            parts.append(f"{summary.text} [{summary.id}]")
        if rows:
            parts.append("Relevant items:\n" + "\n".join(f"- {d.text} [{d.id}]" for d in rows))
        if guidance:
            body = " ".join(guidance.text.splitlines()[1:]).strip()
            parts.append(f"Recommended action: {body} [{guidance.id}]")
        return "\n\n".join(parts)


if __name__ == "__main__":
    import sys
    res_csv = sorted(Path("output").glob("results_*.csv"))[-1]
    analyst = ReconAnalyst(pd.read_csv(res_csv))
    q = " ".join(sys.argv[1:]) or "Which missing in bank items are above 1 lakh and what should I do?"
    out = analyst.ask(q)
    print(f"Q: {out['question']}\n\n{out['answer']}\n\n(mode={out['mode']})")
