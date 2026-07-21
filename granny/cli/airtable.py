"""Airtable operational helpers."""
from __future__ import annotations

import json

import click

from granny.airtable.client import AirtableClient, field_contains, replace_in_value


@click.group()
def airtable() -> None:
    """Inspect and update Airtable records."""


@airtable.command("bases")
def list_bases() -> None:
    """List accessible Airtable bases."""
    client = AirtableClient.from_environment()
    for base in client.list_bases():
        click.echo(f"{base.get('id')}\t{base.get('name')}")


@airtable.command("tables")
@click.option("--base", "base_id", required=True, help="Airtable base id (app...).")
def list_tables(base_id: str) -> None:
    """List tables in a base."""
    client = AirtableClient.from_environment()
    for table in client.list_tables(base_id):
        click.echo(f"{table.get('id')}\t{table.get('name')}")


@airtable.command("find-text")
@click.option("--base", "base_id", required=True, help="Airtable base id (app...).")
@click.option("--table", required=True, help="Table name or table id.")
@click.option("--contains", "needle", required=True, help="Text/URL fragment to search for.")
@click.option("--field", "fields", multiple=True, help="Limit search to field(s). Repeatable.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON lines.")
def find_text(base_id: str, table: str, needle: str, fields: tuple[str, ...], as_json: bool) -> None:
    """Find records whose field values contain text.

    This can find old webhook/API endpoints in record fields, button URL fields,
    or config tables. Airtable automations/scripts themselves are not exposed by
    the public Web API; export/edit those manually if they are not represented in
    records.
    """
    client = AirtableClient.from_environment()
    matches = 0
    for record in client.iter_records(base_id, table, fields=fields or None):
        record_fields = record.get("fields", {})
        haystack = fields or tuple(record_fields.keys())
        hit_fields = [name for name in haystack if field_contains(record_fields.get(name), needle)]
        if not hit_fields:
            continue
        matches += 1
        payload = {"id": record.get("id"), "fields": hit_fields}
        if as_json:
            click.echo(json.dumps(payload, ensure_ascii=False))
        else:
            click.echo(f"{record.get('id')}\t{','.join(hit_fields)}")
    if matches == 0:
        click.echo("no matches", err=True)


@airtable.command("replace-text")
@click.option("--base", "base_id", required=True, help="Airtable base id (app...).")
@click.option("--table", required=True, help="Table name or table id.")
@click.option("--old", required=True, help="Old text/URL fragment.")
@click.option("--new", required=True, help="Replacement text/URL fragment.")
@click.option("--field", "fields", multiple=True, required=True, help="Field(s) to update. Repeatable.")
@click.option("--dry-run/--apply", default=True, help="Preview changes unless --apply is used.")
@click.option("--confirm", is_flag=True, help="Required with --apply.")
def replace_text(
    base_id: str,
    table: str,
    old: str,
    new: str,
    fields: tuple[str, ...],
    dry_run: bool,
    confirm: bool,
) -> None:
    """Replace old endpoint text in selected Airtable record fields."""
    if not dry_run and not confirm:
        raise click.UsageError("--apply requires --confirm")

    client = AirtableClient.from_environment()
    scanned = changed = updated = 0
    for record in client.iter_records(base_id, table, fields=fields):
        scanned += 1
        record_fields = record.get("fields", {})
        update: dict[str, object] = {}
        for field in fields:
            if field not in record_fields:
                continue
            value, did_change = replace_in_value(record_fields[field], old, new)
            if did_change:
                update[field] = value
        if not update:
            continue
        changed += 1
        click.echo(f"{record.get('id')}\t{','.join(update.keys())}")
        if not dry_run:
            client.update_record(base_id, table, record["id"], update)
            updated += 1

    mode = "dry-run" if dry_run else "applied"
    click.echo(f"{mode}: scanned={scanned} changed={changed} updated={updated}")
