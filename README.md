# FurtherAI Skills

This repository stores, validates, and packages skills for FurtherAI agents. A
skill is a folder of instructions and optional supporting files that an agent
uses to perform a task.

The first skill, [document-extraction](skills/document-extraction/SKILL.md),
provides instructions for extracting structured fields from insurance documents.

## Getting started

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
from the repository root:

```sh
uv sync --locked
uv run python scripts/skill_bundle.py skills/document-extraction --output dist/document-extraction.zip
```

This validates the skill and writes `dist/document-extraction.zip`. Omit
`--output` and its path to validate without creating a ZIP. The `dist/` directory
is gitignored.

The ZIP contains `SKILL.md` at its root and preserves resource paths, file contents,
and executable flags. Unchanged inputs produce identical ZIPs.

## Where bundles go

On pull requests and pushes to `main`, CI runs tests, checks types, and packages
`document-extraction`. Open a completed run in
[Actions](https://github.com/Further-AI/furtherai-skills/actions/workflows/validate.yml)
and download `document-extraction` under **Artifacts** to get the ZIP.

When publishing is enabled, a successful run on the current `main` commit also:

1. Authenticates to the backend with GitHub OIDC.
2. Publishes the validated ZIP and its repository/commit provenance to US staging.
   The backend stores the immutable bundle in Azure's private `skill-bundles` container.
3. Promotes the returned content digest to `stable`.

Failed checks or uploads block promotion. Releases run one at a time; stale commits
and conflicting promotions fail. Existing workflow runs keep their pinned versions.
These checks validate packaging; they do not evaluate extraction quality.

## Enable staging publishing

After the backend publishing API is deployed:

- Create a GitHub environment named `us-staging`, restricted to `main`.
- In that environment, set `SKILLS_API_URL` to the backend's HTTPS base URL.
- Configure the backend's `SKILLS_PUBLISH_AUDIENCE` as `furtherai-skills-us-staging`
  and its FurtherAI ownership and Azure storage settings.
- Set the **repository variable** `SKILLS_PUBLISH_ENABLED` to `true`.

Run **Validate and publish** manually on the current `main` commit to publish the
pilot. Later merges publish automatically. No Azure credentials or long-lived
publishing token are needed in this repository. A promotion conflict stops the run;
check the competing release before rerunning the current `main` commit.

## Adding a skill

Each skill follows the [Agent Skills format](https://agentskills.io/specification):
create `skills/<name>/SKILL.md` with YAML `name` and `description` between `---`
delimiters, followed by the instructions. The name must match the folder.
Use the existing skill as an example.

Optional `scripts/`, `references/`, `assets/`, and other resource files are included
recursively. Only the skill's root `tests/` directory is excluded. Run the same
packaging command with your skill's path; CI currently targets the first skill.

Packaging rejects symlinks, special files, and unsafe paths. Limits per skill:

| Item | Maximum |
| --- | --- |
| Individual file | 10 MiB |
| Total file contents and final ZIP (each) | 30 MiB |
| Included files | 1,024 |
| YAML frontmatter | 64 KiB |
| Name / description / compatibility | 64 / 1,024 / 500 characters |

File restrictions, size caps, and file-count limits match FurtherAI's backend
([bundle validation](https://github.com/Further-AI/fai-automation-backend/blob/769a1f66a1cd87ddef9cbb7020ed79462d0966ce/src/backend/skills/storage.py),
[frontmatter validation](https://github.com/Further-AI/fai-automation-backend/blob/769a1f66a1cd87ddef9cbb7020ed79462d0966ce/src/backend/skills/manifest.py)).
Metadata field lengths follow the [Agent Skills specification](https://agentskills.io/specification#frontmatter).

## Development

Packaging lives in [scripts/skill_bundle.py](scripts/skill_bundle.py); publishing
lives in [scripts/publish_skill.py](scripts/publish_skill.py). Tests are in `tests/`.

```sh
uv run pytest -x --tb=short
uv run ty check scripts tests
```
