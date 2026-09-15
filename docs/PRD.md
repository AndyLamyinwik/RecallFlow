# RecallFlow — Product Requirements Document

**Phase:** Phase 2 — Product Definition  
**Status:** Provisional and pre-validation  
**Product principle:** Help the user make a useful decision about previously saved knowledge content.

This document translates the Phase 1 evidence into a deliberately narrow product definition. It does not claim that proactive resurfacing, a seven-day interval, AI-generated context, or active recall has been validated. V1 is designed to test those assumptions while remaining useful when AI is unavailable.

## 1. Product Summary

RecallFlow is a lightweight resurfacing product for people whose knowledge-oriented saved content grows faster than they revisit or act on it. A user saves one public article they cannot process now. On a transparent future date, RecallFlow presents one low-effort card that helps them decide whether to open the article, return to it later, or dismiss it as no longer relevant.

V1 focuses on the after-save loop:

Save → Wait → Resurface → Decide → Record → Adjust

RecallFlow does not optimize for maximum engagement or backlog completion. Its intended value is a useful, low-pressure decision. The primary product question is whether resurfacing creates more useful action than leaving the same item in a passive archive.

## 2. Problem

High-frequency savers such as students, knowledge workers, and self-directed learners regularly save useful articles, videos, PDFs, and other digital resources for future use. Existing tools make capture inexpensive, but saved content can accumulate faster than users revisit or act on it. As time passes, potentially valuable items may be forgotten, lose their original context, or become difficult to prioritize within a growing backlog.

The opportunity is not primarily to build another bookmarking system. It is to investigate whether lightweight resurfacing can help users reconnect with previously saved information at moments when it is useful again.

V1 addresses recall or resurfacing failure rather than general retrieval failure. It does not assume that every saved item deserves attention, that a larger collection necessarily causes greater pain, or that users want an external reminder.

## 3. Target User

Target users are defined behaviorally:

- they save knowledge-oriented content frequently;
- they revisit or act on it less frequently than they save it;
- the imbalance persists and creates a recurring backlog; and
- they already have a functioning capture method.

Students, knowledge workers, self-directed learners, and read-later or PKM users may fit this definition, but occupation is not a qualifying criterion.

**Target User v1:** A person with a recurring high-save, low-revisit pattern who saves public knowledge articles for work or learning and wants a low-effort way to decide what deserves attention later.

This is a recruitment hypothesis, not a validated market segment.

## 4. Job to Be Done

The following jobs are team-authored product hypotheses, not participant quotations.

**Primary job:** When I save knowledge content for later, help me reconnect with it at a transparent future point and make a useful decision about it.

**Functional job:** Restore enough context in 5–15 seconds for the user to choose Open, Later, or Dismiss without browsing an entire archive.

**Emotional job — hypothesis:** Help the user feel in control of saved knowledge rather than pressured, guilty, or overwhelmed by an accumulating backlog. Current evidence is insufficient to treat this emotional outcome as established.

**Desired outcome:** The user recognizes the item, understands why it is appearing and the effort it may require, makes an intentional decision, and can predict what the product will do next.

## 5. Primary Use Case

### Definition

A behaviorally qualified high-frequency saver encounters a public, knowledge-oriented article that they judge worth reading for work or learning but cannot process now. They save its URL with a Read-later intent and accept a fixed seven-day revisit date or choose another date. On or after that date, RecallFlow shows one in-product resurfacing card during the user's next visit. Within approximately 5–15 seconds, the user understands what the article is, why it is being shown, the estimated effort, and intentionally chooses Open, Later, or Dismiss.

This use case is accepted as the provisional V1 focus because it isolates the Wait → Resurface → Decision loop. It deliberately excludes PDF parsing, bulk imports, external notifications, general link management, and mandatory recall.

### Acceptance criteria

