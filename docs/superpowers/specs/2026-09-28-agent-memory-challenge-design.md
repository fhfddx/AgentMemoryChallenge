# MASM: Agent Memory Challenge System Design

**Date:** 2026-09-28  
**Status:** Approved design, pending implementation planning  
**Project root:** `E:\Competitions\AgentMemoryChallenge`  
**Competition target:** Agent Memory Challenge Cycle 2, Multimodal Track, Open-source Methods Division

## 1. Purpose

MASM (Multi-Agent Structured Memory) is a competition-oriented multimodal long-term memory system. Its first priority is to deliver a stable, compliant Add/Search service before the competition deadline. Its second priority is to preserve clear research contributions, reproducible experiments, and ablation evidence for a later paper and prototype release.

The implementation must favor reliability, bounded cost, and a recoverable baseline over architectural complexity. Every advanced component must be removable without breaking the official API.

## 2. Success Criteria

The project is successful when all of the following are true:

1. The public Health, Add, and Search endpoints conform to the official competition contract.
2. An accepted Add is durable and immediately searchable.
3. No retrieval path can return data belonging to another user.
4. A baseline system can be submitted even if advanced agent modules are disabled.
5. The full MASM system can be compared with the baseline on at least one public benchmark.
6. The repository, deployed endpoint, configuration, and experiment records are reproducible.
7. Search returns memory evidence only and never generates or disguises a final answer.

## 3. Scope

### 3.1 Included

- Text memories.
- Image memories.
- Ordered mixed text-and-image messages.
- Multi-user and multi-session isolation.
- Structured memory extraction.
- Text, image, lexical, temporal, metadata, and relation retrieval.
- Duplicate, update, and conflict handling.
- Official Health, Add, and Search endpoints.
- Local evaluation, deployment, observability, and experiment tracking.

### 3.2 Excluded from Cycle 2

- Audio and video processing.
- Unbounded autonomous agent loops.
- A dedicated graph database.
- Large-scale model training or fine-tuning.
- An administrative web interface.
- General chat functionality.
- Final-answer generation inside Search.

The data model may contain modality extension fields, but no implementation work is allocated to audio or video in this cycle.

## 4. Architecture

MASM is a modular monolith. It is deployed as one API application backed by PostgreSQL with pgvector and S3-compatible object storage. External model services are accessed through provider adapters.

```text
AML Evaluation Platform
          |
    API / Authentication
          |
 +--------+---------+
 |                  |
Add Pipeline     Search Pipeline
 |                  |
Content Parser    Query Analyzer
 |                  |
Perception Agent  Hybrid Retrieval
 |                  |
Temporal Agent    Relation Expansion
 |                  |
Curator Agent     Evidence Reranker
 |                  |
Persistence       Response Packer
 +--------+---------+
          |
 PostgreSQL + pgvector
          |
  S3 Object Storage
```

The agents are bounded roles invoked by a deterministic orchestrator. They are not independent services and cannot call one another in an unbounded loop.

## 5. Add Pipeline

The Add path executes the following ordered stages:

1. Authenticate the request.
2. Validate the official request schema and payload limits.
3. Check `request_id` for idempotency.
4. Decode content parts without changing their original order.
5. Store immutable source evidence and image assets.
6. Run the Perception Agent.
7. Run the Temporal-Relation Agent.
8. Retrieve a bounded set of related memories for the same user.
9. Run the Memory Curator Agent.
10. Validate proposed actions with deterministic code.
11. Persist structured data, embeddings, and relation edges in one transactional unit.
12. Verify that the new memory is searchable.
13. Record the request in the idempotency ledger and return success.

An Add response may report success only after durable persistence and indexing. A repeated `request_id` must return the prior logical result without creating duplicate memories.

### 5.1 Graceful Degradation

If an advanced agent fails or returns invalid structured output, the orchestrator retries or repairs the output once. If that also fails, the system stores the original evidence, a basic summary when available, and retrievable embeddings. Agent failure must not corrupt existing memories.

## 6. Agent Responsibilities

### 6.1 Perception Agent

The Perception Agent receives ordered text and image content and returns schema-validated JSON containing:

- Faithful image descriptions.
- OCR text.
- People, objects, locations, and scenes.
- Directly observable actions and events.
- Retrieval keywords.
- Language and modality metadata.
- Confidence and provenance for derived fields.

