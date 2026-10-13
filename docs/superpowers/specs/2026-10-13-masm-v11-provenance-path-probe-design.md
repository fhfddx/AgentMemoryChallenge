# MASM v1.1 Provenance Path Probe Design

## Purpose and evidence

The best observed paid Smoke is 38.46, with direct and atomic retrieval at 100,
multi-session reasoning at 0, and abstention at 22.22. The current semantic
candidate recovers the synthetic connected chain but often selects a complete
chain about another subject. A same-question audit accepted that wrong subject
3/3 times. A question-blind origin check rejected it 5/5 times but also rejected
a valid, paraphrased two-source example 5/5 times. Neither check is safe to ship.

The first-person source claim ("I signed up") and the question's "speaker" are
the same referent, but reducing evidence to a bare origin label loses the
speaker/claim context. More generally, the current Search selector receives
candidate text, source group, position, and retrieval signals, not a verified
source-to-relation-to-value path. Stored `link` edges are generic and the
relation expander returns neighbor memories without edge predicates or quotes.

## Decision: diagnostic stage only

Build a standalone, synthetic, content-safe provenance-path probe. It must not
change Add, Search, schemas, database, runtime, prompt versions, deployment, or
evaluation thresholds. Given a small selected-evidence set, an extraction step
records per-source atomic claims with a source index, an exact quote from that
source, a subject mention, relation, and object mention. A validator rejects
invented quotes and out-of-range indices. A separate judgment step receives the
question and validated claims, including the original source wording, and decides
whether a complete path from the questioned subject to the requested value is
supported. The original evidence is not sent to that second step; the quote-backed
claims are. A first-person subject is represented as the source speaker, without
assuming that two distinct source speakers are one person.

The probe is a falsification tool, not a production gate yet. It must report only
case names, counts, booleans, and coarse failure categories. It must never print
source text, quotes, extracted names, provider responses, credentials, raw logs,
memory IDs, or database rows. It makes no database calls.

## Discriminating cases

Use the existing synthetic cases: wrong-subject complete chain, connected chain,
explicit alias chain, direct two attributes, paraphrased direct, and paraphrased
multi-source. Add disconnected bridge and missing value as negative controls.
Run a deterministic unit suite with fake provider outputs, then five consecutive
real-model probe rounds. Any false acceptance of a negative or rejection of a
positive rejects the hypothesis. A result from this small synthetic probe is
necessary but not sufficient for production; passing it does not justify a paid
Smoke or guarantee a score increase.

## Safety boundaries

- Exact quote validation compares a proposed quote with its own source only. It
  never requires the source to contain a verbatim identifier from the question.
- No graph connectivity pruning is applied to production evidence. No fixed
  two-hop assumption is made.
- The existing d74ad1c direct-recall behavior and the deployed API are untouched.
- No official Smoke or Full evaluation is run in this stage.
- No read, display, move, delete, stage, or commit of `秘钥.txt` or other secrets.

## Next decision

If the probe fails, keep the production code unchanged and use the failure class
to revisit representation or model capability. If it passes five rounds, design a
separate, explicitly reviewed production integration that preserves direct recall
and adds larger paraphrase/abstention regressions before any deployment.
