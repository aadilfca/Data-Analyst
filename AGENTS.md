# Codex Project Instructions

## Purpose

This file is the entry point for Codex when working in this repository. The application is an internal audit platform maintained by a Chartered Accountant who is not a software developer. Communicate decisions and tradeoffs in clear, plain language.

## Authoritative project guidance

Before investigating, planning, reviewing, or changing the project, read `CLAUDE.md` in full. Treat its current contents as the authoritative source for:

- application architecture and file ownership;
- development workflow and approval gates;
- coding conventions and known design decisions;
- phase boundaries and scope discipline;
- testing, documentation, and Git requirements;
- code-health and maintenance rules.

Also consult `File_Map.md` before identifying affected files. Read relevant sections of `CHANGELOG_ARCHIVE.md` when work touches an existing module, behavior, or previously documented pattern. Use `KNOWN_ISSUES.md` for code-health findings as directed by `CLAUDE.md`.

If this file conflicts with `CLAUDE.md`, stop and ask the user which instruction should prevail. Do not silently choose one or rewrite either file.

## Required workflow

For every brief, state which available model is appropriate for the task, considering complexity, risk, number of files, and required reasoning depth.

For any proposed code or documentation change:

1. Identify the candidate files, give a one-line reason for each, and wait for the user's confirmation before opening or editing those files.
2. After the file list is confirmed, present the approach for the current phase and wait for explicit approval before implementation.
3. Work on one approved phase at a time. Do not expand beyond the confirmed file list without stopping for approval.
4. Preserve existing working behavior unless the approved requirement explicitly changes it. Follow established patterns before introducing new abstractions.
5. Verify the change in proportion to its risk and report any checks that could not be run.
6. End the phase with the two-column `Change Required` / `Developed As` summary required by `CLAUDE.md`.
7. Wait for the user to test and confirm the phase. Only after that confirmation may Codex stage, commit, or push changes, using the approved commit message.

Read-only discussion, investigation, diagnosis, and review do not authorize implementation. When the user asks only for analysis, do not change files.

## Documentation ownership

Do not duplicate application history, architecture descriptions, conventions, or status entries in this file.

- Update `CLAUDE.md` as the single source of truth for architecture, conventions, workflow rules, and its concise Status log.
- Put detailed implementation and verification history in `CHANGELOG_ARCHIVE.md`.
- Update `File_Map.md` only when a file is added, removed, or repurposed, subject to its size guard.
- Record code-health findings in `KNOWN_ISSUES.md`, not in the Status log.
- Update `AGENTS.md` only when a Codex-specific instruction or the relationship between Codex and the authoritative project documents changes.

Before maintaining these files, apply all word-count, drift, scope, and self-check rules in `CLAUDE.md`. If required maintenance would touch a file outside the confirmed list, stop and obtain approval rather than silently expanding scope.

## Engineering discipline

- Prefer small, reversible changes that match the approved brief.
- Never add temporary production code, inline event handlers, speculative features, or unrelated cleanup.
- Preserve user changes and unrelated work already present in the worktree.
- Use existing shared utilities, adapters, API conventions, and UI patterns where applicable.
- Explain important technical choices in business-friendly language.
- Surface uncertainty, conflicts, migration risks, and destructive actions before proceeding.
- Never commit secrets, credentials, generated private data, or local environment files.

## Git discipline

Do not stage, commit, push, reset, discard, or rewrite history unless the user has authorized that exact step. A completed implementation is not permission to commit. Follow the post-testing Git sequence in `CLAUDE.md`.

## Instruction maintenance

Keep this file concise. Project-specific knowledge belongs in the existing authoritative documents, not in repeated Codex-only copies. When a reusable rule applies to both Claude Code and Codex, propose changing the authoritative rule in `CLAUDE.md` rather than duplicating it here.
