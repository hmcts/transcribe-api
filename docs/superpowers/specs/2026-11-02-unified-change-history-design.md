# Unified change history (DIAAT-371)

Date: 2026-11-02
Repos: transcribe-api, transcribe-web
Status: design, ready for build

## Intent

Clerks change AI transcripts in six ways: they edit a section's text, edit a run of words, accept a section as it is, rename a section's speaker, rename a speaker everywhere, and combine two sections. History exists for two reasons.

The first reason is undoing mistakes. A clerk who makes a wrong edit, renames the wrong speaker or combines the wrong sections needs to undo it. The undo must restore exactly what was there before, including the parts of the transcript that change has touched.

The second reason is training on corrections. The difference between the AI output and the clerk's final transcript is what a model can learn from. That only works if the training data matches the transcript as it finally stands. A correction the clerk later undid is not a correction.

Today neither reason is served well. This document says what to build and why. It leaves some details to the builder, and the section "Left to the builder" lists them.

## Current state

The code was read on origin/master at 75cd54d (transcribe-api) and on origin/master of transcribe-web. Line numbers are approximate.

### Storage

History is the `correction_history` list (`CorrectionEntry`) inside each `DialogueEntry` (`src/transcribe_api/domain/models_recording.py`). All entries are stored in the `transcription_job.dialogue_entries` JSONB column. A `CorrectionEntry` has a timestamp, a kind (`segment`, `word_range`, `rollback`, `accept_all`), the whole section's text before and after, and, for word-range edits only, the word range and the changed phrase. It does not record who made the change. Each write replaces the whole `dialogue_entries` list (`_save_entries`, `routes_recording.py` ~1281).

Section ids (`seg_<hex>`, DIAAT-367) stay the same across edits, rollbacks and renames. A combine gives the result a new id. `_entries_with_ids` derives an id for a legacy entry saved without one.

### Operations (`src/transcribe_api/api/routes_recording.py`)

- `correct_segment` (~1289): sets `corrected_text`, clears word corrections, appends a `segment` entry, and writes a dataset row.
- `correct_word_range` (~1341): replaces overlapping word corrections, appends a `word_range` entry with the changed phrase, and writes a dataset row.
- `accept_segment` (~1525): sets `accepted` and appends an `accept_all` entry whose before and after text are the same.
- `rename_speaker` (~1569): overwrites `speaker` on one section, or with `apply_to_all` on every section whose current label matches exactly. It writes no history. The docstring says a rename is deliberately not a text correction and a section rollback leaves it alone. The previous label is lost.
- `merge_segment_with_next` (~1455) uses `merge_dialogue_entries`. It writes no history entry for the combine. It folds both sections' histories into the new section and adds the other half's current text to each entry. It drops each entry's word range and sorts the result by timestamp. The second section's id, speaker and start time are lost, because the result takes the first section's speaker and start and gets a new id. `parts` (`EntryPart`) keeps each original section's AI text, its text at the moment of combining and its confidence. It does not keep enough to rebuild either original section.
- `rollback_to_history_entry` (~1609) takes any index, not just the latest. It reverts a single word-range edit exactly when nothing since overlaps it. Otherwise it restores the `previous_text` snapshot as `corrected_text`, which flattens every word correction and every later edit. It then appends a `rollback` entry, so nothing is removed from history. Rolling back to an `accept_all` entry clears `accepted` only on a combined section.
- `rollback_segment` (~1491) resets the section to its AI text: it clears corrections and `accepted`, and sets the history to `None`. It leaves the speaker alone and does not split a combined section.
- No endpoint rolls back the whole transcription.
- `_to_response` (~488) returns each section's history inside its `DialogueEntryResponse`. It has no job-level history and no version.

### Concurrency

None. Every write loads the job, changes the list in memory and commits the whole list. Two clerks editing different sections at the same time can overwrite each other's changes without any error. The only 409s are "this section no longer exists" (the id is not found) and the `next_segment_id` check on combine.

### Training dataset

`CorrectionDatasetEntry` (`correction_dataset_entry`, baseline migration `0001`) is written by `record_correction_dataset_entry` in `src/transcribe_api/domain/interface_recording.py`. It adds one row per text or word-range edit, in the same transaction as the job update. Rows are keyed by `segment_index`, a list position that a combine shifts. Rollbacks never delete or change rows, so the data keeps corrections the clerk has undone. Renames and combines are ignored. Writes happen only when `CORRECTIONS_DATASET_EXPORT_ENABLED` (`runtime/settings_recording.py`) is true. It is false by default and stays false until legal and compliance sign off on retention and anonymisation (DIAAT-231; see the class docstring).

