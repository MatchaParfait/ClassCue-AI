# Repository Maintenance

## Public Source And Private Material

The public repository contains buildable application source, tests, lockfiles,
required runtime assets with their licenses, and reviewed documentation.
Tests and examples should use synthetic or anonymized inputs.

Keep original classroom transcripts, generated experiment outputs, caches,
personal paths, raw design drafts, and work-in-progress archives outside public
commits. Required product images belong in the existing asset directories, not
in `outputs/` or `recovered-images/`.

Some production frontend modules import `experiments/subtitle-cyberspace/src/`.
Do not delete or ignore the entire `experiments/` directory. This source
repository includes only the required subtitle scene modules there, not
historical ASR reports, independent prototypes, or private run artifacts.
See [the experiment inventory](../experiments/README.md).

## Backups

Use a separate checkout and repository for private development archives.
An additional remote does not filter files out of the commits pushed to it.
Do not merge private archive history into public branches.

Before repository cleanup, preserve both reachable Git history and current
working files, including reviewed untracked files. A Git bundle does not
include uncommitted files; a source-file archive does not replace Git history.
Verify both backups and document their scope and exclusions.

Ignored databases, models, runtimes, and local credentials are not covered by
a tracked/nonignored source snapshot. Back them up separately where needed;
keep secrets and release-signing private keys in an appropriate encrypted
backup or secret manager, not in either Git repository.

Do not publish a development snapshot merely because its destination is
believed to be private. Confirm repository identity and visibility first.

## Before Public Commits

1. Separate repository cleanup, tooling changes, and product changes.
2. Stage explicit paths or selected hunks.
3. Review `git diff --cached --stat`, `git diff --cached`, and
   `git diff --cached --check`.
4. Run `python scripts/check_repository_hygiene.py` after staging.
5. Run locked backend tests, frontend tests, and the frontend production build
   as described in [CONTRIBUTING.md](../CONTRIBUTING.md).
6. Confirm that the actual destination branch and repository are public-safe.

The hygiene checker is a deterministic check of staged paths and selected
high-confidence credential formats. It is not a comprehensive secret scanner,
license audit, content anonymizer, or examination of every historical commit.
Review documentation, screenshots, and new assets manually.

Configure the public default branch to require successful backend and frontend
checks and reviewed pull requests. Workflow files alone do not enable branch
protection in repository settings.

## Previously Published Sensitive Material

Removing a file from the index with `git rm --cached` preserves its local copy
and prepares a deletion for the next commit. Adding `.gitignore` rules does not
untrack existing files. Neither operation removes old public commits.

If a real credential was exposed, revoke or rotate it before attempting history
cleanup. Inventory affected paths and refs using redacted findings, and preserve
a verified private backup before any authorized history rewrite.

Coordinate history changes with contributors and explicitly approve any force
push. Audit tags and other published refs as well as the default branch.
Rewriting the origin does not erase existing clones, forks, or copied files;
additional removal requests may be required. Never describe a local cleanup as
complete removal from the public Internet.