| Criterion | V1 definition |
|---|---|
| User | A high-save, low-revisit, recurring-backlog user |
| Trigger | The item's due date has arrived and the user next opens RecallFlow |
| Job | Decide whether to engage now, reschedule, or retire the item |
| Start state | One unprocessed public knowledge article with a saved date, Read intent, and revisit date |
| End state | Opened, rescheduled, or dismissed, with the resulting state recorded |
| Understandable value | Context and effort are restored without opening the full archive |
| Measurable outcome | A qualifying resolved outcome on a viewed resurfacing event |
| Non-AI baseline | Fixed seven-day interval, user override, earliest-due then oldest-first ordering |
| Explicit failure modes | Irrelevance, bad timing, fatigue, wrong context, broken source, or no action |

The seven-day interval is a transparent starting rule, not a research-backed claim about ideal timing.

## 6. Product Hypothesis

**Overall hypothesis:** If behaviorally qualified users are shown selected previously saved knowledge content through a low-effort resurfacing experience, and the experience is relevant without being excessively disruptive, they may be more likely to reconnect with or intentionally retire that content.

Planned thresholds and minimum acceptable effects must be defined before testing. No result is currently claimed.

| # | Testable hypothesis | Expected behavior | Planned metric | Baseline or comparison | What would falsify it |
|---:|---|---|---|---|---|
| H1 | Lightweight resurfacing produces more intentional decisions than leaving eligible items in a passive archive | Users reach Opened, a dated Later, an eligible completed Recall, or Dismissed after resurfacing | Item-level resolved-outcome rate within a pre-registered observation window; live Meaningful Decision Rate as an operational measure | Randomly assign comparable eligible items within enrolled users to resurfaced or holdout conditions; holdout items remain available in the passive archive without a system-created trigger | The resurfaced group does not exceed the pre-registered minimum improvement, or apparent lift is accompanied by worse intrusiveness or opt-out signals |
| H2 | A minimal context card enables a faster, better-informed decision than title and source alone | Users identify the item and choose a next step within about 5–15 seconds with less confusion | Qualified-decision rate: the share of card exposures in a moderated test where the participant both answers one pre-registered source-subject check correctly and selects a qualifying action within 15 seconds | Title-and-source-only card, using the same items and comprehension checks | Added context does not improve qualified decisions, increases decision time, or causes more incorrect comprehension responses |
| H3 | Lightweight timing control improves timing fit without making capture too burdensome | Users offered a date override show fewer immediate or repeated Later actions without abandoning capture | Immediate-Later rate, repeated-Later rate, high-intrusiveness rate, and capture abandonment as diagnostic measures | Randomly assign enrolled participants to fixed seven-day timing with or without an optional date override | The override variant increases capture abandonment or does not improve timing-related signals compared with the fixed-only control |
| H4 | Optional Recall may add value only for eligible learning content that the user has already engaged with | Eligible users intentionally start and complete Recall and find it preferable to a non-question alternative | Recall start and completion, plus separately sampled usefulness feedback | The same eligible content with a concise takeaway and direct Open but no question | Recall is rarely selected or completed, is not more useful than the comparison, or increases confusion, dismissal, or intrusiveness |

Product value and AI value must be evaluated separately. H1 can succeed even if AI adds no improvement; conversely, a good summary does not prove that resurfacing itself is wanted.

## 7. User Journey

