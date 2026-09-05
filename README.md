# grok-provider-model-sync

Syncs the `[model.*]` entries in a Grok `config.toml` with the provider catalogs published by [models.dev](https://models.dev). When a provider adds, renames, or removes models, this script reconciles your config so the Grok TUI always offers the current catalog.

## What it does

For every `[model_providers.<name>]` block in the config:

1. Resolves the matching models.dev provider — by exact `base_url` match first, falling back to a name match where `_` becomes `-`.
2. Compares your config's `[model.*]` entries that point at that provider (`model_provider = "<name>"`) against the registry's model list.
3. Plans the reconciliation:
   - **ADD** — registry (chat) models that are missing from the config, each with a fresh block containing `model_provider`, `context_window`, and `reasoning_efforts` when the registry lists named effort levels.
   - **KEEP** — entries that already exist.
   - **REMOVE** — entries whose model id is absent from the registry (skipped with `--keep-stale`).

The plan is printed as tables; nothing is written unless you pass `--apply`.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (or Python >= 3.9 and pip)
- The config file lives at `../config.toml` relative to this script (i.e. `~/.grok/config.toml`), or anywhere you like via `--config`.

## Usage

From the project directory:

```sh
uv run python sync_models.py               # dry run: print the plan, change nothing
uv run python sync_models.py --apply       # write the changes to the config
```

### Options

| Flag            | Effect                                                                  |
| --------------- | ----------------------------------------------------------------------- |
| `--apply`       | Write the planned changes. Default is a dry run.                        |
| `--keep-stale`  | Never remove config entries whose model id is absent from the registry. |
| `--include-all` | Also add non-chat models (image/video generators) from the registry.    |
| `--config PATH` | Config file to sync (default: `../config.toml`).                        |

## Example

Given this provider block in `config.toml`:

```toml
[model_providers.my_provider]
base_url = "https://my_provider.com/v1"
```

the script matches it against the models.dev provider with that base URL and creates one block per chat model, e.g.:

```toml
[model.deepseek-v4-flash]
model_provider = "my_provider"
context_window = 1000000

[[model.deepseek-v4-flash.reasoning_efforts]]
id = "high"
value = "high"
default = true
```

`reasoning_efforts` is only emitted when the registry lists named levels for that model. When no default is marked, a family-specific default is chosen:

| Family   | Default |
| -------- | ------- |
| qwen     | medium  |
| deepseek | high    |

otherwise the first listed effort.

## Safety guarantees

- The script **never reads environment variables and never authenticates** against your providers — models.dev is fetched over public HTTPS.
- On any failure to fetch or validate models.dev, it logs an error and exits without touching the config file.
- It refuses to continue if `[models].default` is scheduled for removal; re-run with `--keep-stale` to avoid removing it.
- Run without `--apply` first: the plan is informational, so you can review exactly what would change.

## Development

```sh
uv sync
uv run python sync_models.py
```

Dependencies: `prettytable`, `pydantic`, `requests`, `tomlkit` (see `pyproject.toml`).
