# ADR 0002 — Free-tier LLM behind an OpenAI-compatible port, rules first

**Status:** accepted

## Context
The project must run on a free LLM tier. Free tiers rate-limit (HTTP 429), time out and
change models. The agent must still work when the LLM is unavailable, and personal data
must not be sent to a third-party provider.

## Decision
- One generic client for any **OpenAI-compatible** `/chat/completions` endpoint
  (`src/llm/openai_compat.py`). The default is Groq (`llama-3.3-70b-versatile`), and
  OpenRouter `:free`, Gemini or a local Ollama work through `.env` alone. JSON mode,
  temperature 0, and its own circuit breaker.
- **Rules first** (`RuleBasedExtractor`, pt-BR regex and keywords). The LLM is called
  **only when the rules are inconclusive**: no field extracted and no clear intent. This
  saves quota and latency.
- The LLM receives **masked** text. CEP is extracted locally from the raw text. LLM
  output is type-checked and validated like user input. Safety intents (human request,
  out of scope) from the rules can never be overridden by the LLM.
- Any LLM failure silently degrades to the rule result.

## Consequences
- The whole system, including CI and the simulation, runs with **no API key**.
- On the challenge dataset the rule extractor reaches 100% on age, vehicle model/year and
  CEP (`make eval`). That dataset is template-generated, so the LLM path exists for
  real-world phrasing the rules miss.
- Prompts are versioned (`EXTRACTION_PROMPT_VERSION`) and the version is logged with
  each call.
