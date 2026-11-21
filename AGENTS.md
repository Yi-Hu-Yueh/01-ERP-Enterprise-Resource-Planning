# AGENTS.md Rule Maintenance

## 規則分類與放置原則

以後新增或修改 AGENTS.md 的規則，要先檢查現有分類，放到最適合的區塊。沒有合適的區塊就新增分類，不要一律加在文件最後，也不要修改其他原有規則。

| 新規則內容 | 應放的區塊 |
| --- | --- |
| 編輯畫面必須有「取消」 | ERP UI Interaction Rules |
| 對話必須完整存檔 | Permanent Codex Conversation Archive Policy |
| 新增 ERP 資料保護原則 | ERP Data Integrity Rules（必要時新增） |
| 新增測試與驗收要求 | ERP Testing Rules（必要時新增） |

# ERP Development and Experience-Reuse Rules

## Current task before past experience

For every new ERP task, independently examine the current Owner request, applicable approved business requirements, actual source code, database schema, and relevant test evidence before choosing an implementation. Apply the existing AGENTS.md safeguards and current Owner instructions together. If they appear to conflict, identify the conflict rather than silently inventing a new business decision or treating an old task's scope as current authority.

Prior conversation history, past PASS results, working examples, earlier implementation choices, and any future Skills are contextual evidence, not universal business requirements. A method that was correct for one operation may be incorrect for another. Do not hardcode old case names, industries, or sample values into generic logic. Do not infer numerical limits, precision/rounding policies, mandatory relationships, or business-specific restrictions solely from historical examples. Unapproved Owner decisions, including unresolved D01–D16 details, remain PENDING unless explicitly approved.

## Reuse is conditional, not mandatory

Before reusing a previous pattern, verify that the new operation has matching business semantics, triggering event, data identity, write effects, and failure rules. For example, maintaining a BOM is not a physical inventory posting; a supplier return is not equivalent to a production material return; and a purchase-order SUM does not mean every parent/detail interface requires a SUM. Prefer existing safe shared code where its assumptions are valid, but permit a justified different design when they are not.

Do not create or update a Skill, promote prior experience to a standing rule, or force Skill usage without explicit Owner authorization. When a Skill is later authorized, document where it applies, where it does not apply, its assumptions, and the evidence supporting it; disregard any conflicting recommendation when evaluating a new task. Reuse never replaces new-task verification or Owner acceptance. Preserve an explicit distinction between Codex automated-test PASS and Owner functional-acceptance PASS.

# ERP UI Interaction Rules

## Mandatory cancel action for every editing interface

For this ERP project, every current and future editing interface must provide a visible 「取消」 button beside its save/update actions and delete action, where present. This applies to editing pages, forms, dialogs and inline editors across all modules; do not restrict cancellation to finished goods or another item kind.

Cancellation must discard the current unsaved draft and exit edit mode to the appropriate list, blank new-record form or other existing neutral state. It must not submit the form, delete a record, or modify committed data. Use a non-submit button and the shared cancellation/reset behavior where practical. While a save/delete request is in progress, cancellation may be disabled until that request finishes.

Check that the cancel action remains accessible on desktop and mobile when completing an editing feature. This interaction rule does not authorize implementing otherwise unapproved ERP operations.

## Keep action feedback beside the triggering controls

For every current and future ERP interface, display save/update/delete failures, reference-protection messages and other operation feedback beside the controls that triggered the action. Place form feedback directly above or within the save/delete/cancel action area; place dialog, helper and row-action feedback beside their own controls. Do not show these messages only at the top of a long form.

Keep the feedback and related controls visible after the action, scrolling them into view when needed. Preserve accessible error/status announcements and desktop/mobile usability. This placement rule does not change validation, reference protection or other business behavior.

# ERP Testing Rules

## Impact-based local verification