It must not infer facts that are not observable in the source.

### 6.2 Temporal-Relation Agent

The Temporal-Relation Agent receives perception output and a bounded set of same-user historical candidates. It returns:

- Absolute and relative time expressions.
- Normalized event time and time precision.
- Entity, event, and location relations.
- Event ordering.
- Candidate supplement, update, duplicate, and conflict links.
- A confidence value and supporting source references for each relation.

It proposes relations but cannot mutate storage.

### 6.3 Memory Curator Agent

The Memory Curator Agent may propose only these actions:

- `CREATE`: create a new memory.
- `LINK`: connect the new evidence to existing memories.
- `MERGE`: mark substantially duplicate content and combine retrieval metadata.
- `SUPERSEDE`: mark a prior derived claim as replaced by explicit newer evidence.
- `CONFLICT`: retain both claims and place them in the same conflict group.

All actions are validated by code. The agent never writes directly to the database. Immutable source evidence is never overwritten by a curator action.

## 7. Memory Model

The logical memory record contains:

```text
MemoryRecord
+-- identity
|   +-- memory_id
|   +-- user_id
|   +-- session_id
|   +-- request_id
+-- source
|   +-- ordered_content_parts
|   +-- original_text
|   +-- image_object_uri
+-- semantics
|   +-- summary
|   +-- entities
|   +-- events
|   +-- keywords
|   +-- modality
+-- temporal
|   +-- event_time
|   +-- time_precision
|   +-- observed_at
+-- governance
|   +-- confidence
|   +-- status
|   +-- duplicate_of
|   +-- supersedes
|   +-- conflict_group_id
+-- retrieval
    +-- text_embedding
    +-- image_embedding
```

### 7.1 Required Tables

- `users`: anonymous competition user identities.
- `sessions`: sessions and timestamps.
- `source_messages`: immutable source messages and ordered parts.
- `assets`: object URI, media type, hash, decoded size, and dimensions.
- `memories`: structured memory units.
- `memory_entities`: people, locations, objects, and canonical labels.
- `memory_events`: events and normalized temporal attributes.
- `memory_relations`: directed typed edges among memories, entities, and events.
- `memory_embeddings`: text and image embeddings with model versions.
- `memory_conflicts`: conflict groups and version relationships.
- `processing_runs`: model, prompt, code, latency, and outcome metadata.
- `request_ledger`: idempotency state for Add requests.

Every queryable record must contain or resolve to `user_id`. Storage APIs must require a user scope rather than accepting it as an optional filter.

## 8. Search Pipeline

Search runs four bounded stages: query analysis, hybrid recall, relation expansion, and evidence ranking.

### 8.1 Query Analysis

The analyzer emits a schema containing:

```json
{
  "text_queries": [],
  "visual_queries": [],
  "entities": [],
  "time_constraints": [],
  "location_constraints": [],
  "relation_hints": [],
  "intent": "fact | temporal | relational | visual"
}
```

At most three subqueries may be produced. Rule-based parsing handles simple cases. A language model is used only for complex temporal or relational queries.

### 8.2 Hybrid Recall

The system performs same-user retrieval through:

- PostgreSQL full-text search for names, OCR, and exact terms.
- Text-vector search for semantic similarity.
- Image-vector search for visual similarity and image queries.
- Entity, location, and time filters.

Results are combined with weighted Reciprocal Rank Fusion. Channel weights are configuration values selected on public development data, not embedded in application code.

### 8.3 Relation Expansion

Only high-ranked seed memories receive relation expansion, and expansion is limited to one hop. Expansion may add memories from the same event, adjacent events in time, linked entities, or the opposing member of a conflict group. The system does not perform recursive agentic graph exploration in Cycle 2.

### 8.4 Evidence Ranking

Candidates are scored by:

- Query relevance.
- Entity, time, location, and relation match.
- Evidence completeness and provenance.
- Duplication penalty.
- Conflict-group coverage.
- Result diversity.

Deterministic rules first reduce the set. A lightweight reranker may score the remaining leading candidates. The reranker judges relevance only and cannot write an answer.

### 8.5 Response Packing

