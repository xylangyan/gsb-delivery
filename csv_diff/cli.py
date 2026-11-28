import click
import json as std_json
from . import load_csv, load_json, compare, human_text
from . import reconcile as reconcile_mod


@click.command()
@click.version_option()
@click.argument(
    "previous",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, allow_dash=False),
)
@click.argument(
    "current",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, allow_dash=False),
)
@click.option(
    "--key", type=str, default=None, help="Column to use as a unique ID for each row"
)
@click.option(
    "--format",
    type=click.Choice(["csv", "tsv", "json"]),
    default=None,
    help="Explicitly specify input format (csv, tsv, json) instead of auto-detecting",
)
@click.option(
    "--json", type=bool, default=False, help="Output changes as JSON", is_flag=True
)
@click.option(
    "--singular",
    type=str,
    default=None,
    help="Singular word to use, e.g. 'tree' for '1 tree'",
)
@click.option(
    "--plural",
    type=str,
    default=None,
    help="Plural word to use, e.g. 'trees' for '2 trees'",
)
@click.option(
    "--show-unchanged",
    is_flag=True,
    help="Show unchanged fields for rows with at least one change",
)
@click.option(
    "extras",
    "--extra",
    type=(str, str),
    multiple=True,
    help="key: format string - define extra fields to display",
)
@click.option(
    "occurrences",
    "--occurrences",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, allow_dash=False),
    default=None,
    help="Occurrence archive (CSV/TSV/JSON) with split/merge/transfer/issue vouchers",
)
@click.option(
    "--reconcile",
    is_flag=True,
    help="Produce a resumable split/merge reconciliation plan",
)
@click.option(
    "--teams",
    type=str,
    default="A,B",
    show_default=True,
    help="Comma-separated team order for segmented handover",
)
@click.option(
    "--segment-limit",
    type=str,
    multiple=True,
    help="TEAM=N: pause after N actions for that team (repeatable)",
)
@click.option(
    "--rules",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, allow_dash=False),
    default=None,
    help="JSON/YAML-free rules file with legal transfers and location availability",
)
@click.option(
    "--checkpoint",
    type=int,
    default=None,
    help="Resume reconciliation at this segment number",
)
@click.option(
    "--checkpoint-offset",
    type=int,
    default=0,
    show_default=True,
    help="Resume this many actions into the checkpoint segment",
)
@click.option(
    "--max-nodes",
    type=int,
    default=300000,
    show_default=True,
    help="Maximum search branches when repairing a half-posted archive",
)
@click.option(
    "--execute-team",
    type=str,
    default=None,
    help="Execute only this team's segment from the plan",
)
def cli(
    previous,
    current,
    key,
    format,
    json,
    singular,
    plural,
    show_unchanged,
    extras,
    occurrences,
    reconcile,
    teams,
    segment_limit,
    rules,
    checkpoint,
    checkpoint_offset,
    execute_team,
    max_nodes,
):
    "Diff two CSV or JSON files"
    dialect = {
        "csv": "excel",
        "tsv": "excel-tab",
    }

    if extras and json:
        raise click.UsageError(
            "Extra fields are not supported in JSON output mode",
            ctx=click.get_current_context(),
        )

    def load(filename):
        if format == "json":
            return load_json(open(filename), key=key)
        else:
            return load_csv(
                open(filename, newline=""), key=key, dialect=dialect.get(format)
            )

    if reconcile or occurrences:
        if not occurrences:
            raise click.UsageError(
                "--reconcile requires --occurrences",
                ctx=click.get_current_context(),
            )
        limits = {}
        for item in segment_limit:
            if "=" not in item:
                raise click.BadParameter(
                    "expected TEAM=N, got {!r}".format(item)
                )
            team_name, value = item.split("=", 1)
            try:
                limits[team_name.strip()] = int(value)
            except ValueError:
                raise click.BadParameter(
                    "segment limit must be an integer: {!r}".format(value)
                )
        team_order = tuple(t.strip() for t in teams.split(",") if t.strip())
        rule_data = None
        if rules:
            rule_data = std_json.load(open(rules, encoding="utf-8"))
        plan = reconcile_mod.plan_reconciliation(
            previous,
            current,
            occurrences,
            format=format,
            team_order=team_order,
            segment_limits=limits or None,
            rules=rule_data,
            max_nodes=max_nodes,
        )
        if checkpoint is not None or execute_team is not None:
            executed = reconcile_mod.execute_segment(
                plan,
                team=execute_team,
                from_segment=checkpoint or 1,
                segment_offset=checkpoint_offset,
            )
            if json:
                print(std_json.dumps(executed, indent=4, ensure_ascii=False))
            else:
                print(
                    "{}: {} actions, checkpoint segment {}".format(
                        executed["status"],
                        len(executed["actions"]),
                        executed["checkpoint"].get("next_segment"),
                    )
                )
            return
        if json:
            print(std_json.dumps(plan, indent=4, ensure_ascii=False))
        else:
            print(reconcile_mod.render_text(plan))
        if plan.get("status") != "ok":
            raise click.exceptions.Exit(2)
        return

    previous_data = load(previous)
    current_data = load(current)

    diff = compare(previous_data, current_data, show_unchanged)
    if json:
        print(std_json.dumps(diff, indent=4))
    else:
        print(
            human_text(diff, key, singular, plural, current=current_data, extras=extras)
        )
