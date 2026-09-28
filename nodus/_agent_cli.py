"""Terminal operations for managed deployments and durable runs."""

import json
from pathlib import Path
import sys

from ._agent import encode
from ._managed_agents import _key
from ._project_cli import add_source_arguments, source_options
from .errors import NodusError, ValidationError


def add_parser(commands, positive_cost, page_limit):
    parser = commands.add_parser("agent", help="deploy managed durable agents")
    sub = parser.add_subparsers(dest="agent_cmd", required=True)
    deploy = sub.add_parser("deploy", help="deploy a Python agent project")
    deploy.add_argument("name")
    add_source_arguments(deploy, project_help="project directory, defaults to the current directory")
    deploy.add_argument("--entrypoint", default="agent:main")
    deploy.add_argument("--setup", help="dependency setup command saved with the agent revision")
    deploy.add_argument("--budget", type=positive_cost, default=None, help="deprecated compatibility field")
    deploy.add_argument("--max-workers", type=int)
    deploy.add_argument("--min-workers", type=int)
    deploy.add_argument("--secret", action="append")
    deploy.add_argument("--network-permission", action="append")
    deploy.add_argument("--idempotency-key")
    listing = sub.add_parser("list", aliases=["ls"], help="list managed agents")
    listing.add_argument("--limit", type=page_limit, default=50)
    for action in ("detail", "submit", "runs", "signal", "pause", "resume", "cancel", "steps", "resolve"):
        operation = sub.add_parser(action)
        operation.add_argument("agent_id")
        if action in ("signal", "cancel", "steps", "resolve"):
            operation.add_argument("run_id")
        if action in ("submit", "signal"):
            data = operation.add_mutually_exclusive_group(required=action == "submit")
            data.add_argument("--input", help="JSON input")
            data.add_argument("--input-file", help="read JSON input from a file")
            operation.add_argument("--idempotency-key", required=True)
        elif action in ("pause", "resume", "cancel", "resolve"):
            operation.add_argument("--idempotency-key")
        if action == "submit":
            operation.add_argument("--session")
        if action == "signal":
            operation.add_argument("name")
        if action in ("runs", "steps"):
            operation.add_argument("--limit", type=page_limit, default=50)
            operation.add_argument("--after")
        if action == "resolve":
            operation.add_argument("--step-id", required=True)
            operation.add_argument("--expected-revision", type=int, required=True)
            operation.add_argument("--decision", choices=("completed", "no_effect", "cancelled"), required=True)
            operation.add_argument("--reason", required=True)
            operation.add_argument("--evidence-digest", required=True)
            operation.add_argument("--result", help="verified result as JSON for a completed decision")
            operation.add_argument("--checkpoint-id", help="verified application state to retain with a completed result")


def _input(args):
    raw = args.input
    if args.input_file:
        with Path(args.input_file).open("rb") as source:
            data = source.read((256 << 10) + 1)
        if len(data) > 256 << 10:
            raise ValidationError("Agent input exceeds 256 KiB")
        raw = data.decode("utf-8")
    try:
        value = json.loads(raw) if raw is not None else None
    except (ValueError, UnicodeError):
        raise ValidationError("Agent input must be valid JSON") from None
    encode(value)
    return value


def run(args, client_factory):
    source = source_options(args, default_project=True) if args.agent_cmd == "deploy" else None
    with client_factory(base_url=args.base_url) as client:
        action = args.agent_cmd
        key = _key(getattr(args, "idempotency_key", None))
        try:
            if action == "deploy":
                result = client.agents.create(name=args.name, entrypoint=args.entrypoint, budget=args.budget,
                    min_workers=args.min_workers, max_workers=args.max_workers, secrets=args.secret,
                    idempotency_key=key, **source)
                output = result.raw
            elif action in ("list", "ls"):
                output = [agent.raw for agent in client.agents.list(limit=args.limit)]
            else:
                agent = client.agents.get(args.agent_id)
                if action == "detail":
                    output = agent.raw
                elif action == "submit":
                    output = agent.submit(_input(args), session=args.session, idempotency_key=key).raw
                elif action == "runs":
                    rows, after = agent.runs.list_page(limit=args.limit, after=args.after)
                    output = {"runs": [row.raw for row in rows], "next_after": after}
                elif action in ("pause", "resume"):
                    output = getattr(agent, action)(idempotency_key=key).raw
                else:
                    workload = agent.runs.get(args.run_id)
                    if action == "signal":
                        output = workload.signal(args.name, _input(args), idempotency_key=key).raw
                    elif action == "cancel":
                        output = workload.cancel(idempotency_key=key).raw
                    elif action == "steps":
                        output = workload.steps(limit=args.limit, after=args.after or "")
                    else:
                        result = json.loads(args.result) if args.result is not None else None
                        output = workload.resolve(step_id=args.step_id, expected_revision=args.expected_revision,
                            decision=args.decision, reason=args.reason, evidence_digest=args.evidence_digest,
                            result=result, checkpoint_id=args.checkpoint_id, idempotency_key=key)
        except NodusError as error:
            if isinstance(error.body, dict) and error.body.get("idempotency_key"):
                print("Retry the unchanged operation with --idempotency-key " + json.dumps(key), file=sys.stderr)
            raise
        print(json.dumps(output, indent=2, default=str))
    return 0
