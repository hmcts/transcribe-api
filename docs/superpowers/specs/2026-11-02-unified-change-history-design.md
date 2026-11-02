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
8. Jobs edited before this ships cannot be rebuilt exactly. Their renames overwrote the original speaker, their combines keep only `parts`, and their Azure batch job has been deleted. On those jobs, the following actions are hidden in the UI and refused by the API, with a clear message explaining why: whole-transcription reset, and undoing or resetting a combine or rename made before this ships. Partial or best-effort resets are not allowed. On new jobs everything stays exact, using an immutable copy of the original AI output, speakers included, stored when the job's results are saved. The API says whether an exact reset is possible, so the web can hide the controls.
9. A section reset restores the speaker for single renames. A rename-all that covers the section survives the reset, because removing it from one section would half-undo it. The dependency refusal applies wherever a reset would otherwise break a later multi-section change.
10. Anyone with write access to the transcription (the existing `_check_job_access` write check) can undo or reset anyone's change. There is no author-only rule. Whole-transcription reset requires an explicit confirmation step in the UI.
11. Speaker corrections are training signal. Dataset rows store the original AI speaker label and the final speaker label, so a section whose only correction is a rename still produces a row. The new columns go in the migration-first PR.

### Our calls

- Our call: a section's latest change may be a multi-section change (rename-all or combine) that another section has changed since. In that case section rollback is refused, with a clear message naming the section that has a later dependent change. Nothing is ever partly undone.
- Our call: rolling back a whole section that was created by a combine also splits it back into the two original sections. Each keeps its pre-combine changes.
- Our call: every user sees the same history. Each entry records the user and the time, and the web's "Changed by" column shows the real user.
- Our call: concurrency is an optimistic version check on the transcription. A stale edit or rollback gets a 409, and the UI asks the user to refresh.
- Our call: a combine entry stores the full pre-merge state of both entries. A rename-all entry stores the affected segment ids and their previous labels.
- Our call: on a legacy job, a section reset restores the AI text, undoes the changes recorded since this ships under the same rename-all rules as a new job, and says that it leaves labels from renames made before this ships as they are. Those renames left no record, so they cannot be detected or refused one by one. This needs the user's confirmation (see "Resets").
- Our call: existing `correction_history` entries on legacy jobs are shown in history but cannot be undone one by one. They record only text snapshots, so undoing them would not be exact. A section reset still clears them, because the AI text (`text`) has never been changed.

## Goals

- One ordered history per transcription that records all six modification types, with the user and the time of each.
- Undo of the latest change, from the section view or the whole-transcription view, that restores the exact earlier state, including multi-section changes.
- Section reset and whole-transcription reset as defined in decisions 5, 8 and 9 and our calls.
- Every reset or undo the system offers is exact. Anything that cannot be done exactly is hidden and refused, with the reason.
- Training data, including speaker corrections, that equals the final transcript paired with the AI output at all times, while the flag is on.
- No lost updates: a write based on a stale copy of the transcription fails visibly.

## Non-goals

- Redo. A rolled-back change is gone (decision 6).
- Undoing a change other than the latest, or picking changes out of the middle of the stack.
- Per-user or private histories, or restricting undo to the change's author (decision 10).
- Rebuilding the original AI output of legacy jobs, for example by re-running Azure Speech.
- Turning on the dataset flag, or any retention or anonymisation work. The compliance gate stays exactly as it is.
- Editing the text of a history entry, or merging and splitting sections in new ways beyond undoing a combine.
- Real-time collaboration or live push of other users' changes. A stale client is told to refresh.

## Anti-goals

These outcomes must never happen. Tests should prove that each one cannot.

- A half-undone multi-section change: a rename-all restored in some sections but not others, or a combine split without restoring both original sections exactly.
- A partial or best-effort reset presented as a reset, for example on a legacy job.
- Training data that still holds rolled-back corrections, or that disagrees with the transcript as it stands.
- A concurrent edit silently lost: one user's write overwriting another's without either seeing an error.

## Design

### Original AI output

When the poller saves a job's results (`save_job_results`, `interface_recording.py` ~130), it also stores an immutable copy of the dialogue entries as produced: ids, speakers, times, text, words, alternatives and confidence. Nothing ever writes to that copy again. It is the reference for whole-transcription reset, for the original speaker of every section, and for the dataset's original speaker label.