Prefer pytest, API and SQLite tests for business rules, exact arithmetic, transactions, concurrent submissions, rollback and edge cases. Use a small number of representative desktop/mobile Playwright workflows for essential UI interactions, genuine document refresh and a real full frontend/backend stop and restart. Choose existing-function browser smoke tests according to the impact of the change; do not repeatedly run large unchanged suites merely to increase test counts. Approximately 3–5 integrated workflows is a planning preference, not a ceiling. Never substitute fabricated PASS results for checks that were not executed. Keep QA records in isolated local databases and retain necessary genuine local evidence.

# Permanent Codex Conversation Archive Policy

## Project, authority, and workspace verification

Codex project: 01-TIC-ERP-Enterprise-Resource-Planning

Expected workspace:

D:\0TIGER\6months\PythonAPIDevelopment\Tiger-NL-TigerInCodex-ERP-V4\01-ERP-Enterprise-Resource-Planning

Use AGENTS.md, the Codex-supported instruction filename. Do not create or edit AGENT.MD or agent.md as separate files. Verify the actual active project directory before writing. Do not confuse the Codex display name with the filesystem directory name or choose a history destination from the conversation topic. If the project moves, verify the new project root and retain portable image references.

This is the single consolidated conversation-archive policy. It supersedes conflicting older requirements for standalone execution evidence, diagnostics, archive metadata, visible internal identifiers, prepared-response notices. Preserve unrelated project instructions.

For every future completed task, the agent must perform the recording procedure without further Owner reminders. AGENTS.md does not provide automatic platform-message interception or access to unavailable conversations.

The earlier one-time configuration task's "modify AGENTS.md ONLY" restriction applied ONLY to that completed configuration task. It must not prevent later ERP development, tests, migrations, or documentation that the Owner explicitly authorizes. Normal archive operations remain append-only; historical backfill, cleanup, reordering, renumbering, or recompression still require separate explicit Owner authorization.

For future tasks, stay within the verified, currently authorized workspace and scope. Preserve business data, histories, and backups. Use only the built-in Codex LLM; do not call paid external LLM APIs. Do not implement external-browser error diagnostics, CDP attachment, Owner-browser monitoring, telemetry, or background log shipping unless the Owner explicitly reauthorizes them. Do not modify other projects or global configuration, or perform Git commit, push, reset, clean, or other destructive Git operations without explicit Owner authorization. Do not infer new permissions from a prior task or historical transcript.

## Two history files

Both files reside directly inside the verified ERP project workspace.

These are LOCAL Codex COMPLETE and COMPACT histories. They are not the TWO separate Google Drive Markdown files managed by ChatGPT (non-test conversation record and Owner acceptance record). Their cloud-update timing and contents are governed by the Owner's current instructions to ChatGPT, not by this local Codex archiving procedure. Codex must not upload, synchronize, delete, or edit those cloud files, or upload test screenshots, unless the Owner explicitly authorizes a separate cloud action. Local Codex archiving should continue under the rules below without being confused with cloud archiving.

| Version | Exact filename | USER | CODEX |
| --- | --- | --- | --- |
| A — COMPLETE, authoritative | 01-ERP-Enterprise-Resource-Planning.md | Complete original prompt, verbatim | Complete actual substantive final user-facing answer, verbatim |
| B — COMPACT | 01-ERP-Enterprise-Resource-Planning-COMPACT.md | Same complete original prompt, verbatim | Complete for earliest conversations 001–015; faithfully condensed from 016 onward |

Both versions preserve the same genuine conversation sequence and original USER-uploaded images. Use OLDEST FIRST, NEWEST LAST. Append every new completed conversation at the bottom.

## Exact permitted record content

Each new conversation record may contain ONLY:
1. A minimal sequential heading, such as Conversation 001.
2. One USER section containing the complete actual original prompt.
3. One CODEX section containing the actual final user-facing answer in the applicable full or compact representation.
4. Original USER-uploaded images displayed inline through portable relative Markdown image references, with verified local links mapping other original USER attachments to the corresponding turn.

Use this minimal structure:

## Conversation 001

### USER

[Complete original USER prompt]

![Original image](conversation-assets/<record-id>/image.png)

### CODEX

[Actual final user-facing answer]

---