| Step | User goal | System behavior | AI role | User control | Main failure mode | Data to log |
|---|---|---|---|---|---|---|
| Capture | Hand off an article without processing it now | Accept one public article URL and preserve source and save time | None required | Cancel or delete; optionally add one line explaining why it was saved | Capture is harder than the user's current method or the source cannot be read | Submission, success or failure, input type, abandonment, and capture duration |
| Understand | Confirm what the item is and what future action is intended | Obtain title and source, estimate effort by deterministic bands, default intent to Read, and set a seven-day due date | Optionally generate one source-grounded value line; AI failure does not block saving | Edit title or value line, add own note, choose Read or Learn, and override date | Extraction or synopsis is wrong; the system invents personal relevance | Field provenance, generation success or fallback, edits or rejection, intent choice, and date override |
| Wait | Stop thinking about the item without creating pressure | Keep the item silent until due; avoid overdue counts or clear-the-backlog language | None | Change the date, delete the item, pause resurfacing | Due items accumulate or the user forgets the product exists | Due date, timing mode, schedule changes, deletion, pause, and eligible count for internal diagnosis |
| Resurface | Recover enough context to decide quickly | On the next in-product visit after the due date, show one card selected by transparent rules | Reuse the saved value line; no AI selection or timing | Open, Later, Dismiss, leave without action, or use Recall only when eligible | Bad content, bad timing, opaque reason, or inbox pressure | Verified card impression, scheduled and shown time, selection rule, displayed fields, action latency, and no-action |
| Decision or action | Make one intentional choice | Execute the selected action; resolve the event only when it reaches a defined state outcome | Generate one grounded question only on an eligible Recall branch | Open, Later, Dismiss, or optional Recall | Too many actions, ambiguous labels, a source-open failure, or Open is misread as completed reading | First action, resolved outcome, time to action, open attempt and success, new date, dismissal and undo, Recall steps |
| Feedback | Communicate the outcome without filling a separate form | Treat the explicit action as the primary feedback signal | None | Later controls timing; Dismiss stops resurfacing; no-action remains neutral | Extra feedback creates friction or action meaning is ambiguous | Action and resulting state; no-action must not be inferred as dislike |
| Adjust | Know exactly what happens next | A confirmed source open or completed Recall resolves the event; Later creates a new due date; Dismiss removes future eligibility | No automated AI learning in V1 | After opening a Learn item, optionally mark it Ready for Recall and choose a recall date; change dates, undo Dismiss, restore items, pause or resume | Opaque personalization or repeated resurfacing becomes unpredictable | Resulting state, next due date, eligibility change, Ready-for-Recall choice, repeat-Later count, pause or resume |

Raw article text should not be treated as behavioral analytics. Only data necessary to evaluate the product hypotheses should be logged.

## 8. MVP User Flow

Add public article URL  
↓  
Extract title and source; estimate effort by rule  
↓  
Optionally generate a source-grounded one-line value statement; fall back to an excerpt  
↓  
User confirms or edits intent and revisit date  
↓  
Save  
↓  
Wait silently until due  
↓  
On the next app visit, show one transparent resurfacing card  
↓  
Open / Later / Dismiss  
↓  
Record the action attempt and the resulting resolved state separately

Recall is a conditional Should Have branch, not part of the default path. The complete happy path and alternative paths are specified in docs/user-flow.md.

## 9. MVP Scope

### Must Have

- One public, readable knowledge-article URL per capture; no bulk import.
- Source URL, title, source domain, save time, item status, intent, and revisit date.
- A minimal saved-items list that lets the user reopen an item, edit its date, or delete it; no search, folders, or collection-management features.
- Optional user-authored “Why I saved this” note.
- User-selected Read or Learn intent, with Read as the default.
- Deterministic effort bands based on article length.
- One source-grounded AI value line with a clear excerpt fallback; AI failure must not block saving.
- Fixed seven-day initial revisit rule with a user-selected date override.
- Rule-based eligibility and ordering: due items first, earliest due first, then oldest saved.
- One in-product resurfacing card at a time, with no more than one new item shown per day by default.
- A transparent “Why now” explanation.
- Open, Later, and Dismiss actions with deterministic state changes.
- Immediate Undo after Dismiss.
- Pause and Resume resurfacing without a complex settings area.
- Logging of capture outcomes, verified impressions, action attempts, resolved outcomes, state changes, AI success or fallback, and timing signals needed for the planned metrics.

### Should Have

- Optional Recall for user-selected Learn content only after the user explicitly marks it ready for recall and the source can support a grounded question.
- A post-Open control for a Learn item that lets the user select “Recall later” and choose a concrete recall date; nothing is scheduled automatically.
- Pasted-text capture as a fallback when a public URL cannot be read.
- AI intent suggestion, always subordinate to the user's selection.

### Later / Not Now

