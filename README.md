# AccelAgent

A learning project: I'm figuring out how agentic AI systems get built for
safety-critical control settings by actually building one, against a
simulated accelerator control environment.

## What this is

I wanted to understand what it actually takes to put an AI agent in front of
a control system where a bad write matters — not just read a paper about it.
So I'm building an agent that operates against a simulated accelerator
control backend (16 EPICS-style channels, see
[docs/simulator.md](docs/simulator.md)): reading channel state, analyzing
history, proposing corrective actions, with any actual write gated behind
human approval. This is very much in progress and I don't have the whole
design nailed down.

Loosely inspired by Hellert et al., "Agentic AI at the Advanced Light
Source," NeurIPS 2025 Workshop on Machine Learning and the Physical
Sciences — [PDF](https://ml4physicalsciences.github.io/2025/files/NeurIPS_ML4PS_2025_93.pdf).

See [docs/architecture.md](docs/architecture.md) for the current (evolving)
design thinking and roadmap.

---

*Built with the assistance of Claude Sonnet 5 (Anthropic), via Claude Code.*
