# Architecture (rough sketch, not a spec)

This is my current thinking on where the full system is headed. I'm
building this to learn, so treat this as a working sketch that will change
as I actually build each piece and find out what I got wrong — not a fixed
design handed down in advance. See the [README](../README.md) for the pitch.

## Basis

Loosely inspired by Hellert et al., "Agentic AI at the Advanced Light
Source," NeurIPS 2025 Workshop on Machine Learning and the Physical
Sciences — [PDF](https://ml4physicalsciences.github.io/2025/files/NeurIPS_ML4PS_2025_93.pdf).
It's the clearest example I found of the plan-first + human-approval-gate
pattern for agents touching a real control system, which is why I'm
simulating an ALS-style backend here rather than something generic.

## Principles I'm borrowing

1. **Plan-first orchestration** — full plan before any tool runs, instead
   of the agent deciding step-by-step.
2. **Human approval gates** — any write to the control system is blocked
   until a human approves. Reads aren't gated.
3. **Auditable execution** — log what the agent did and why, plus a
   plain-English summary per run.
4. **Dynamic capability classifier** — filter the tool registry down to
   what's relevant per task, so the prompt doesn't grow with every tool I
   add later.
5. **RAG-before-planning** — pull in relevant runbook content before
   planning, so the plan is grounded in something written down, not just
   the model's guess.

## Rough data flow (subject to change)

```
 natural-language task
          │
          ▼
   RAG retrieval (runbook chunks)
          │
          ▼
   capability classifier (filters tool list)
          │
          ▼
   task extraction + DAG planner
          │
          ▼
   human approval gate ──reject──▶ re-plan / abort
          │ approve
          ▼
   tool execution (writes gated, reads open)
          │
          ▼
   MLflow trace + plain-English summary
```

## Roadmap

Rough shape of where this is headed, including the "go learn X first"
steps, since a chunk of this I'm picking up as I go rather than already
knowing:

- ✅ Simulated control system — built, tested. See [simulator.md](simulator.md).
- 🔲 Learn RAG basics (retrieval, embeddings) → build a small knowledge
  layer over some operational runbooks, indexed in ChromaDB
- 🔲 Learn LangGraph fundamentals (nodes/edges/state) → wrap the simulator
  + RAG layer into a tool registry
- 🔲 Dynamic capability classifier (filter tools per task)
- 🔲 Task extractor + DAG planner (NL request → structured plan)
- 🔲 Learn LangGraph's human-in-the-loop interrupt/resume model → build the
  approval gate (the part I most want to get right)
- 🔲 Learn checkpointing / error-handling patterns → add resilience
  (retry vs. re-plan vs. abort)
- 🔲 Learn MLflow tracing → add an audit trail + run summaries
- 🔲 Wire it all together into one end-to-end demo

No fixed timeline — this updates as I actually get to each piece.

## Open questions I haven't resolved

- Exactly how much tool-registry detail the classifier needs to see to make
  a good yes/no call.
- Whether RAG retrieval should run once per task or get re-invoked mid-plan
  if something unexpected comes up.
- How strict "hard abort" should be versus letting the agent propose a
  revised plan for approval.

This list will probably look different once I've actually built the next
piece.
