# Repository contribution guidelines

For user-authorized deployment or messaging-platform setup, also read [Agent administration guidance](docs/agents/AGENTS.md). These contribution rules continue to apply to repository changes.

## Language

- Write commit subjects and bodies, PR titles and descriptions, and code review comments in English.
- Keep user-facing text and translated documentation in their intended language.

## Commits

- Use Conventional Commit subjects: `<type>[optional scope]: <summary>`.
- Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `ci`, `build`, and `chore`.
- Use a concise, imperative summary describing the actual change; avoid vague subjects such as "update" or "fix stuff".
- Keep each commit focused on one coherent change. Explain non-obvious reasons and compatibility considerations in the body when needed.
- Never include credentials, personal tokens, private task details, or generated test artifacts in commits.
- Do not rewrite shared history unless explicitly authorized.

## Pull requests

- Continue targeting `release/2.x` until the 3.0.0 development and acceptance work is complete. Then align `main` to that exact accepted commit and make it the default branch; subsequent development targets `main`. Preserve the previous `main` history in an archive branch before alignment, and do not rename `release/2.x` as part of this transition.
- Use a Conventional Commit style title suitable for the final squash commit.
- Describe the concrete problem and resulting behavior, followed by relevant validation.
- Scale the description to the change. Small changes need only a few sentences; larger changes should explain material compatibility, migration, or rollout considerations.
- Distinguish completed checks from pending checks and state meaningful limitations. Do not claim deployment or client end-to-end verification from unit tests alone.
- Keep the title and description aligned with the final diff. Exclude unrelated changes and conversational history.
- Wait for all required checks on the current PR head and satisfy branch protection before merging. Do not bypass checks or use a stale successful run.
- Prefer squash merging so that the final commit represents the complete reviewed change. Merge, tag, release, and deploy only within the user's authorized scope.

## Examples

- Commit or PR title: `fix(queue): preserve booking order when releasing a device`
- Commit or PR title: `docs(agent): clarify public task summaries and skill updates`
- PR description: "Agents could publish internal task details in shared resource notes. Document how to retain useful task context while omitting sensitive details. Validation: Skill copies match and 25 discovery and download tests pass."