A job is legacy when it has no stored copy. That covers every job whose results were saved before this ships, whether or not it has been edited. Changes made after this ships are fully recorded on legacy jobs too, so they can still be undone exactly. Only the actions in decision 8 are refused there.

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

A rolled-back entry is deleted (decision 6). A section reset deletes the section's entries that it undoes. A whole-transcription reset deletes every entry.

Legacy entries are the `correction_history` entries saved before this ships. They are shown with an unknown user and cannot be undone individually (our call). They are always older than any new entry, so they sit at the bottom of the stack and never block an undo.

Recommended approach (the builder decides): a new table, for example `transcription_change`, with columns `job_id`, `seq`, `user_id`, `created_at`, `kind`, `segment_ids` and a JSONB `payload`, with a unique constraint on (`job_id`, `seq`). Each change writes its log row and its dialogue entries in one transaction. A table gives a real foreign key to `user`, cheap deletes, and lookups such as "the latest entry touching section X" without loading the whole transcript. A JSONB log column on `transcription_job` would also work. It is simpler to migrate, but every write must rewrite it.

### Dependencies between changes

A later change depends on an earlier one when they touch a common section id. A combine touches both original ids and the new id.

- Section view: the section's latest change is the latest entry that touches its id, or for a combined section, its id or the id that the combine created. Undoing it is refused when that entry is a `rename_all` or `combine` and a later entry touches any other section it affected. The 409 message names the first such section by its current label and position, so the clerk knows what to undo first (our call).
- Whole-transcription view: the latest entry overall never has dependants, so it can always be undone, unless it is a legacy change (decision 8).

Here is an example of a refused undo. Speaker 2 is renamed to "Judge" everywhere, and then one of the renamed sections is edited. Undoing the rename-all from another renamed section is refused, and the message names the edited section. The clerk undoes that edit first, or uses the whole-transcription view.

### Resets

A section reset (decisions 5 and 9) does the following:

- It sets the section's text to its AI text and clears its corrections and `accepted`.
- It restores the section's speaker from before its single `rename` entries.
- It keeps any `rename_all` that covers the section, and the label it applied.
- It deletes the entries it undid from the log.
- If the section was created by a combine, it splits the section back into the two original sections. Each keeps its pre-combine changes and their entries (our call).

A reset never leaves a surviving entry with stale before-state. When it deletes a section's single renames and keeps a `rename_all` that covers the section, it rebases that rename-all's stored previous label for the section. The new previous label is the label the section would have with the deleted renames removed: the label set by the previous surviving rename-all for the section, or its AI label if there is none. The section keeps the label of its latest surviving rename-all, or gets its AI label back if none covers it. Here is an example. Section A is renamed from "Speaker 1" to "Judge" on its own, and then a rename-all changes A to "Chair". Resetting A deletes the single rename, keeps "Chair", and rebases the rename-all so that undoing it later restores "Speaker 1", not the deleted "Judge".

The reset is refused, by the same rule and with the same message as an individual undo, when a surviving multi-section change would depend on something the reset removes and cannot be rebased. The main case is splitting a combined section that a later rename-all or combine touched: that change refers to the combined section's id, which the split removes. The builder may narrow the rule to the cases that really conflict, but must never let a reset partly undo a multi-section change or leave a surviving entry that undoes to a deleted state.

On a legacy job (decision 8):

- A section reset is refused if the section is a combine made before this ships. The section has `merged` set and no `combine` entry, and it cannot be split exactly.
- Renames made before this ships left no record, so nothing can tell which labels they changed, and the API cannot refuse them one by one. A section reset on a legacy job does what it does on a new job for every change recorded since this ships. It restores the AI text, clears corrections and `accepted`, and removes single renames. It keeps any covering rename-all, with the same rebasing and refusal rules. It does not touch a label changed before this ships. The UI and the API message for a legacy job say plainly that the reset covers the text and the changes made since this ships, not speaker names changed before (our call). The alternative is to refuse section reset on every legacy job, which would also take away text resets that are exact. The user should confirm which they want.
- Whole-transcription reset is refused.