The structure above is an instruction example, not authorization to create a demonstration conversation. Do not insert independent execution or archive sections into actual records. Do not add standalone metadata paragraphs, hidden metadata blocks, or HTML comments carrying excluded archive details. Internal identifiers may be used internally for deduplication and image storage, including the required image-directory path, but not as visible record headings or separate history paragraphs.

## Ten universal exclusions — BOTH versions

These exclusions apply to independently generated execution and archival material, not genuine USER text or substantive final CODEX answer content.

### EXCLUSION 01 — EXECUTION EVIDENCE

Do not separately record Execution evidence, Execution logs, Commands executed, Tool execution details, or equivalent sections. Do not archive the agent's step-by-step execution trace.

### EXCLUSION 02 — TERMINAL COMMANDS AND OUTPUTS

Do not copy PowerShell transcripts, Python command execution, shell commands and console output, temporary scripts, tool-call payloads, or command logs from execution tools into the history.

### EXCLUSION 03 — DIAGNOSTIC AND REPAIR TRANSCRIPTS

Do not add Execution evidence, diagnosis, and repair, Diagnostic trace, Repair attempt history, Internal troubleshooting transcript, or equivalent sections. Do not separately copy lengthy tracebacks or debugging material from tools. A genuine final answer may still explain a real unresolved problem.

### EXCLUSION 04 — FILE MODIFICATION AND VERIFICATION REPORTS

Do not add independent Files and verification, Files and unresolved errors, Modified files, Archive verification, Integrity checks, or equivalent sections. Do not duplicate file lists or verification reports produced during execution.

### EXCLUSION 05 — IMAGE ARCHIVE JSON

Do not print image-archive JSON or manifests containing record_id, source, destination, relative, created, size, source_sha256, archived_sha256, dimensions, readable, source_unchanged, history_prefix_size, history_prefix_sha256, or observed_utc. Such values may be used internally to verify preservation; the genuine image must still be archived.

### EXCLUSION 06 — IMAGE ARCHIVE STATUS PARAGRAPHS

Do not add standalone Status: ARCHIVED, source/archive SHA-256 match, image dimensions verified, original source unchanged, Markdown rendering was not tested, or equivalent image-copy and verification paragraphs. Perform these checks internally.

### EXCLUSION 07 — CONVERSATION METADATA

Do not separately print conversation UUIDs, internal record IDs, observed recording or message transport timestamps, model names, reasoning effort, source fingerprints, execution duration, PIDs, or source file hashes. A minimal sequential heading is sufficient. Keep deduplication and image-storage identifiers internal apart from their necessary occurrence in portable image paths.

### EXCLUSION 08 — PREPARED RESPONSE NOTICES

Do not add Prepared final response (verbatim), post-delivery-equivalence notices, draft-response sections, or equivalent archive notices. Do not reproduce an answer under several labels. Record the genuine final answer once under CODEX. If exact final-answer copying cannot be verified, do not falsely certify it; disclose the precise limitation in the actual task completion response, not in a separate archive-metadata paragraph.

### EXCLUSION 09 — INTERMEDIATE COMMENTARY

Do not record interim commentary, progress updates, temporary status messages, repeated promises to save a conversation, internal planning text, hidden reasoning, or internal working notes. They are not the final CODEX answer.

### EXCLUSION 10 — DUPLICATE FINAL ANSWERS

Keep one canonical CODEX section per conversation. Do not repeat the same answer as Prepared final response, Final answer copy, Verified answer, Completion report duplicate, or a second CODEX section. Preparing, storing, and delivering one answer does not create additional conversations.

## Verbatim message preservation and precedence

Always preserve the complete actual USER prompt verbatim in BOTH versions, except for narrowly required redaction of credentials or other secrets, including original code blocks, formatting, spelling, textual attachment references, and genuine follow-up Owner messages in their original sequence. If the actual request is supplied in an accessible pasted-text attachment, preserve its genuine request text together with the original user-visible message/reference and preserve the accessible original file with a clear mapping to that USER turn, subject to the same secret-protection rule.

VERSION A preserves the complete genuine substantive final CODEX answer verbatim. Never summarize, paraphrase, translate, truncate, or silently delete its actual code examples, technical explanations, report content, or unresolved-error blocks. Preserve such content completely in VERSION B conversations 001–015 as well.