- PDF, video, audio, image, social-media, or multi-format ingestion.
- Browser extension, mobile share sheet, email forwarding, and bulk backlog migration.
- Notion, Obsidian, Google Drive, or other integrations.
- Search, semantic search, AI search, embeddings, RAG, or a knowledge graph.
- AI-selected timing, AI relevance ranking, complex personalization, or an advanced recommendation engine.
- Email, push, or mobile notifications.
- Statistics dashboards, streaks, gamification, overdue counts, or clear-the-backlog mechanics.
- Social features, collaboration, shared spaces, or recommendation feeds.
- Chatbot, agent, or multi-agent behavior.
- Full spaced-repetition scheduling.

## 10. Non-goals

V1 is not:

- a universal second brain;
- a general bookmark manager or generic link organizer;
- a replacement for Notion, Obsidian, or another PKM system;
- a chatbot or AI search engine;
- a semantic search or retrieval product;
- a knowledge graph;
- a social bookmarking network or recommendation feed;
- an automatic learning platform;
- a complex spaced-repetition system;
- a browser-extension or multi-integration product;
- a multi-agent system;
- a tool for shopping, restaurants, travel, entertainment wishlists, or random social-media saves;
- a system that forces the user to consume or clear everything they saved; or
- a product optimized for maximum engagement.

These exclusions protect the test of the core Wait → Resurface → Decision loop.

## 11. Resurfacing Experience

### What gets resurfaced

An item is eligible only when it:

- is a successfully captured public knowledge article;
- is not deleted or dismissed;
- has Read or eligible Learn intent;
- has reached its revisit date; and
- has not already been resolved in the current resurfacing cycle.

Archive-only items are not eligible. When multiple items are due, V1 selects the earliest due item and then the oldest saved item. AI does not choose or rank items in V1.

### When and how often

- Default first revisit: seven days after saving.
- The user may choose another date at capture.
- Later offers three days, seven days, or a custom date.
- V1 shows at most one new item per day and does not repeat an ignored card on the same day.
- The default channel is in-product only. External email or push acceptance remains a separate future hypothesis.

### Why this item

The card uses a transparent explanation such as “Scheduled when saved” or “Saved 7 days ago · due today.” It must not claim that AI selected the perfect moment.

### Minimum information architecture

In visual priority order:

1. Title, with source shown as secondary text.
2. One line of context: the user's own note first; otherwise a labeled source-grounded AI value statement; otherwise a source excerpt.
3. “Saved X days ago · estimated effort · why now.”
4. Open as the primary action, with Later and Dismiss as secondary actions.
5. Recall only when the item satisfies the restricted eligibility rules.

Topics, concept lists, confidence scores, backlog totals, long summaries, and recommendation explanations do not appear on the V1 card.

### One-screen test

Within 30 seconds of opening the home screen, a new user should be able to state:

1. RecallFlow brings selected saved knowledge back for a decision.
2. The current item appears because its transparent revisit rule is due.
3. The available actions are Open, Later, and Dismiss.

If this requires onboarding prose or a tour, the screen has failed the test.

## 12. Recall Interaction

Recall is a restricted Should Have experiment, not the default or only path.

### Recall may appear only when

- the user explicitly selected Learn intent;
- the user has explicitly marked the item ready for recall after engaging with it;
- the source text is sufficiently complete;
- the question can be answered from the source; and
- the user actively chooses Recall.

An Open click alone must not be treated as proof that the article was read or learned.

### Ready-for-Recall path

After a Learn item opens successfully, the user may return to its item view and select “Recall later.” They must choose a concrete date. This explicit action changes the item to Ready for Recall and creates the next due date. If the user does nothing, no Recall is scheduled. On or after the selected date, the next eligible card may offer Recall.

### Recall must not appear when

- the item has Read, Reference, Action, or Archive intent;
- it is the first resurfacing of an unread item;
- only a title or inadequate excerpt is available;
- extraction failed;
- a grounded question cannot be generated; or
- the user has disabled or skipped Recall.

### Minimal Recall flow

Recall → one source-grounded question → optional think or type step → Reveal → Got it / Open source / Later

“Got it” is a self-report, not evidence of mastery. V1 must not label this action Mastered or automatically start a spaced-repetition sequence.