The packer returns no more than the requested `top_k`, including requests with `top_k=100`. It enforces per-image and total response limits, preserves multimodal part order, avoids repeated evidence, and keeps the highest-ranked complete items when truncation is necessary. Visual queries prioritize relevant image evidence; text queries do not include unrelated images merely because they are available.

## 9. Public API

The only required public endpoints are:

- `GET /health`
- `POST /add`
- `POST /search`

The public adapter must match the official request and response schemas. Internal domain models may be richer but must not leak extra answer-like content into the official Search response.

Supported authentication methods are configurable Bearer, Token, or `X-Api-Key`. The service enforces schema validation, image decoding and format validation, payload limits, idempotency, request timeouts, rate limits, and bounded concurrency.

### 9.1 Error Semantics

| HTTP status | Meaning |
| --- | --- |
| 400 | Malformed JSON or invalid content structure |
| 401/403 | Missing or invalid authentication |
| 413 | Image or request exceeds configured limits |
| 422 | Valid schema but content cannot be processed |
| 429 | Rate or concurrency limit reached |
| 500 | Unexpected internal failure |
| 503 | Required model or storage dependency unavailable |

Retriable errors include a machine-readable error code and request identifier but never raw competition content.

## 10. Model and Provider Strategy

All models are accessed through interfaces that record model name, version, prompt version, token usage, latency, and outcome. The initial model policy is:

- Perception and relation extraction: an adapter compatible with the model required or allowed by the official competition rules, initially `gpt-4o-mini` where applicable.
- Text embedding: a replaceable multilingual embedding provider.
- Image embedding: a lightweight CLIP- or SigLIP-family encoder.
- Reranking: a lightweight reranker or tightly constrained relevance-scoring model.

DeepSeek is used primarily as an implementation harness and may be used for offline analysis. The deployed system must not depend on undocumented harness behavior. No provider-specific object may cross the provider adapter boundary.

## 11. Deployment

The reference deployment uses one low-cost CPU cloud host and external model APIs:

```text
Caddy or Nginx
      |
   HTTPS
      |
FastAPI application
      |
PostgreSQL + pgvector
      |
Persistent volume

External dependencies:
- S3-compatible private object storage
- Model APIs
```

The implementation stack is Python 3.11, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL, pgvector, an S3-compatible client, HTTPX, Docker Compose, Pytest, and Caddy or Nginx.

Secrets are injected through environment variables. Only `.env.example` is committed. The deployment must survive application restarts without losing accepted memories.

## 12. Security, Privacy, and Compliance

The system must:

- Enforce user, session, task, and evaluation-run isolation.
- Keep API secrets out of source control and logs.
- Avoid logging raw text, images, Base64 data, or model prompts containing evaluation content.
- Keep the database and object storage private.
- Use HTTPS for public traffic.
- Never use evaluation data for training.
- Provide deletion by evaluation run and retention deadline.
- Avoid hard-coded questions, answers, benchmark-specific rules, or leaked data.
- Disclose reused papers, repositories, licenses, and modifications.

Evaluation content must be deleted within the official retention period. Operational metadata may remain only if it cannot reconstruct evaluation content.

## 13. Observability

Structured logs and metrics include:

- Request identifier and anonymized user identifier.
- Add and Search latency.
- Agent execution status and degradation reason.
- Model request count, token use, latency, and estimated cost.
- Candidate counts from each retrieval channel.
- Returned evidence count.
- HTTP status and dependency health.

Required aggregate metrics are Add/Search success rates, P50/P95 latency, model failure rate, mean request cost, degradation rate, and storage health. Raw competition content is excluded.

## 14. Testing Strategy

### 14.1 Unit Tests

- Official and internal schemas.
- Base64 and media validation.
- User-scope enforcement.
- Add idempotency.
- Temporal normalization.
- Duplicate and conflict rules.
- Rank fusion.
- Response-size and context-budget enforcement.

### 14.2 Integration Tests

- Add followed by immediate Search.
- Text-to-text retrieval.
- Image-to-text and text-to-image retrieval.
- Mixed-content part ordering.
- Multiple sessions for one user.
- Similar memories belonging to different users.
- New and old information conflicts.
- Model timeout and graceful degradation.
- Database rollback and service restart durability.

### 14.3 Contract Tests

