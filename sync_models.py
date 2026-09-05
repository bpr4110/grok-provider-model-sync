"""Sync [model.*] entries in a Grok config.toml with models.dev provider catalogs.

For each [model_providers.<name>] block in the config, the script resolves the
matching models.dev provider (by exact base_url match, falling back to a
name match with "_" -> "-") and reconciles the config's [model.*] entries that
point at that provider against the registry's model list.

It never reads environment variables and never authenticates against the
provider itself; models.dev is fetched over public HTTPS.

Usage:
  uv run python sync_models.py              # dry run: log plan, change nothing
  uv run python sync_models.py --apply      # write the changes to the config
  uv run python sync_models.py --keep-stale # never remove config entries
  uv run python sync_models.py --include-all # also add image/video models
  uv run python sync_models.py --config PATH # config file (default: ../config.toml)

On any failure to fetch or validate models.dev the script logs an error and
exits without touching the config file.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import requests
import tomlkit
from pydantic import BaseModel, Field, RootModel
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from prettytable import PrettyTable

REGISTRY_URL: str = "https://models.dev/api.json"
REQUEST_TIMEOUT_SEC: int = 60
DEFAULT_CONFIG: Path = Path(__file__).resolve().parent.parent / "config.toml"

logger: logging.Logger = logging.getLogger(__name__)


class Limit(BaseModel):
    context: int | None = None
    output: int | None = None


class ReasoningOption(BaseModel):
    type: str
    values: list[str | None] = Field(default_factory=list)


class RegistryModel(BaseModel):
    """One entry in a provider's \"models\" map."""

    id: str
    family: str | None = None
    limit: Limit | None = None
    reasoning_options: list[ReasoningOption] = Field(default_factory=list)


class Provider(BaseModel):
    api: str | None = None
    models: dict[str, RegistryModel] = Field(default_factory=dict)


class Registry(RootModel[dict[str, Provider]]):
    """models.dev /api.json schema: provider id -> provider catalog."""