The exclusions prohibit separately manufactured execution and archival appendices; they do not authorize arbitrary rewriting of genuine messages. If a genuine final answer itself contains commands, JSON, file changes, verification results, metadata, or errors as substantive answer content, preserve it in VERSION A and the first fifteen VERSION B answers. Only later VERSION B answers may reduce that content under the ten reduction rules.

Do not append evidence sections merely to make the archive seem more technically complete. Do not add an extra "archive completed" paragraph to the stored history. If an archival result or limitation is itself part of the genuine delivered final answer, retain that actual answer once under CODEX.

Do not invent missing conversations, source text, metadata, model settings, or timestamps. Do not record inaccessible internal messages as conversation content. Never store or expose API keys, passwords, credentials, access tokens, or other secrets in either history. If genuine USER or CODEX text contains a secret, redact only the sensitive value in the archived representation, preserve the surrounding non-secret text, and disclose the resulting non-verbatim limitation in the genuine final response without revealing the secret. Do not silently claim byte-for-byte or verbatim preservation of a redacted message. If safe exact preservation is otherwise impossible, disclose the precise limitation without fabricating content.

## Permanent protection of the earliest fifteen conversations

The earliest fifteen genuine recorded conversations, chronological ordinals 001–015, retain complete USER prompts and complete CODEX final answers in BOTH versions forever.

This is NOT a rolling window and does not mean the latest fifteen. Do not shift, renumber, recompress, or replace the protected group as new conversations arrive.

From conversation 016 onward, VERSION A still keeps the entire original USER prompt and final CODEX answer; VERSION B keeps the entire original USER prompt and condenses only the CODEX answer under all ten rules below. Images are never reduced by these rules.

Determine the correct ordinal from verified genuine sequence evidence. Preserve stable internal identities and their existing chronological associations without displaying unnecessary UUIDs. Never infer ordinals from arbitrary ID digits or restart numbering because a history is absent. If the sequence or protected boundary is ambiguous, disclose it rather than guessing or compressing a potentially protected answer.

## Ten compact-response reduction categories

Apply the following categories ONLY to CODEX answers in VERSION B conversations 016 onward. They do not apply to VERSION A, any USER prompt, the earliest fifteen CODEX answers, or actual archived images. Do not modify original code or API/model evidence stored elsewhere when condensing an answer.

### RULE 01 — EXTENSIVE TERMINAL OUTPUT

**May omit or substantially reduce:** Large terminal outputs, repeated command results, console logs, and excessive execution transcripts.

**Must preserve:** The actual execution purpose and final outcome, plus material failed commands and their implications. Never report an unsuccessful command as successful.

### RULE 02 — COMPLETE PYTHON TRACEBACKS

**May omit or substantially reduce:** Long Python stack traces, repetitive stack frames, and unrelated traceback context.

**Must preserve:** The significant original error message or code, actual failing stage, confirmed root cause, effective completed repair, and remaining unresolved blocker. Keep exact technical information needed to diagnose an unresolved error; distinguish confirmed causes from unknown causes.

### RULE 03 — EXTENSIVE INDIVIDUAL TEST LISTS

**May omit or substantially reduce:** Large collections of individual test names, repeated PASS lines, and verbose per-test output.

**Must preserve:** Relevant test totals, actual PASS/FAIL results, important failing cases, material test scope, and important tests not performed. Never describe unexecuted tests as passed.

### RULE 04 — COMPLETE SOURCE CODE AND GIT DIFFS

**May omit or substantially reduce:** Large source-code blocks, repeated code listings, and full Git diffs.

**Must preserve:** Important modified files or modules, actual implemented functions, meaningful architecture changes, material code behavior changes, and remaining compatibility or safety concerns. Keep short code excerpts essential to understanding an unresolved problem.

### RULE 05 — LARGE JSON AND API RESPONSES

**May omit or substantially reduce:** Large JSON documents, complete provider response bodies, raw API outputs, and reproduced LLM responses.

