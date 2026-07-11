# System Analyst App – AI Agent Rules

## Project Context
- **Tech Stack:** HTML + CSS + JS (Frontend), Python + FastAPI + SQLite (Backend)
- **User:** CA (Chartered Accountant) – non‑CS, but expert in accounting, analysis, and audit.
- **Goal:** Build a professional audit & verification application with modular, phase‑based development.

---

## File Access & Context – Auto‑Include All Files
- The agent has **full access to the entire project directory**.
- Before generating code, the agent should **read and understand** the relevant files for the task.
- When providing FIND/REPLACE chunks, the agent may modify **any file** in the project as needed.
- The agent should not ask the user to list files – it should automatically determine which files are affected based on the task.

---

## Preferred Code Format – FIND/REPLACE
All code changes must be provided as **copy‑pasteable FIND/REPLACE chunks**:

- **FIND:** Exact code block to locate (function name, unique comment, or variable assignment). Must be literal and immutable.
- **REPLACE WITH:** New code to replace it.
- **INSERT AFTER / INSERT BEFORE:** Code to insert after/before a specific block (show complete containing block).
- **REMOVE:** Code to delete without replacement.

**Rules:**
- Every chunk must declare affected files at the top: `📁 Files affected: filename.py, filename2.js`
- Never place `<script>` inside `<div>` or any HTML container.
- Never use inline event handlers (`onclick`, `onload`, `onchange`) inside HTML tags.
- Never mix HTML and JavaScript within the same block.
- Never use "temporary code" – all code must be production‑ready.
- All generated code must include precise, meaningful comments.


---

## Critical Constraints
- **100% functional code** – keep all other code exactly the same.
- If a change violates the "change only exact element" rule, state the conflict and propose a redesigned constraint.
- Use **proper separation of concerns** – HTML, CSS, JS, and Python should be in their respective files.
- **No temporary code** – everything must be final and ready for production.

---

## Files to Consider
- The agent should examine the entire project structure and determine which files are relevant.
- Typical backend files: `routers/*.py`, `database.py`, `main.py`
- Typical frontend files: `static/*.html`, `static/*.js`, `static/*.css`

---

## Communication Style
- Be clear, concise, and professional.
- Explain the purpose of each change before providing code.
- Always include a summary of effects and a phase status table after each chunk response.

---

## Project‑Specific Notes
- **Verification module:** Cashbook → cashbook page → verification sheet (with button).
- **Audit procedures:** Stored in `audit_procedure_library` table; may need a management UI later.
- **Performance:** Use `executemany` for bulk inserts; avoid pandas in real‑time flows.
- **Global navbar:** Handled by `client_context.js`; back arrow and AREA label are dynamic.