### Web (transcribe-web)

- `components/recording/transcript/transcript-segment.tsx` has a per-section `HistoryPanel`. It offers "Roll back to before this" on every entry and "Roll back whole section", plus the speaker rename form and the combine controls.
- `components/recording/transcript/modification-history-table.tsx` is the whole-transcript table from DIAAT-230. It is read-only. `lib/recording/modification-history.ts` builds it by flattening each section's history and numbering sections by their current position (`segmentIndex + 1`). "Changed by" is `job.caller`, which `lib/recording/types.ts` says is always undefined, so the table always shows "Unknown".
- `components/recording/transcript/job-detail-view.tsx` sends section actions one at a time. It shows the same "section has changed, reload" message for every 409.
- The browser calls Next.js API routes under `app/api/jobs/[jobId]/segments/[index]/` (the `[index]` segment carries the section id). These routes call `lib/recording/api-client.ts`, which calls the API and passes `BackendApiError` statuses through.

## Decisions

### The user's decisions

1. Every modification type goes into history: segment text edit, word-range edit, accept, single speaker rename, rename-all, and combine sections.
2. There is one history for the transcription. The per-section view and the whole-transcription view are both views of it.
3. Individual rollback works only on the latest change, one at a time, like a stack. In the section view it is the section's latest change. In the whole-transcription view it is the latest change overall.
4. A rename-all or combine that is the latest change is rolled back in full. For a rename-all, the name is restored everywhere it was applied. For a combine, the combined section is split back into the original two sections, with their ids, speakers, times, words and pre-combine state.
5. Rolling back a whole section resets that section to its AI text. Rolling back the whole transcription resets everything to the original AI output: it splits combined sections, restores the original speaker labels and empties the history.
6. A rolled-back change is deleted from history. There is no hidden or "undone" record.
7. Training uses only the final state paired with the original AI output. This work fixes the dataset: it keys rows by segment id, keeps them in step with rollbacks (deleting or updating rows), covers renames and combines, and keeps the existing flag and compliance gate.

### Our calls

- Our call: a section's latest change may be a multi-section change (rename-all or combine) that another section has changed since. In that case section rollback is refused, with a clear message naming the section that has a later dependent change. Nothing is ever partly undone.
- Our call: rolling back a whole section that was created by a combine also splits it back into the two original sections. Each keeps its pre-combine changes.
- Our call: every user sees the same history. Each entry records the user and the time, and the web's "Changed by" column shows the real user.
- Our call: concurrency is an optimistic version check on the transcription. A stale edit or rollback gets a 409, and the UI asks the user to refresh.
- Our call: a combine entry stores the full pre-merge state of both entries. A rename-all entry stores the affected segment ids and their previous labels.

## Goals

- One ordered history per transcription that records all six modification types, with the user and the time of each.
- Undo of the latest change, from the section view or the whole-transcription view, that restores the exact earlier state, including multi-section changes.
- Section reset and whole-transcription reset as defined in decision 5 and our calls.
- Training data that equals the final transcript paired with the AI output at all times, while the flag is on.
- No lost updates: a write based on a stale copy of the transcription fails visibly.

## Non-goals

- Redo. A rolled-back change is gone (decision 6).
- Undoing a change other than the latest, or picking changes out of the middle of the stack.
- Per-user or private histories.
- Turning on the dataset flag, or any retention or anonymisation work. The compliance gate stays exactly as it is.
- Editing the text of a history entry, or merging and splitting sections in new ways beyond undoing a combine.
- Real-time collaboration or live push of other users' changes. A stale client is told to refresh.

## Anti-goals

These outcomes must never happen. Tests should prove that each one cannot.

- A half-undone multi-section change: a rename-all restored in some sections but not others, or a combine split without restoring both original sections exactly.
- Training data that still holds rolled-back corrections, or that disagrees with the transcript as it stands.
- A concurrent edit silently lost: one user's write overwriting another's without either seeing an error.

## Design

### The change log

Each transcription has one ordered log of changes. Every entry has:

- its position in the log (a strictly increasing sequence per transcription);
- its kind: `segment`, `word_range`, `accept`, `rename`, `rename_all` or `combine`;
- the user who made it, the time it was made, and enough to display both;
- the section ids it touched;
- enough before-state to undo it exactly, without reading any other entry;
- display data, such as the changed phrase, the old and new label, or the text before and after.

The before-state per kind:

- `segment`, `word_range` and `accept`: the section's `corrected_text`, `word_corrections`, `accepted`, `unreviewed_confidence` and `parts` as they were before the change. Undo restores these fields. This replaces today's snapshot and surgical rollback logic. Each undo is exact because it only ever undoes the latest change, and nothing has built on it.
- `rename`: the section id and its previous label.
- `rename_all`: every affected section id and its previous label (our call). Today rename-all matches on the label, so different sections can have had different previous labels only if the builder changes the matching rule. Store the label per section anyway, so undo never depends on that rule.
- `combine`: the full pre-merge `DialogueEntry` of both sections, with their ids, speakers, times, words, alternatives, corrections, accept state and parts (our call). Also store the new section's id. Undo replaces the combined section with the two stored entries at its position.

The log must no longer be folded into sections, and combines must no longer rewrite it. Earlier entries for the two original sections keep their original section ids. The combine entry links those ids to the new one, so the section view of a combined section can show its constituents' earlier changes under it.

A rolled-back entry is deleted (decision 6). A section reset deletes that section's entries. A whole-transcription reset deletes every entry.

Recommended approach (the builder decides): a new table, for example `transcription_change`, with columns `job_id`, `seq`, `user_id`, `created_at`, `kind`, `segment_ids` and a JSONB `payload`, with a unique constraint on (`job_id`, `seq`). Each change writes its log row and its dialogue entries in one transaction. A table gives a real foreign key to `user`, cheap deletes, and lookups such as "the latest entry touching section X" without loading the whole transcript. A JSONB log column on `transcription_job` would also work. It is simpler to migrate, but every write must rewrite it.

### Dependencies between changes

A later change depends on an earlier one when they touch a common section id. A combine touches both original ids and the new id.

- Section view: the section's latest change is the latest entry that touches its id, or for a combined section, its id or the id that the combine created. Undoing it is refused when that entry is a `rename_all` or `combine` and a later entry touches any other section it affected. The 409 or 422 message names the first such section by its current label and position, so the clerk knows what to undo first (our call).
- Whole-transcription view: the latest entry overall never has dependants, so it can always be undone.

Here is an example of a refused undo. Speaker 2 is renamed to "Judge" everywhere, and then one of the renamed sections is edited. Undoing the rename-all from another renamed section is refused, and the message names the edited section. The clerk undoes that edit first, or uses the whole-transcription view.

### Resets

Section reset (decision 5) sets the section's text to its AI text. It clears corrections and `accepted` and deletes the section's entries from the log. If the section was created by a combine, it also splits back into the two original sections, which keep their pre-combine changes and the entries for them (our call). The reset is refused if it would delete an entry that a later multi-section change depends on, by the same rule and with the same message as individual undo.

Open question for the user: does a section reset also restore the section's speaker label? Decision 5 says "AI text". A single `rename` belongs to the section alone and can be removed with it. A `rename_all` that included the section cannot be removed from one section without partly undoing it. We recommend that a section reset also removes the section's single renames and leaves `rename_all` entries in place, but this needs the user's confirmation before build.

Whole-transcription reset rebuilds the transcript from the original AI output. It splits every combined section, restores the original speaker labels and empties the log. The original AI output should come from the stored pre-combine states and the AI fields (`text`, `words`, `speaker` from before any rename), not from a fresh Azure fetch. The builder should confirm this reconstruction is exact. If it is not, the builder should store an immutable copy of the original `dialogue_entries` when the job succeeds, for example a new column written by `save_job_results`.

### Users and visibility

Every entry records the acting user's id from `AuthenticatedUser` (our call). `User` holds `email` and `azure_user_id` and has no display name. The builder decides whether to show the email, take a name from the token at write time and store it on the entry, or add a column. The web's "Changed by" column shows that user instead of `job.caller`. Every user who can open the transcription sees the same log. Access control does not change: `_check_job_access` and the jurisdiction dependency still apply to every read and write.

### Concurrency

`transcription_job` gains a version number. Every read returns it. Every edit, rollback and reset sends the version it was based on. The write is a compare-and-set: either an `UPDATE ... WHERE id = :id AND version = :expected` that bumps the version, or the equivalent row lock and check. A mismatch returns 409 with a code distinct from the existing "section no longer exists" 409. The web shows "This transcript has changed since you loaded it. Refresh to see the latest version." and does not retry automatically (our call). Combine's `next_segment_id` check can stay as a more specific message.

Rollout order matters. The API should first return the version and accept writes without one. Then the web should send it. Then the API should require it. Otherwise a web deploy that lags the API would fail every write.

### Training dataset