- Official field names and response shapes.
- Authentication variants.
- `top_k`, including 100.
- Supported image types and size limits.
- Health behavior.
- 429 and retry semantics.

### 14.4 Isolation Tests

Isolation tests are release blockers. They create nearly identical memories for multiple users and verify that every lexical, vector, metadata, relation, conflict, and fallback path returns only the requested user's evidence.

## 15. Experimental Design

Three systems are compared:

- **B0:** image description plus one vector retrieval channel.
- **B1:** structured memory plus hybrid retrieval.
- **MASM:** three-agent structured write pipeline, hybrid retrieval, relation expansion, and conflict-aware reranking.

Ablations remove one of the following at a time:

- Temporal-Relation Agent.
- Memory Curator Agent.
- Relation expansion.
- Image-vector retrieval.
- Conflict handling.
- Multi-channel retrieval.

Metrics include Recall@10, Recall@100, MRR, nDCG, downstream answer accuracy, Add/Search latency, API cost, model calls, and degradation rate. Experiments begin with controlled subsets of ATM-Bench and Mem-Gallery. Dataset size is increased only after the full pipeline and metrics are stable.

## 16. Repository Layout

```text
E:\Competitions\AgentMemoryChallenge
+-- README.md
+-- pyproject.toml
+-- .env.example
+-- config
|   +-- base.yaml
|   +-- models.yaml
|   +-- retrieval.yaml
+-- docs
|   +-- competition
|   +-- architecture
|   +-- api
|   +-- experiments
|   +-- handoff
|   +-- superpowers/specs
+-- src/masm
|   +-- api
|   +-- agents
|   +-- orchestration
|   +-- memory
|   +-- retrieval
|   +-- providers
|   +-- storage
|   +-- schemas
|   +-- observability
+-- tests
|   +-- unit
|   +-- integration
|   +-- contract
|   +-- isolation
+-- scripts
+-- deployments
+-- experiments
+-- artifacts
```

Runtime artifacts, databases, caches, images, secrets, and evaluation data are excluded from version control.

## 17. Responsibilities and Handoff Contract

### 17.1 Architecture Owner

Codex owns architecture, public and internal contracts, task decomposition, acceptance criteria, implementation review, experiment analysis, and scope control.

### 17.2 Implementation Harness

DeepSeek Harness implements bounded tasks, adds corresponding tests, runs required verification commands, and reports unresolved failures. It must not change frozen public interfaces, storage invariants, or module boundaries without an approved design amendment.

Every harness task must state:

- Objective.
- Allowed files.
- Forbidden boundaries.
- Input and output contracts.
- Implementation constraints.
- Test commands.
- Acceptance criteria.
- Failure report format.

## 18. Delivery Sequence

1. Repository skeleton and official API schemas.
2. PostgreSQL, object storage, migrations, and user isolation.
3. Baseline Add and Search.
4. Docker deployment and official Smoke readiness.
5. Three-agent Add pipeline.
6. Hybrid retrieval and one-hop relation expansion.
7. Conflict handling and evidence reranking.
8. Public benchmark experiments and ablations.
9. Reliability, cost, privacy, and security review.
10. Version freeze and Full evaluation.

Each numbered stage must leave a runnable version. Advanced work cannot remove the ability to deploy the last accepted baseline.

## 19. Release Gates

A competition release is blocked unless:

- Health, Add, and Search contract tests pass.
- Add is durable and immediately searchable.
- All isolation tests pass.
- The endpoint is reachable over public HTTPS.
- Restart durability is verified.
- Model failures produce a controlled error or documented degradation.
- Logs contain no raw evaluation content.
- The README enables third-party reproduction.
- The deployed version matches the frozen repository revision.
- At least one public benchmark compares a baseline with MASM.
- A rollback-ready baseline image and configuration are retained.

## 20. Design Rationale

The chosen design sits between a low-risk multimodal RAG baseline and a high-risk autonomous graph-memory system. Structured role separation provides a publishable multi-agent contribution, while deterministic orchestration and a modular baseline keep the competition submission feasible. PostgreSQL with pgvector is sufficient for the expected scale and avoids operating an additional graph database. Provider adapters preserve model flexibility, and strict evidence-only Search behavior keeps the system aligned with the competition's evaluation boundary.
