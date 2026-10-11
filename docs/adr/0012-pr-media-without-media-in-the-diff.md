# ADR-0012: Screenshots and a demo in the change request, never in its diff

**Status:** accepted · **Date:** 2026-10-11 · Refines [ADR-0010](0010-external-integration-records.md) and [ADR-0011](0011-no-design-change-requests.md).

## Context
A reviewer of a UI change should see it working: a couple of screenshots and a short demo at the top of the pull request ([post-mortem 2026-10-11](../postmortems/2026-10-11-evergreen-frontier-models-pr.md) §4). The browser evidence is in Factory's artifact store, which only the owner's Console serves. Media committed to the change's branch would land in its diff and on the default branch, which is the failure this post-mortem is about. ADR-0010 and ADR-0011 keep factory bookkeeping out of target repositories.

What renders inline in a private repository's pull request, outside the diff:
- **GitHub** renders an image referenced by `https://github.com/<owner>/<repo>/blob/<ref>/<path>?raw=true` for anyone who can read the repository. It plays video inline only for files uploaded as its own attachments (`user-attachments`), and no API uploads those. A GIF is an image, so it renders and animates inline.
- **GitLab** has a project uploads API (`POST /projects/:id/uploads`). Uploads belong to the project rather than a branch, and a merge request description renders uploaded images and plays uploaded MP4 or WebM inline.

## Decision
1. `Forge.publish_media(folder, items)` publishes at most three screenshots and one demo and returns Markdown for the description's "See it" section (`orchestrator/factory/pr_media.py` chooses them; `stages/integrate.py` calls it).
2. **GitHub:** the files are committed under `<run_id>/<spec_id>/<head>/` on `factory-evidence`, an orphan branch created on first use. It shares no history with the code, is never merged, and is never the base or head of a change request. Images and a GIF demo render inline. A video demo is a link, the honest choice, because GitHub will not play it. The factory's browser runner makes a GIF of the recording when ffmpeg is present, so the usual demo plays inline.
3. **GitLab:** the files go through the uploads API. Nothing touches a branch.
4. **What may be published:** only media from browser checks the factory's own runner ran (a repository's visual command or a connected browser does not check the screen for credentials) that passed on the head the merge judge approved, observed at the requested viewport, validated by `visual.collect`, re-read from the store at their recorded fingerprint, with captions free of credential shapes. Nothing at all is published if a test credential was ever on screen during this change's checks.
5. `FACTORY_PR_MEDIA=off` turns it off. A failure to publish leaves the section out, is recorded in the run record, and never blocks the change request.

## Consequences
- This is a scoped exception to ADR-0011's "nothing from the factory in the target repository", made at Gate 3 and only on GitHub. The evidence sits in the repository's git storage and branch list, but in no diff, no default branch and no change request. The alternatives were an image host the owner would have to run, or no inline media. The branch says what it is in its README and is safe to delete; deleting it breaks the images in old descriptions and nothing else.
- Anyone who can read the repository can read the evidence. It holds only what the checks show with synthetic data, and never anything recorded while a credential was visible.
- The run record keeps every screenshot and recording. The description shows at most four files.