The dataset must equal a function of the current transcript: each corrected section's original AI text paired with its final text. The simplest way to keep that invariant is to recompute the affected sections' rows in the same transaction as every change, undo and reset, rather than appending rows per edit.

- Key rows by segment id instead of `segment_index`.
- When a section's final text equals its AI text, the section has no row.
- A rename updates `speaker` on the affected rows. A rename-all updates every affected row.
- A combine replaces both original sections' rows with the combined section's row. Undoing the combine or resetting the section restores them. The combined section's `parts` give per-original-section pairs if the builder prefers to keep rows at the original sections' granularity.
- Whether word-range corrections stay as separate rows or the row is always the whole section is left to the builder, as long as every row reflects the final state.
- `record_correction_dataset_entry` keeps its `CORRECTIONS_DATASET_EXPORT_ENABLED` check, and the class keeps its compliance note. When the flag is off, nothing is written or deleted.

The flag has been off everywhere, so the table should be empty. The builder should check this in each environment before changing its key. If it has rows, they can be rebuilt from the current transcripts.

### API shape

The job response carries the version and the transcription's change log, or a separate endpoint serves the log. Each entry carries its kind, user, time, section ids, display data and whether it can be undone from the whole-transcription view (only the latest can). Each section in the response, or the log itself, lets the web find a section's latest entry and whether undoing it would be refused.

The new operations are: undo the latest change for a section, undo the latest change overall, reset a section, and reset the whole transcription. Today's `history/{history_index}/rollback` and `rollback` endpoints are replaced or redefined. The web calls all of them through Next.js API routes under `app/api/jobs/[jobId]/`, never directly from the browser.

### Web

- The section history panel lists the section's entries, newest first. It offers undo only on the newest one, and shows the refusal message when the API refuses. It keeps "Roll back whole section".
- The whole-transcription table shows all six kinds and the real user. It identifies sections by id and shows their current position and label, so its rows stay correct after a combine. Rows for sections that no longer exist say which section they were combined into. It offers undo on the newest row and a whole-transcription reset.
- `job-detail-view.tsx` stores the version from each response, sends it with every action, and distinguishes the stale-version 409 from the section-gone 409.

## Constraints

- Migrations go only through master (README "Branches and environments"). Any schema change, such as the log table, the version column, the dataset key change or a stored original-output column, lands first as an additive Alembic migration in its own PR to master. Then master is merged into develop, and then the feature goes in. The next revision is `0006`. `tests/unit/test_migrations.py` covers migrations.
- Old and new pods run side by side during a rollout. New code must read jobs whose history is still inline `correction_history`, and old pods' writes in that window must not corrupt the log.
- Both repos have tests, and this work must extend them. transcribe-api uses pytest under `tests/unit/` (for example `recording/unit/api/test_routes.py`, `recording/unit/database/test_correction_dataset.py` and `recording/integration/test_dialogue_entries_persistence.py`). transcribe-web uses Vitest under `tests/recording/unit/` (the segment, speaker and merge-next route tests, `modification-history.test.ts` and `modification-history-table.test.tsx`) and Playwright under `tests/recording/e2e/` (`modification-history.spec.ts` and `speaker-rename.spec.ts`).
- The web calls the API only through Next.js API routes, which must pass through the version and the new 409 code.

Tests should cover at least the following:

- Each of the six kinds writes an entry with the user, and undoing it restores the exact previous state.
- Undoing a rename-all restores every label.
- Undoing a combine gives back both original sections exactly as they were, with their ids.
- An undo that would affect a dependent change is refused, and the message names the section.
- Section reset of a combined section splits it.
- Whole-transcription reset gives back the original AI output and an empty log.
- Every dataset row matches the final transcript after any sequence of changes, undos and resets with the flag on, and nothing is written with the flag off.
- Two writes based on the same version: one succeeds and the other gets a 409.

## Left to the builder

- Storage shape: a new table or a JSONB log, the migration details, and how to migrate existing `correction_history` data. Existing entries have no user, contain `rollback` entries that decision 6 does not allow, and on combined sections have lost their word ranges. One option is to import them as entries with an unknown user and drop the `rollback` entries, with snapshot restore for undoing them. Another is to keep them read-only as legacy history.
- Exact endpoint paths and request and response shapes.
- UI layout details.
- How the user is shown (email, name from the token, or a new column).
- Whether dataset rows are per section, per original section or per word range, within the rules in "Training dataset".

## Open questions

- Whether a section reset restores the section's speaker label (see "Resets"). This needs the user's answer before build.