If question generation fails, the item returns to the standard card with its value line or excerpt and Open, Later, and Dismiss.

## 13. User Feedback

V1 uses actions as low-effort feedback. It removes the persistent Useful, Not useful, Done, and Mastered button row because those labels either duplicate another action, add friction, or lack a defined consequence.

- **Must Have:** Open, Later, and Dismiss / No longer relevant.
- **Should Have experiment:** Recall, only for eligible Learn items.
- **Removed from V1:** Useful, Not useful, Done, and Mastered.

| Signal | Immediate product effect | Interpretation boundary |
|---|---|---|
| Open | Attempt to open the source; resolve the current event only after successful navigation. For a Learn item, a separate post-Open “Recall later” choice may create a new date | A successful source open is not proof that the article was read, used, or valuable |
| Later | Require a new date and hide the item until then | Means “not now”; repeated Later is a timing-risk signal, not repeated success |
| Dismiss / No longer relevant | Remove the item from future resurfacing while retaining an immediate Undo | May represent a useful decision or bad selection; action mix must be reviewed |
| Recall completed — Should Have | Resolve the current event without creating an automatic repetition schedule | Means the interaction was completed, not that learning occurred |
| No action | Do not repeat the card that day and make no preference inference | Silence does not mean dislike |

If an Open attempt cannot reach the source, the event remains unresolved and the standard actions return. The attempt is logged for diagnosis but does not count as Opened or as a reconnection.

Separate research interviews or test-session questions may measure perceived usefulness, but V1 does not add a persistent usefulness button that has no defined product-state effect.

## 14. AI Role

AI is used only where source language needs compression or transformation:

- generate a single source-grounded description of what the article covers;
- optionally suggest a small number of important concepts for evaluation, not the main card;
- optionally suggest Read or Learn intent, subject to user confirmation;
- generate one grounded Recall question for an eligible learning item; and
- later, only after baseline evidence, test whether relevance or timing estimation improves outcomes.

AI must not:

- invent why the user personally saved an item;
- silently override user intent;
- decide V1 timing or queue order;
- prevent capture or resurfacing when generation fails;
- claim that an item is important to the user;
- infer that Open means read or Recall means learned; or
- present personalization that the product has not actually implemented or evaluated.

Every AI output should be attributable to the source, distinguishable from a user note, correctable or removable, and compared with a simpler baseline.

## 15. Rule-based / AI Boundary

| Product task | V1 authority | AI role | Fallback or constraint |
|---|---|---|---|
| Title and source | Source metadata plus user correction | Not required | URL and source domain |
| Why I saved this | User | AI must not infer it | May remain empty |
| Intent | User; default Read | Optional suggestion only | User selection always wins |
| Effort estimate | Deterministic length bands | Not required | Show Unknown when content is unavailable |
| One-line content value | Source-grounded AI | Generate a concise description | Use a source excerpt or omit |
| Topics and concepts | Out of the core card | Optional later evaluation | Do not display in V1 card |
| Recall eligibility | User intent and explicit readiness rules | Cannot override eligibility | Hide Recall |
| Recall question | Optional AI | Generate only from adequate source text | Standard card and direct Open |
| First revisit time | Seven-day rule plus user override | None in V1 | Transparent fixed schedule |
| Queue eligibility and order | Due-date and state rules | None in V1 | Earliest due, then oldest saved |
| Feedback effect | Explicit action rules | No implicit learning in V1 | Deterministic state changes |
| “Perfect moment” | Not promised | Future research hypothesis only | User-controlled baseline |

AI-assisted timing should be considered only if baseline testing produces repeated evidence that content remains valuable but appears at the wrong time.

## 16. Success Metrics

All metrics below are planned definitions. No targets or results are claimed.

### Primary metric — Meaningful Decision Rate

**Definition:** Unique viewed resurfacing events that reach one qualifying resolved outcome within the pre-registered observation window, divided by unique viewed resurfacing events.

Qualifying actions:

