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

## DEC-14 · Rules are the floor, the LLM may only add

**Decision:** The clinical guardrail is a deterministic word list that runs before the model. An optional second layer (`GUARDARRAIL_CLINICO_LLM`, off by default) asks a cheap LLM whether a message that the rules let through still sounds clinical. That layer can only turn a "pass" into a "hand off": it never overrides a rule, it is consulted only when the rules did not fire, and if it fails (an exception, or an empty answer such as a reasoning model running out of tokens) the message proceeds as if it did not exist. Any other answer than a clean "NO" hands off ("NO SÉ" included). The patient's message is passed whole, inside delimiters, as data.

**Why:** A word list is auditable, free and works when the provider is down, but it misses paraphrase ("my mouth feels numb"). A classifier catches paraphrase but cannot be audited and drifts between model versions. Letting the LLM only add keeps the guarantee that matters (a rule hit is never lost) and buys recall where the list is blind. Cost asymmetry decides every tie: a false positive costs a human one minute; a false negative is the system opining on a patient's symptom.

**Rejected:** An LLM-only classifier (no floor when the model is down). An LLM that can veto a rule hit (a prompt-injected message could talk its way out of a hand-off). Always-on second layer (one extra model call per message before anyone has measured the recall it buys).

---

## DEC-15 · The answer is checked after the model, not only instructed before it

**Decision:** Before a reply leaves `run_agent`, every euro amount in it must be backed by evidence the model was legitimately given. Otherwise the reply is replaced by a fixed sentence and a `salida_importe_no_respaldado` decision is logged. `SECURITY_GUARD` rule 7 separately forbids answering with the price of a similar product.

**What counts as evidence:** the business's own prompt (without the date line), what the agent already said in earlier turns, the operator's instruction when correcting a draft, and the tool results of this turn. In the prompt, history and non-knowledge-base tools only a number written as an amount counts; in knowledge-base chunks a bare number also counts (spreadsheet rows read "Limpieza,60"). **Not evidence:** what the customer wrote ("they told me it is 300 €" must not confirm 300 €) and the text the model writes next to a tool call ("I think it is 350 €").

**Why:** Eval F2 showed the agent answering a question about a zirconia implant with the price of the porcelain one. The prompt already said "if it is not in the knowledge base, it does not exist"; a prompt is a request, this is a check. Fail-closed on money: a false positive costs a person confirming a price, a false negative is an invented price the customer trusts.

**What it does NOT catch, stated on purpose:** F2 itself. If the porcelain price is in the tool result and the model attaches it to the zirconia implant, the amount is "backed" and the reply goes out (`test_limite_conocido_el_precio_del_producto_vecino_pasa_si_esta_en_la_kb` pins this). Catching it needs understanding which product a sentence is about, not comparing numbers; the only barrier there is rule 7 plus the F2 eval run against a real LLM, which has not been run. Also uncovered: times and dates (computed from the calendar, they would produce false positives), currencies other than euros, "mil euros", and English-style "1,200 €". A derived amount (a sum, a discount) is treated as unsupported. A bare number in a long knowledge-base document can back an invented amount by coincidence. The similarity threshold (0.3) was not changed: tuning it without a real knowledge base and recall evals would be guessing.

**Rejected:** Verifying every number (false positives on phone numbers, addresses, appointment times). An LLM judge for groundedness (same auditability problem as DEC-14, plus latency on every reply). Raising the threshold now (unmeasurable here).

---

## DEC-16 · Escalation is a notice, not a reassignment

**Decision:** A hand-off is a promise ("they will answer you as soon as they can"). After `handoff.sla_minutes` without a single operator message, one internal web push is sent per hand-off. Off by default. The bridge message can also say the business is closed (`handoff.out_of_hours_notice`, off by default). Both are switched on by an admin with `PUT /api/v1/admin/settings/handoff` (`{"sla_minutes": 15, "out_of_hours_notice": true}`); there is no panel screen for it yet. The out-of-hours phrase relies on the calendar settings, and the existing parser drops overnight ranges such as `22:00-02:00`, so a night-time business would always read as closed: leave it off there.

**Why:** Nothing used to check that the promise was kept. The smallest useful step is to tell the team. Who to reassign to, and what to tell the customer, are product decisions that depend on how each clinic staffs the inbox, so they are not made by a cron job.

**Rejected:** Auto-reassignment (needs teams and skills, which the data model does not have). Writing to the customer on timeout (a second automated message to someone already waiting for a human). Default-on (a clinic with nobody on call would get alerts nobody can act on).

---

## DEC-17 · Router and domain agents: not built, designed

**Decision:** Not implemented. One agent per channel with a per-agent tool allowlist stays. If the product moves towards a platform (open decision 1), the target is: gates first (user recognition, hours and queue, clinical/urgency rules), then a light router (rules, then a cheap LLM) to domain agents (information, appointments, after-sales), each with its own tools and its own slice of the knowledge base; hand-off always carries a summary.

**Why:** One deployment per customer and seven tools do not need a router; it would add a failure surface and a cost per message. The design is recorded so that the decision is deliberate, not an omission.

**Rejected:** Building the router now (no workload that needs it, and it would be built twice if the n8n question is answered the other way). A router that is itself a single big LLM prompt (one more unauditable decision in front of every message).

---

## Open decisions

| # | Question | Reference |
|---|---|---|
| 1 | Should the chatbot replace or complement the n8n workflows? | Decision 3 in the umbrella CLAUDE.md; building both creates duplicate work |
| 2 | Should deployment use EasyPanel or adapt the existing Caddy Compose files? | Decision 2 in the umbrella CLAUDE.md |