Whole-transcription reset on a new job replaces the dialogue entries with the stored original AI output. This splits every combined section, restores the original speaker labels and ids, and empties the log. The web asks the user to confirm before sending it (decision 10).

### Users and visibility

Every entry records the acting user's id from `AuthenticatedUser` (our call). `User` holds `email` and `azure_user_id` and has no display name. The builder decides whether to show the email, take a name from the token at write time and store it on the entry, or add a column. The web's "Changed by" column shows that user instead of `job.caller`. Every user who can open the transcription sees the same log.

Anyone who passes `_check_job_access(job, user, "transcript", "write")` and the jurisdiction dependency can make, undo or reset any change, whoever made it (decision 10). Access control does not otherwise change.

### Concurrency

`transcription_job` gains a version number. Every read returns it. Every edit, rollback and reset sends the version it was based on. The write is a compare-and-set: either an `UPDATE ... WHERE id = :id AND version = :expected` that bumps the version, or the equivalent row lock and check. A mismatch returns 409 with a code distinct from the existing "section no longer exists" 409. The web shows "This transcript has changed since you loaded it. Refresh to see the latest version." and does not retry automatically (our call). Combine's `next_segment_id` check can stay as a more specific message.

Rollout order matters, for the web and for old API pods.

- Web: the API first returns the version and accepts writes without one. Then the web sends it. Then the API requires it. Otherwise a web deploy that lags the API would fail every write.
- API: old pods rewrite the transcript without the version or the log. A new pod could then undo a logged change whose before-state predates an old pod's edit, and lose that edit without anyone seeing it. Two releases prevent this. The first release only makes every transcript write bump the version, and checks the version when one is sent. It does not change history. The second release adds the log and the new undo and reset operations. During its rollout, the only other writers are first-release pods, which bump the version.
- Each log entry records the version before and after it. When an entry's "before" version is not the "after" version of the entry below it, a write happened that the log did not record. Undo stops at that gap: the entries below it become non-undoable, like legacy entries, and the API says why. A section reset or whole-transcription reset is still exact, because it works from the AI text or the stored original output rather than from those entries. An edit made by an old pod during the transition is therefore never discarded by an undo.

### Training dataset

The dataset must equal a function of the current transcript: for each changed section, its original AI text and speaker paired with its final text and speaker. The simplest way to keep that invariant is to recompute the affected sections' rows in the same transaction as every change, undo and reset, rather than appending rows per edit.

- Key rows by segment id instead of `segment_index`.
- Each row stores the original AI speaker label and the final speaker label (decision 11), as well as the original and final text.
- A section has a row when its final text or its final speaker differs from the AI output. A section whose only change is a rename still has a row. An accept alone produces none.
- A rename or rename-all updates the final speaker on the affected rows, or creates or deletes them.
- On new jobs the original speaker comes from the stored original AI output. On legacy jobs it is unknown, because renames made before this ships overwrote it. The builder stores it as null there and does not guess it from the current label, so training can leave those rows out.
- A combined section keeps the lineage of its sources. Each source's segment id, its original AI speaker label and its AI text come from the stored pre-merge states and the original AI output. `EntryPart` today has no source id or speaker, so it gains them for combines made after this ships. The dataset holds one row per source segment, or one structured row that lists every source with its own original speaker. Either way, each source's original speaker is paired with the combined section's final speaker. A section produces training data when any source's text or speaker differs from the AI output, for example when two speakers' sections are combined under the first speaker's label. Undoing the combine or resetting the section restores the sources' own rows. For combines made before this ships, the source ids and speakers are unknown and are stored as null.
- Whether word-range corrections stay as separate rows or the row is always the whole section or source is left to the builder, as long as every row reflects the final state.
- `record_correction_dataset_entry` keeps its `CORRECTIONS_DATASET_EXPORT_ENABLED` check, and the class keeps its compliance note. When the flag is off, nothing is written or deleted.

The flag has been off everywhere, so the table should be empty. The builder should check this in each environment before changing its key. If it has rows, they can be rebuilt from the current transcripts.

### API shape

The job response carries the following:

- the version;
- the transcription's change log, unless a separate endpoint serves it;
- whether an exact whole-transcription reset is possible, and the reason when it is not (decision 8);
- per section, whether a reset is possible and the reason when it is not.