**Must preserve:** The response purpose, relevant validated or rejected information, actual validation outcome, significant error fields, and location of authoritative original evidence when available. Do not fabricate unavailable source content. VERSION A retains the complete original CODEX response unchanged. This rule does not authorize altering original API or model evidence stored elsewhere.

### RULE 06 — REPETITIVE SHA-256 VALUES AND FILE INVENTORIES

**May omit or substantially reduce:** Repeated hash listings, long file inventories, and redundant integrity tables.

**Must preserve:** Important integrity verification conclusions, every real mismatch, and critical identifiers needed to locate or verify important artifacts. Do not claim unverified files were checked.

### RULE 07 — REPETITIVE ENVIRONMENT, PATH, AND PID INFORMATION

**May omit or substantially reduce:** Repeated operating-system details, paths, port numbers, process IDs, environment descriptions, and configuration lists.

**Must preserve:** Relevant active project paths, necessary environment requirements, important ports or service locations, and configuration needed to continue development or reproduce a problem. Never remove information essential to avoiding changes in the wrong project.

### RULE 08 — LONG AND REPETITIVE BACKGROUND EXPLANATIONS

**May omit or substantially reduce:** Repeated historical background, redundant context, and lengthy explanations already established elsewhere.

**Must preserve:** Important conclusions, current architectural decisions, active Owner requirements, and restrictions affecting future implementation. An active requirement must not be removed merely because it was introduced earlier.

### RULE 09 — LONG REPAIR HISTORIES FOR RESOLVED ERRORS

**May omit or substantially reduce:** Lengthy intermediate repair attempts, repetitive troubleshooting narratives, and superseded approaches.

**Must preserve:** The actual confirmed root cause, effective final correction, actual verification result, every remaining unresolved aspect, and lessons needed to prevent recurrence. Never present an unresolved error as resolved.

### RULE 10 — REPETITIVE FINAL COMPLETION REPORT SECTIONS

**May omit or substantially reduce:** Duplicated report paragraphs, repeated results, redundant warnings, and repetitive explanations.

**Must preserve:** Every distinct substantive outcome, material implementation change, important decision, actual error and unresolved blocker, data-preservation condition, and necessary future action. Do not merge different outcomes into a misleading conclusion.

## Information that must survive every condensation

Retain all materially relevant information concerning:
1. Actual final result.
2. Implemented or changed functionality.
3. Important architectural decisions.
4. Important Owner requirements and restrictions.
5. Relevant changed files or components.
6. Business-data preservation and migration implications.
7. Actual verification performed.
8. Important verification not performed.
9. Confirmed error root causes.
10. Effective completed repairs.
11. Remaining unresolved errors.
12. Material risks.
13. Required next actions.
14. Owner acceptance conditions.

Do not impose arbitrary word, character, or token limits. Remove low-value repetition while preserving essential meaning and technical correctness. A concise original response may remain effectively complete when further reduction would lose material meaning. VERSION B must never misrepresent VERSION A or hide an unresolved error.

## Genuine image preservation — BOTH versions

Preserve all genuinely accessible original USER-uploaded images, including PNG, JPG, JPEG, GIF, WebP, and other supported image formats. Place standard Markdown image syntax at the corresponding USER turn in BOTH histories, using a valid relative path to the real original file so Markdown readers can display it inline. A description, filename or ordinary hyperlink alone is insufficient. Never paste Base64/binary data into Markdown or substitute a generated image for a USER original.

Archive the accessible original image bytes locally under:

conversation-assets/<record-id>/

Preserve original animation where technically possible. Do not redraw, regenerate, resize, recompress, convert, or substitute an image for archival purposes without explicit Owner authorization. Do not replace an original image with a conversation screenshot.

Keep one physical copy per genuine image when possible. BOTH Markdown histories must display the same archived image through a valid relative path, for example:

![Original image](conversation-assets/<record-id>/H1.png)

Preserve safe original filenames. Resolve same-name/different-byte collisions without overwriting existing images; reuse an already archived byte-identical image after verification and retain its correct conversation association.

