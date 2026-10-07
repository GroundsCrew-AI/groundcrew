# Feedback on Nebius Token Factory and NVIDIA Nemotron

What we found while building Groundcrew, for the hackathon feedback form and for Nebius and NVIDIA. Each item
says what we saw, how we measured it, and what we did about it. Newest first within each section.

Measured with `scripts/probe_token_factory.py` and `scripts/probe_followup.py` (2026-10-07), base URL
`https://api.tokenfactory.nebius.com/v1/`. Small samples: treat single runs as signals, not benchmarks.

## Models and context

- **Nano accepts long prompts but stops answering correctly well before its limit.** Nemotron 3 Nano took a
  65,743-token prompt without error, but a simple "what is the last line number" question was answered right at
  about 6k tokens and wrong at about 20k (502 instead of 952) and 60k (20779 instead of 2857). Lightning answered
  all three right. *What we did:* `LLM_CONTEXT_NANO=8000` is Nano's usable context; bigger prompts go to the
  route's fallback tier, and evidence chunks for Nano are about 5k tokens. *Ask:* publish an effective-context
  guideline per model, not only the maximum.
- **Cold start.** The first Nano call of a session took 20.6 s; warm calls took 280 to 360 ms. *What we did:* a
  120 s timeout. *Ask:* document cold starts, or offer a warm-up endpoint.

## Reasoning controls

- **`low_effort` is not always cheaper.** It cut completion tokens on Nano (152 vs 234 with full reasoning) and
  Super (48 vs 144), but on Nemotron 3.5 Lightning it used more than full reasoning (603 vs 460, one sample). On
  Ultra, 5 samples each averaged 311 (low) vs 362 (on) with a wide spread (174 to 700 for on); a single earlier
  sample had low higher (327 vs 218). *What we did:* Ultra and Lightning never get `low`; Nano and Super do.
- **The reasoning trace field differs by model.** Nano returns it in `reasoning`; Super and Ultra in
  `reasoning_content`. No `<think>` tags were seen in content. *What we did:* read both fields. *Ask:* one field
  name across models.
- `enable_thinking: false` gave zero thinking tokens on every tier, and tool calls parsed correctly on every tier
  with reasoning on and off.

## Structured output

- **Strict `json_schema` forces a value inside the enum even when it is wrong.** With
  `enum: [python, javascript, go, other]` and a prompt about a Rust repository, Nano answered `python` and Super
  and Ultra answered `javascript`; only Lightning chose `other`. Enforcement works, but it turns "none of these"
  into a confident wrong answer. *What we did:* every enum includes `other` (the client refuses schemas without
  it), prompts say to use `other` or null when nothing fits, and Scout prefers rule-based detection over the
  model and marks unsupported model values as low confidence.
- **Free-text values vary in casing.** The same question gave `TypeScript`, `JavaScript`, and `javascript` across
  tiers. *What we did:* normalize model values onto lowercase allowed values before validation.

## Catalog

- **No NVIDIA embedding or rerank model on Token Factory.** `/v1/models` listed only `Qwen/Qwen3-Embedding-8B`
  for embeddings (4096 dimensions; it honours `dimensions=1024`) and no rerank model. *What we did:* Qwen3
  embeddings at 1024 dimensions, and evidence ranked by embedding cosine similarity instead of a reranker. *Ask:*
  an NVIDIA embedder and reranker (e.g. the NeMo Retriever models) on Token Factory.
- **Nemotron 3.5 Lightning is listed but we found no public price.** It ran a 3-turn tool loop exactly right at
  Nano-like latency (about 270 ms). *What we did:* Lightning routes for tool steps switch on once its price is
  set; until then it is never called, because we do not make calls whose cost we cannot count.