def fetch_registry() -> Registry:
    try:
        resp = requests.get(REGISTRY_URL, timeout=REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        return Registry.model_validate(resp.json())
    except Exception as exc:  # noqa: BLE001 - report any failure and stop
        logger.error(
            "could not fetch %s (%s: %s); no changes made",
            REGISTRY_URL,
            type(exc).__name__,
            exc,
        )
        sys.exit(1)


def resolve_provider(
    registry: Registry, name: str, base_url: str
) -> tuple[str, Provider] | None:
    """Return (provider_id, provider_entry), or None if there is no match."""
    if base_url:
        norm_url = base_url.rstrip("/")
        for prov_id, prov in registry.root.items():
            if (prov.api or "").rstrip("/") == norm_url:
                return prov_id, prov
    norm_name = name.replace("_", "-")
    prov = registry.root.get(norm_name)
    if prov is not None:
        return norm_name, prov
    return None


def effort_values(model: RegistryModel) -> list[str]:
    """Named reasoning efforts (type == 'effort'), in registry order."""
    return [str(v) for opt in model.reasoning_options for v in opt.values if opt.type == "effort"]


def is_chat_model(model: RegistryModel) -> bool:
    output = model.limit.output if model.limit else None
    return output is not None and output > 0


# The registry does not mark a default, so we define family-specific defaults here.
FAMILY_DEFAULT_EFFORT: dict[str, str] = {"qwen": "medium", "deepseek": "high"}


def default_effort_label(efforts: list[str], family: str | None) -> str:
    preferred = FAMILY_DEFAULT_EFFORT.get(family or "")
    if preferred and preferred in efforts:
        return preferred
    return efforts[0]


def build_model_block(model: RegistryModel, provider_name: str) -> Table:
    """A fresh [model.<id>] table: provider ref, context window, efforts."""
    tbl = tomlkit.table()
    tbl["model_provider"] = provider_name
    ctx = model.limit.context if model.limit else None
    if ctx is not None and ctx > 0:
        tbl["context_window"] = ctx
    efforts = effort_values(model)
    if efforts:
        default_label = default_effort_label(efforts, model.family)
        aot = tomlkit.aot()
        for label in efforts:
            et = tomlkit.table()
            et["id"] = label
            et["value"] = label
            et["default"] = label == default_label
            aot.append(et)
        tbl["reasoning_efforts"] = aot
    return tbl


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s][%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument(
        "--keep-stale",
        action="store_true",
        help="keep config entries whose model id is absent from the registry",
    )
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="also add non-chat (image/video generator) registry models",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to the config TOML")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        logger.error("config file not found: %s", config_path)
        sys.exit(1)

    doc: TOMLDocument = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    registry: Registry = fetch_registry()

    providers: dict[str, Any] | None = doc.get("model_providers")
    model_tbl: dict[str, Any] = doc.get("model", {})
    models_glob: dict[str, Any] | None = doc.get("models")

    if providers is None:
        logger.error("config has no [model_providers] section")
        sys.exit(1)

    add_plans: list[tuple[str, str, RegistryModel, str]] = []
    remove_plans: list[str] = []
    keep: list[str] = []
    errors: list[str] = []

    for prov_name, prov in providers.items():
        if not isinstance(prov, dict):
            continue
        base_url = str(prov.get("base_url", "") or "")
        resolved = resolve_provider(registry, prov_name, base_url)
        if resolved is None:
            errors.append(
                f"provider '{prov_name}': no models.dev provider matches "
                f"base_url {base_url!r} or name {prov_name!r}"
            )
            continue
        prov_id, prov = resolved
        models = prov.models
        current: dict[str, Any] = {
            mid: tbl
            for mid, tbl in model_tbl.items()
            if isinstance(tbl, dict) and tbl.get("model_provider") == prov_name
        }
        logger.info(
            "provider '%s' -> models.dev '%s' (%d registry models; %d chat-eligible)",
            prov_name,
            prov_id,
            len(models),
            sum(is_chat_model(m) for m in models.values()),
        )
        for model_id, model in models.items():
            if not args.include_all and not is_chat_model(model):
                continue
            if model_id in current:
                keep.append(model_id)
            else:
                add_plans.append((prov_id, model_id, model, prov_name))
        if not args.keep_stale:
            for model_id in current:
                if model_id not in models:
                    remove_plans.append(model_id)

    if errors:
        for e in errors:
            logger.error(e)
        logger.error("no changes made")
        sys.exit(1)

    default = (models_glob or {}).get("default")
    if default in remove_plans:
        logger.error(
            "[models].default = %r is scheduled for removal; "
            "refusing to continue. Use --keep-stale to avoid removing it.",
            default,
        )
        sys.exit(1)

    add_plans = sorted(add_plans, key=lambda x: x[1])
    keep = sorted(keep)
    remove_plans = sorted(remove_plans)

    plan_lines = [f"SYNC PLAN for {config_path}" + (" (stale kept)" if args.keep_stale else "")]
    if add_plans:
        plan_lines.append(f"ADD ({len(add_plans)}):")
        table = PrettyTable(["Model ID", "Context", "Efforts", "Provider"], align="l")
        for prov_id, model_id, model, prov_name in add_plans:
            ctx = model.limit.context if model.limit else None
            efforts = ", ".join(effort_values(model)) or "-"
            table.add_row([model_id, ctx, efforts, prov_name])
        plan_lines.append(table.get_string())
    if keep:
        plan_lines.append(f"KEEP ({len(keep)}):")
        table = PrettyTable(["Model ID"], align="l")
        table.add_rows([[model_id] for model_id in keep])
        plan_lines.append(table.get_string())
    if remove_plans:
        plan_lines.append(f"REMOVE ({len(remove_plans)}):")
        table = PrettyTable(["Model ID"], align="l")
        table.add_rows([[model_id] for model_id in remove_plans])
        plan_lines.append(table.get_string())
    logger.info("\n".join(plan_lines))

    if not args.apply:
        logger.info("dry run: no changes written (re-run with --apply to apply)")
        return

    for prov_id, model_id, model, prov_name in add_plans:
        model_tbl[model_id] = build_model_block(model, prov_name)
    for model_id in remove_plans:
        del model_tbl[model_id]
    doc["model"] = dict(sorted(model_tbl.items()))

    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    logger.info("wrote %s", config_path)


if __name__ == "__main__":
    main()