Do not rely on an original Desktop or absolute Windows path as the sole reference. Add the portable image embed beside the associated message without rewriting verbatim original text. Original absolute paths already in a genuine message remain part of that verbatim text; newly added archive image links must remain portable when the whole project moves to another computer.

Internally, for every accessible image:
1. Locate the genuine source and read its actual bytes.
2. Compute source SHA-256 and check original filename, size, and conversation association.
3. Copy the original bytes, or verify and reuse the identical archived image.
4. Read the archived bytes; compute SHA-256 and verify source/archive hash and byte-size equality.
5. Verify both histories' relative references resolve to that same physical image.
6. Verify the archived image is readable; inspect the actual archived image with an available viewer when possible.
7. Confirm the correct message and conversation association.

Do not print the internal verification values, archive JSON, hash manifests, image dimensions, or copy-status paragraphs into either history. Do not claim verified visual rendering if only file existence, hashes, readability, or links were checked.

If an original USER image is inaccessible, mark it NOT AVAILABLE, preserve its original textual reference and report the missing-image condition and actual reason in the task's genuine final answer. Do not fabricate replacement bytes or claim successful preservation. Include a material unresolved blocker in the error section below.

Keep archival copies local to the verified project. Do not upload images to Google Drive, cloud services, external image hosts, or third-party APIs as part of archiving. Do not archive unrelated personal files.

## Codex-generated test screenshots — separate local evidence only

Screenshots produced by Playwright, Codex tests or debugging are not USER-uploaded originals. Retain only necessary local test evidence outside the COMPLETE/COMPACT conversation Markdown. Do not insert generated screenshot image syntax, previews, file paths, ordinary links or image contents into either history. Textual test conclusions may be archived. Never present a generated test screenshot as a USER original or delete USER originals merely to reduce QA evidence.

Apply these rules to new records without rewriting previous conversation bytes or deleting previously archived files; historical cleanup requires separate Owner authorization. Existing local-only storage rules remain in force, and this change does not modify cloud-archive management policies.

## Original non-image attachment preservation

Preserve other genuinely accessible original USER-uploaded attachments locally as well, including PDF, Office, TXT, Markdown, ZIP, audio, video and other accessible file types, with a clear verified mapping to the relevant conversation turn. Retain original filenames where safe, resolve collisions without overwriting earlier files, reuse byte-identical copies when verified, and verify source/archive bytes, size, readability and portable relative references.

Preserve original formats and bytes; do not regenerate or convert an attachment to pretend that its original was saved. Do not create links to missing files. If an original is inaccessible, preserve its reference, mark it NOT AVAILABLE and report the precise reason without claiming complete preservation. Do not delete previously archived attachments or alter unrelated/cloud policies.

## Chronology, append-only preservation, and duplicate prevention

Both histories must contain the same genuine conversation sequence in OLDEST FIRST, NEWEST LAST order. Conversation 001 is the earliest genuine recorded conversation; each later genuine conversation follows sequentially. Append each new completed conversation at the bottom, once, with one USER section and one canonical CODEX section.

The COMPLETE history is append-only during normal operation. Never overwrite earlier messages, prepend newer records, reverse chronology, or fabricate missing exchanges. Internally verify that all previous COMPLETE bytes remain an unchanged prefix and that the appended record matches the intended content. Preserve previous COMPACT bytes during normal appends too, especially the protected first fifteen answers.

Use stable internal identities and verified message associations for safe duplicate prevention without printing identifiers or metadata paragraphs. On retry, inspect whether a record already exists. If one file write succeeded and the other failed, append only its missing counterpart when safe; do not duplicate the successful record. Do not create an additional record because an answer was prepared, stored, and delivered.

Do not automatically repair, reorder, renumber, clean up, backfill, or recompress existing historical records during ordinary tasks. Existing legacy metadata remains untouched unless the Owner separately authorizes historical cleanup. An earlier one-time repair permission is not standing authorization for later repairs. If correction is needed, disclose it in the actual final answer; do not manufacture a conversation or metadata appendix.