Each log entry carries its kind, user and time, the section ids it touched, its display data, and whether it can be undone. The web uses these flags only to hide controls. The API enforces every rule on its own and returns the same messages.

The new operations are: undo the latest change for a section, undo the latest change overall, reset a section, and reset the whole transcription. Today's `history/{history_index}/rollback` and `rollback` endpoints are replaced or redefined. The web calls all of them through Next.js API routes under `app/api/jobs/[jobId]/`, never directly from the browser.

### Web

- The section history panel lists the section's entries, newest first. It offers undo only on the newest one when the API says it can be undone. It shows the refusal message when the API refuses. It keeps "Roll back whole section", which is hidden when a reset is not possible, with the API's reason shown instead.
- The whole-transcription table shows all six kinds and the real user. It identifies sections by id and shows their current position and label, so its rows stay correct after a combine. Rows for sections that no longer exist say which section they were combined into. It offers undo on the newest row. It offers whole-transcription reset only when the API says it is possible, and only after an explicit confirmation step. On a legacy job it says why reset is not available.
- `job-detail-view.tsx` stores the version from each response, sends it with every action, and distinguishes the stale-version 409 from the section-gone 409.

## Constraints

- Migrations go only through master (README "Branches and environments"). The schema changes land first as additive Alembic migrations in their own PR to master: the stored original AI output, the version column, the log storage, and the dataset's segment id and original and final speaker columns. Then master is merged into develop, and then the feature goes in. The next revision is `0006`. `tests/unit/test_migrations.py` covers migrations.
- Old and new pods run side by side during a rollout. New code must read jobs whose history is still inline `correction_history`, and old pods' writes in that window must not corrupt the log or be lost to an undo (see "Concurrency" for the two releases and the version gap rule). A job whose results an old pod saves during the rollout has no stored copy, so it is legacy.
- Both repos have tests, and this work must extend them. transcribe-api uses pytest under `tests/unit/` (for example `recording/unit/api/test_routes.py`, `recording/unit/database/test_correction_dataset.py` and `recording/integration/test_dialogue_entries_persistence.py`). transcribe-web uses Vitest under `tests/recording/unit/` (the segment, speaker and merge-next route tests, `modification-history.test.ts` and `modification-history-table.test.tsx`) and Playwright under `tests/recording/e2e/` (`modification-history.spec.ts` and `speaker-rename.spec.ts`).
- The web calls the API only through Next.js API routes, which must pass through the version, the reset flags and the new 409 code.

Tests should cover at least the following:

- Each of the six kinds writes an entry with the user, and undoing it restores the exact previous state.
- A user can undo or reset another user's change.
- Undoing a rename-all restores every label.
- Undoing a combine gives back both original sections exactly as they were, with their ids.
- An undo or reset that would affect a dependent change is refused, and the message names the section.
- A section reset restores single renames, keeps a covering rename-all and rebases its previous label, and splits a combined section.
- An unrecorded write between two log entries stops undo at that point.
- A combine of two sections with different speakers produces training rows that keep each source's original speaker.
- Whole-transcription reset on a new job gives back the stored original AI output and an empty log. The web asks for confirmation first.
- On a legacy job, whole-transcription reset and undoing or resetting a legacy combine are refused with the reason, and the web hides those controls.
- Every dataset row matches the final transcript after any sequence of changes, undos and resets with the flag on, including rename-only sections. Nothing is written with the flag off.
- Two writes based on the same version: one succeeds and the other gets a 409.

## Left to the builder

- Storage shape: a new table or a JSONB log, the migration details, and how to carry existing `correction_history` entries into the log as legacy entries. Those entries have no user, include `rollback` entries that decision 6 does not allow, and on combined sections have lost their word ranges. They can be imported with an unknown user and the `rollback` entries dropped, or kept read-only where they are.
- Where the original AI output is stored, for example a JSONB column on `transcription_job`.
- Exact endpoint paths and request and response shapes.
- UI layout details, including the wording of the confirmation and the refusal messages.
- How the user is shown (email, name from the token, or a new column).
- Whether dataset rows are per section, per source segment, structured, or per word range, within the rules in "Training dataset".