- a confirmed successful source open;
- Later only when a new revisit date is saved;
- Dismiss / No longer relevant; or
- completed Recall when that optional branch is eligible.

Passive exposure, an unsuccessful open attempt, duplicated clicks, automated state changes, notification dismissal, and card views without a resolved outcome do not count. Attempted actions remain diagnostic events.

Meaningful Decision Rate is more appropriate than Open Rate because the product job is to support a deliberate decision, not force content consumption. It still does not prove that an article was read or that the decision was valuable. Action mix must always be reported so a high rate caused mainly by Dismiss or repeated Later is not presented as product success.

### Secondary metrics

1. **Reconnection Rate:** Viewed resurfacing events followed by a confirmed successful source open or completed Recall, divided by viewed resurfacing events. This separates reconnection from backlog triage, while still not claiming reading or learning.
2. **Repeated-Later Rate:** Eligible resurfaced items that receive Later in more than one resurfacing cycle, divided by all eligible resurfaced items. Immediate-Later rate after the first presentation is reported as a separate timing diagnostic.
3. **High-Intrusiveness Rate:** Survey respondents who have experienced at least one resurfacing and rate the experience as clearly intrusive, divided by all respondents who have experienced at least one resurfacing. The exact scale and threshold must be specified before data collection. Survey response rate, pause, disable, and experiment attrition must be reported separately so non-response is not treated as a non-intrusive rating.

Decision time, capture abandonment, action mix, AI correction rate, summary fallback rate, and Recall funnel events are diagnostic measures rather than additional headline success metrics.

## 17. Failure Modes

Severity definitions: **Critical** can invalidate the core value or drive users to leave; **High** materially damages value or trust; **Medium** harms usability or learning but can be isolated without destroying the core flow.

| Failure mode | Severity | Detection signal | Possible mitigation — not yet implemented |
|---|---|---|---|
| Bad resurfacing | Critical | Immediate Dismiss, persistent no-action, inability to explain why the item deserves attention, or repeated rejection of similar items | Restrict the eligible pool, show very few items, explain why now, honor Dismiss, and fall back to transparent rules |
| Bad timing | High | An item is still valued but immediately receives Later; the same item is repeatedly postponed; users report “not now” | Let users override dates, provide Later and Pause, default to low frequency, and record timing separately from relevance |
| Notification fatigue | Critical | Intrusiveness rises, Meaningful Decision Rate falls, users pause or disable resurfacing, or describe another inbox | Enforce a low exposure cap, avoid repeated prompts, provide Pause, and avoid streaks, unread badges, or backlog-clearing pressure |
| Wrong intent classification | High | Users change Learn to Read, disable or repeatedly skip Recall, or state that the content is not for learning | Keep user-selected intent authoritative; use AI only as an editable suggestion; show Recall only under explicit rules |
| Poor AI summary | High | Users correct or reject it, source audits find contradictions, or the card becomes misleading | Keep one source-grounded line, preserve title and source, allow correction or removal, and fall back to an excerpt |
| Bad Recall question | High | The question is unsupported, too easy or difficult, rarely selected or completed, or raises dismissal | Restrict it to ready Learn items, keep it optional, audit grounding, and provide direct Open or takeaway alternatives |
| Feedback friction | Medium | Feedback completion declines, exits rise around prompts, or users click only to close the interface | Use actions as feedback, sample explicit questions sparingly, and remove any signal without a deterministic purpose |
| Backlog pressure | Critical | Users express anxiety, avoid opening, mass-dismiss items, or interpret the screen as an overdue task list | Show one item, hide backlog totals and overdue language, normalize Dismiss, provide Pause, and avoid repeated pursuit |

These mitigations are design options for later testing, not implemented features or proven solutions.

## 18. User Control

V1 provides the smallest controls needed to remain predictable:

- select Read or Learn intent;
- add or edit the personal why-I-saved note;
- correct or remove the AI value line;
- override the first revisit date;
- choose a concrete date through Later;
- Dismiss or never resurface an item, with immediate Undo;
- skip Recall every time;
- after opening a Learn item, explicitly choose whether to schedule “Recall later” and select its date;
- pause and resume all resurfacing; and
- use a simple low-frequency pace or accept the one-item-per-day default.