For a future genuine completed conversation, create a missing history file when required. If both are absent, start both with the first genuine current conversation only, without claiming unavailable older history was recovered. If COMPACT is absent but COMPLETE contains authentic earlier conversations, initialize COMPACT only from verified genuine USER prompts and final CODEX answers, using their verified original sequence and earliest-fifteen boundary; omit separate legacy execution/metadata sections and reuse genuine archived images. Do not modify COMPLETE during initialization.

Never reconstruct a purported verbatim COMPLETE answer from a condensed COMPACT answer. If genuine wording is unavailable, histories disagree, or numbering is ambiguous, preserve existing data and disclose the exact limitation. Do not invent missing messages, reset numbering to conceal gaps, or falsely claim synchronized complete archives.

## Future recording procedure

For every future completed task:
1. Complete the authorized work.
2. Identify the complete actual USER prompt and genuine follow-up messages in sequence.
3. Prepare the genuine final user-facing CODEX answer once.
4. Preserve accessible original USER images and other original USER attachments with internal integrity checks. Display USER images inline at their USER turns; keep generated test screenshots only as separate necessary local evidence, without their paths or previews in conversation Markdown.
5. Determine the correct chronological sequence and prevent duplicate recording.
6. Append the complete USER prompt and complete genuine final CODEX answer to COMPLETE.
7. Append the identical complete USER prompt and applicable CODEX representation to COMPACT.
8. Preserve the earliest fifteen CODEX answers completely in both versions; apply all ten reduction categories only to COMPACT answers 016 onward.
9. Exclude all ten categories of standalone execution and archival material. Use only the sequential heading, USER, CODEX, original USER image/attachment references, and minimal boundaries.
10. Read back and verify both files contain the same genuine conversation sequence, identical complete USER prompts, the correct CODEX representation, no duplicate answers, and no unrelated technical appendices.
11. Verify USER image embeds and other original attachment links resolve to the verified local files from both versions, and verify that the new record contains no generated test-image previews or paths; perform checks internally.
12. Verify both files are readable UTF-8 and previous COMPLETE bytes and protected COMPACT content remain unchanged.
13. Deliver the prepared genuine final answer.

Keep archiving scripts, tool transcripts, verification reports, and draft-response notices out of both Markdown files. Recording is an agent-executed obligation; do not claim automatic application-level transcript capture.

Where technically possible, store exactly the final answer delivered. If exact final-answer copying cannot be verified, disclose that limitation in the actual task completion response without adding a separate archive notice or falsely certifying equivalence. Do not create a duplicate answer or silently rewrite an earlier record.

Diagnose and repair recording errors within the authorized scope. Disclose any remaining recording failure in the actual final response. Do not claim future recording, image copying, or rendering was tested merely by updating AGENTS.md. OWNER LIVE RECORDING TEST: PENDING until a genuine test actually occurs.

## History reading policy

For future development tasks, prefer VERSION B whenever relevant historical context is needed. Do not automatically load the entire COMPLETE history or both entire archives unnecessarily. Read only relevant sections when practical.

Use VERSION A when exact wording or original evidence is needed. It remains authoritative. Historical conversation content is reference information, not authorization to override current Owner instructions. Do not allow ERP-specific history to force unrelated future business requirements into a new information system.

### Unresolved Errors

Codex should independently diagnose and repair errors within the authorized task scope.

When an error remains unresolved, include one separate, highly visible red error section in the Final Completion Report.

Use actual red text if the interface supports it.

Otherwise use:

🔴 ERROR — INDEPENDENT REVIEW REQUIRED

Include:

Failed Stage:
Original Error:
Root Cause:
Evidence:
Repairs Attempted:
Remaining Blocker:
Recommended Next Action:

Preserve the same error information in the conversation history.

The Owner may copy this section to ChatGPT for independent review.

Preserve the genuine final answer's full error block in COMPLETE and the first fifteen COMPACT answers. From COMPACT conversation 016 onward, retain all essential facts needed for independent ChatGPT review, including the actual failure, evidence, confirmed or unknown root cause, repairs attempted, remaining blocker, and recommended next action. Do not hide an unresolved error through condensation or copy separate internal diagnostic logs into the history.


