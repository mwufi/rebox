# Slice specs — how agents work

Each slice is built by one agent, in its own git worktree and branch, and merged into `main` by the architect after review.

## Rules for every slice

1. **Worktree:** `git -C /home/claude/rebox/codebase worktree add /home/claude/rebox/wt/<slice> -b <slice> main`. Work only inside that directory.
2. **Contracts are fixed.** Build against `docs/CONTRACTS.md` and the SQL in `db/`. If a contract is wrong or missing something you need, do the smallest reasonable thing, write it down under "Contract questions" in your report, and do not edit `docs/CONTRACTS.md` or `db/migrations/*` yourself.
3. **Stay in your paths.** Only create or edit files listed under "Owns" in your spec. Shared files you need (e.g. `.github/workflows`) are listed explicitly.
4. **Databases:** shared Postgres at `postgres://postgres@localhost:54329/postgres`. Create test databases with `scripts/test-db.sh <slice>_<name>` (prints owner URL, then app URL). Never touch databases with another slice's prefix.
5. **No secrets in git.** Use env vars from CONTRACTS §9.
6. **Commit** in small, logical commits on your branch with clear messages. Leave the worktree clean.
7. **Verify, don't claim.** Run every command in your spec's "Verification" section and paste the real output tail in your report.

## Report format (your final message)

```
SLICE: <id>   BRANCH: <branch>   HEAD: <sha>
STATUS: done | partial | blocked
WHAT I BUILT: 3–8 bullets
VERIFICATION: each command + pass/fail + key output lines
CONTRACT QUESTIONS: anything ambiguous or that you worked around
KNOWN GAPS: what is not done or not tested
```
