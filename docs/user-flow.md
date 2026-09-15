# RecallFlow — V1 User Flow

**Scope:** One public, knowledge-oriented article saved for later.  
**Primary outcome:** The user makes an intentional Open, Later, or Dismiss decision after the article resurfaces.  
**Status:** Provisional product flow; not yet validated with users.

Recall is a conditional experiment for eligible learning content. It is not part of the default path and does not appear for an unread article.

## End-to-End Happy Path

### 1. Add content

The user encounters a public knowledge article that appears useful for work or learning but cannot be processed now.

They add the article URL to RecallFlow.

RecallFlow confirms the title and source, estimates the reading effort, and prepares a one-line description of what the article covers. If the description is unavailable, the flow continues with a source excerpt or title and source only.

The user may add a short “Why I saved this” note. A user-authored note always takes priority over generated context.

### 2. Confirm intent and timing

The item defaults to:

- Intent: Read
- First revisit: 7 days after saving

The user may change the intent to Learn or choose another revisit date. RecallFlow does not claim that seven days is the ideal time or that AI selected it.

The user selects Save.

### 3. Wait

RecallFlow remains quiet until the revisit date.

The item does not create an overdue badge, streak, unread count, or backlog-clearing pressure. The user may change its date, delete it, or pause resurfacing before it becomes due.

### 4. Resurface

On or after the due date, the user's next visit to RecallFlow shows one resurfacing card.

The card contains only:

- article title and source;
- the user's why-I-saved note, or a labeled one-line content description or excerpt;
- saved age;
- estimated effort;
- a transparent explanation such as “Scheduled when saved” or “Saved 7 days ago · due today”; and
- Open, Later, and Dismiss.

The user should understand within approximately 5–15 seconds:

1. what the article is;
2. why it appears now; and
3. what actions are available.

### 5. Open

The user selects Open.

The original article opens successfully. RecallFlow records the navigation separately from the initial click. It does not infer that the article was read, used, understood, or valuable.

The current resurfacing event is resolved and the card leaves today's view. The item is not scheduled again automatically. If this is a Learn item, the user may later return to its item view and explicitly choose Recall later; otherwise no new date is created.

### Happy-path summary

Add public article URL  
↓  
Confirm title, source, context, Read intent, and revisit date  
↓  
Save  
↓  
Wait silently  
↓  
One card appears on or after the due date  
↓  
Understand title, context, effort, and why now  
↓  
Open  
↓  
Source opens and the current resurfacing event is resolved

## Open Path

**Entry:** A due resurfacing card is visible.

1. The user selects Open.
2. RecallFlow records an open attempt and tries to open the original source.
3. After successful navigation, RecallFlow records source opened and resolves the current event.
4. The current card is removed from today's resurfacing view.
5. No future date is created automatically.
6. For a Learn item only, the item view may offer Recall later. Selecting it requires a concrete date and leads to the Recall path; ignoring it creates no new schedule.

**Alternative outcome:** If the source is unavailable, RecallFlow records the failed attempt, does not resolve the event, and restores the actionable card. The user may try Open again or choose Later or Dismiss. A broken source is neither a resolved Open outcome nor a successful reconnection.

**End state:** The current resurfacing event is resolved as Opened.

## Later Path

**Entry:** A due resurfacing card is visible, but the user does not want to engage now.

1. The user selects Later.
2. RecallFlow offers three days, seven days, or a custom date.
3. The user chooses a concrete next date.
4. RecallFlow confirms “Returns on [date].”
5. The current card leaves today's view.
6. The item remains hidden until the new date.

Selecting Later without saving a new date does not resolve the event.

A first dated Later can represent a useful timing decision. Repeated Later actions on the same item are treated as evidence of timing mismatch or avoidance, not as repeated product successes.

**End state:** The item is Rescheduled with a new revisit date.

## Dismiss Path

**Entry:** A due resurfacing card is visible, and the user decides the item no longer deserves future attention.

1. The user selects Dismiss / No longer relevant.
2. RecallFlow removes the item from future resurfacing.
3. An immediate Undo action appears.
4. If the user does not undo, the item remains excluded from resurfacing.

Dismiss does not automatically delete the underlying saved item. Deletion is a separate user decision.

Dismiss may be a useful outcome because the product is intended to support decisions rather than force consumption. A high Dismiss rate can also signal irrelevant resurfacing, so it must not be presented as success without reviewing the action mix and user feedback.

**End state:** The item is Dismissed and no longer eligible to resurface.

## Recall Path

**Entry:** Recall appears only as an optional action for an eligible learning item that has completed the explicit readiness path.

An item is eligible only when:

- the user selected Learn intent;
- the user explicitly marked it ready for recall after engaging with the source;
- sufficient source text is available;
- a question can be answered from the source; and
- the user has not disabled or skipped Recall.

An initial, unread item is not eligible. Open alone is not proof that the content was read.

### Readiness path

1. A Learn item follows the Open path and its source opens successfully.
2. The user returns to the item's view after engaging with the source.
3. The user explicitly selects Recall later.
4. The user chooses a concrete recall date.
5. RecallFlow confirms the date and marks the item Ready for Recall.
6. The product waits silently until that date.
7. On or after the selected date, the next eligible card may offer Recall.

If the user never selects Recall later, no Recall event is created.

### Recall interaction

1. The user selects Recall.
2. RecallFlow shows one source-grounded question.
3. The user may think or type an answer.
4. The user selects Reveal.
5. RecallFlow shows the source-grounded answer or relevant excerpt.
6. The user chooses Got it, Open source, or Later.

Got it is a self-report about the current interaction. It is not labeled Mastered and does not prove learning.

- **Got it:** resolves the current Recall event without starting an automatic repetition schedule.
- **Open source:** follows the Open path.
- **Later:** follows the Later path and requires a concrete date.

If a reliable question cannot be produced, Recall is hidden and the standard Open, Later, and Dismiss actions remain available.

**End state:** The Recall interaction is completed, the source is opened, or the item is rescheduled.
