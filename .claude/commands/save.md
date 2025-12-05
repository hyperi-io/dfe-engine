# Save Session Progress

Checkpoint progress to STATE.md and clean up documentation. Run multiple times during a session or at end.

## Save Checklist

### 1. Update STATE.md

**Add current session:**

- What was accomplished
- Important decisions/changes
- Task completion status
- Blockers for next session

**Rationalise (remove stale, update changed):**

| Preserve | Safe to Remove |
|----------|----------------|
| Architecture decisions (why X over Y) | Sessions older than 2-3 |
| Current + previous 1-2 sessions | Deprecated implementation details |
| Long-term strategic goals | Duplicate information |
| Known blockers/issues | Temporary notes no longer relevant |
| Migration paths | Automated checklists |

**Update (don't remove):** Changed file paths, API examples, status markers, versions.

**When in doubt:** Ask before removing. Err on keeping information.

### 2. Clean TODO.md

- **REMOVE completed tasks** (they belong in STATE.md/CHANGELOG, not TODO)
- Add new tasks discovered during work
- Reprioritise remaining tasks
- TODO.md = active/pending work only

### 3. Check Git Status

- Uncommitted changes to commit?
- Any `.tmp/` files to clean up?

### 4. Review Test Status

- All tests pass?
- Skipped or failing tests to note?

### 5. Fix Markdown Linting

Fix issues in STATE.md, TODO.md:

- MD022: Blank lines around headings
- MD031: Blank lines around code blocks
- MD032: Blank lines around lists
- MD040: Language for code blocks

Keep fixes minimal.

### 6. Summary for Developer

- What was accomplished
- TODO items for next session
- Issues or blockers
- Confirm documentation updated

## Save Complete

Provide concise summary of what was saved.