These controls should appear at the point of action. V1 does not need a complex settings page, model settings, recommendation weights, tagging rules, or algorithm controls.

## 19. Assumptions

| Assumption | Risk | Current evidence status | Evidence required before expansion |
|---|---|---|---|
| Target users want resurfacing | Very high | Unvalidated product hypothesis | Primary interviews and behavior in a resurfaced-versus-holdout test |
| A useful time can be identified | Very high | “Right time” remains undefined | Fixed-rule and user-selected-date comparison before any AI timing claim |
| Users act when content is shown | High | Not established by existing research | Viewed-event action data and qualitative follow-up |
| A minimal card restores enough context | High | Information architecture hypothesis | Timed comprehension and decision comparison with title-only cards |
| Recall helps eligible learning content | High | Solution hypothesis | Grounded question comparison after explicit learning readiness |
| AI context beats an excerpt or user note | Medium–high | Untested | Controlled quality and decision experiment against non-AI variants |
| Users will provide repeated feedback | Medium | Untested | Observe action completeness and sampled feedback burden |
| In-product exposure represents proactive demand | High | Not established | Separate later test of off-platform channel and cadence acceptance |

### Decision log

| Question | Decision and trade-off |
|---|---|
| Why not build another bookmark manager? | Capture already has many low-friction alternatives. V1 provides only enough capture to test after-save value. |
| Why focus on high-frequency savers? | A persistent save–revisit gap is easier to observe in this behavioral group, but greater pain is still a hypothesis. |
| Why focus V1 on knowledge-oriented content? | It creates a coherent job around reading or learning and avoids unrelated shopping, travel, entertainment, and generic link-management needs. |
| Why restrict the primary use case to a public article URL? | It removes PDF and multi-format extraction as confounding variables while retaining a realistic knowledge-saving action. |
| Why not make Recall mandatory? | Not every item is learning content, and active recall is one of the highest-risk solution hypotheses. |
| Why can intentional dismissal be potentially successful? | The product helps users decide; confirming that an item no longer matters may remove uncertainty. Dismiss must still be separated from rejection caused by bad resurfacing. |
| Why use a seven-day baseline? | It is transparent, inexpensive, editable, and comparable. It is not assumed to be optimal. |
| Why keep a non-AI baseline? | It separates whether resurfacing works from whether AI adds incremental value. |
| Why use an in-product queue first? | It isolates card and decision value from external-channel annoyance. The trade-off is that it cannot validate off-platform proactive re-engagement. |
| Why not add RAG, search, or agents now? | They mainly address retrieval or technical complexity, while V1's highest risks concern demand, timing, action, and interruption. |

The hardest decision is to keep Recall out of the primary path despite the product name. Evidence does not support making a learning interaction mandatory for all saved knowledge.

## 20. Open Questions

1. Does the behaviorally defined target group experience the save–revisit gap frequently and painfully enough to adopt a separate product?
2. Is a public article with Read intent the best first use case, or should V1 narrow further to an explicit work or learning context?
3. Is seven days a tolerable default, and how often will users override it?
4. Does one in-product item per day feel useful, too slow, or still pressuring?
5. Can a value line restore context without fabricating personal relevance?
6. Does the user-authored why-I-saved note provide more value than AI-generated context?
7. Which no-action pattern should retire or defer an item without guessing user preference?
8. When does an item become legitimately ready for Recall?
9. Does optional Recall outperform a concise takeaway for eligible learning content?
10. Does AI improve a meaningful outcome over metadata, excerpts, and transparent timing rules?
11. Is Meaningful Decision Rate valid, or can it be inflated by low-value Later and Dismiss actions?
12. Which external channel and cadence, if any, can bring users back without notification fatigue?

The in-product V1 can test whether a resurfacing card supports a useful decision after it is seen. It cannot, by itself, validate whether users want off-platform proactive reminders or whether such reminders cause them to return.
