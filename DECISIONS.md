# DECISIONS — Chatbot

Architecture decisions, their rationale and rejected alternatives. A decision without a rejected alternative is a description; those belong in design/design_summary.md.

Reconstructed on 17 August 2026 from the codebase and CLAUDE.md. Dates are approximate unless stated otherwise.

---

## DEC-01 · One customer per deployment, no multi-tenancy

**Decision:** Clone the repository, configure it through the panel and deploy it on that customer's server. Nothing is shared between customers.

**Why:** The system handles health data. Infrastructure isolation cannot accidentally select another customer; a tenant filter can.

**Rejected:** Multi-tenancy with a discriminator column, because one filter failure could expose another clinic's records. A separate schema per customer in one database still leaves one credential that opens everything.

**Accepted cost:** Each deployment is updated separately.

---

## DEC-02 · Every external provider sits behind an interface

**Decision:** No endpoint calls an external API directly. All integrations go through backend/app/providers/ and interfaces such as LLMProvider and WhatsAppProvider.

**Why:** Providers fail and change pricing and contracts. The platform currently supports three LLM providers, two WhatsApp providers and two email providers. Replacing one provider should require changing one file.

**Rejected:** Calling an SDK from an endpoint. It is faster to write and impossible to replace cleanly.

---

## DEC-03 · Prompts live in the database

**Decision:** Prompts are edited from the panel and versioned in agent_prompt_history. Only the internal agent's security guardrails remain in code.

**Why:** The customer tunes the agent's tone. A prompt file would require a deployment for every wording change.

**Rejected:** Versioned prompt files. Finding the current prompt there would be slow and editing it would require deployment.

**Deliberate exception:** Internal-agent prompt-injection guardrails stay in code because they are security controls and must not be disabled from the panel.

---

## DEC-04 · Encrypt data at rest where it matters

**Decision:** nif, direccion, notas_internas, transcripts and credentials use Fernet encryption through core/encrypted_type.py. telefono, email and nombre remain in plaintext.

**Why:** Encrypted columns cannot support SQL filters or indexes. The contact list must search by phone, email and name.

**Rejected:** Encrypting everything, which breaks the application, and encrypting nothing, which exposes health data.

**Accepted risk:** Losing ENCRYPTION_KEY makes encrypted fields unreadable forever. The type returns None instead of crashing, so the loss is silent.

---

## DEC-05 · The classifier fails open

**Decision:** On any classifier error, the message continues to the agent.

**Why:** The two failure modes have different costs. Letting spam through is an annoyance; dropping a real patient asking about an appointment loses a customer without notice.

**Rejected:** Failing closed and dropping messages when uncertain. It keeps the inbox cleaner but hides mistakes.

---

## DEC-06 · Two classifier stages, rules before the LLM

**Decision:** Apply hard rules first (sender, domain and subject). Call the LLM only when no rule matches.

**Why:** Rules catch most spam at no model cost. Calling a model for every newsletter pays for the obvious.

---

## DEC-07 · /health returns 200 when the worker is down

**Decision:** The HTTP status does not depend on the worker; the detail appears in the worker field.

**Why:** If /health failed while the worker was down, the orchestrator would restart the API in a loop. The API still works without the worker; only background tasks are delayed.

**Rejected:** Returning 503 for every degraded state. That turns a partial degradation into a full outage.

---

## DEC-08 · Hand-written migrations, no autogenerate

**Decision:** Write upgrade() and downgrade() by hand, with a header explaining why.

**Why:** Against an up-to-date database, autogenerate proposed dropping four valid tables and fourteen valid indexes. It missed models not imported by init and indexes created with hand-written SQL.

**Rejected:** Fixing generated migrations every time. One distraction could drop a production table.

**Rule:** Never edit an applied migration. Add another migration. Every migration must include a real downgrade(); CI verifies it.

---

## DEC-09 · Human-facing text is English

**Decision:** Human-facing documentation, comments, panel text and test descriptions use English. Database columns, enum values, migration identifiers and compatibility strings retain their existing names.

**Why:** The repository is published for an English-speaking technical audience, while changing persisted identifiers would require migrations and could break existing deployments.

**Rejected:** Renaming database tables, columns and enum values as part of a documentation translation. That would add migration risk without product value.

---

## DEC-10 · Redis burst buffer, except for voice

**Decision:** Consecutive messages from one sender are buffered for a few seconds before processing. Voice messages bypass the buffer.

**Why:** People often split one question across several messages. Replying to every fragment costs more and feels worse. Voice is synchronous and does not need this behavior.

---

## DEC-11 · Two agent kinds, text and voice

**Decision:** kind is text or voice, and the channel and agent kind must match.

**Why:** A writing-oriented prompt does not fit a phone call, where there are no lists or links and turn-taking matters.

---

## DEC-12 · The internal agent has its own budget

**Decision:** The operator agent has separate daily limits and budget from customer-facing agents, plus fixed protection against hidden instructions in messages it reads.

**Why:** It reads third-party messages, so it is a prompt-injection surface. An operator asking questions must not consume the budget used to serve patients.

---

## DEC-13 · Hybrid knowledge-base search

**Decision:** Combine pgvector similarity with full-text search using match_chunks_hybrid.

**Why:** Vector search can miss proper names, references and codes. Full-text search can miss synonyms. Combining both covers the two failure modes.

---

## Open decisions

| # | Question | Reference |
|---|---|---|
| 1 | Should the chatbot replace or complement the n8n workflows? | Decision 3 in the umbrella CLAUDE.md; building both creates duplicate work |
| 2 | Should deployment use EasyPanel or adapt the existing Caddy Compose files? | Decision 2 in the umbrella CLAUDE.md |
