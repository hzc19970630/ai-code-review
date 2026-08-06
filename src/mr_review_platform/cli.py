from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from mr_review_platform.config import get_settings
from mr_review_platform.db import Database
from mr_review_platform.models import Project
from mr_review_platform.services import DEFAULT_EXTENSIONS, ProjectRegistration, register_project


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mr-review")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create tables for local development only")

    register = sub.add_parser("register-project")
    register.add_argument("--provider-project-id", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--namespace")
    register.add_argument("--repository-url", required=True)
    register.add_argument("--web-url")
    register.add_argument("--default-branch")
    register.add_argument("--webhook-secret-ref", required=True)
    register.add_argument("--git-token-ref", required=True)
    register.add_argument("--supported-extensions", default=",".join(DEFAULT_EXTENSIONS))
    register.add_argument("--publish-mode", choices=("publish", "shadow"), default="publish")

    sub.add_parser("list-projects")
    sub.add_parser("reconcile")
    sub.add_parser("dispatch")
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--dataset", required=True)
    evaluate.add_argument("--predictions")
    preview = sub.add_parser("rules-preview")
    preview.add_argument("--file", action="append", required=True)
    preview.add_argument(
        "--enable",
        default="correctness,security,api_compatibility,reliability",
    )
    preview.add_argument("--language", action="append", default=[])
    preview.add_argument("--framework", action="append", default=[])
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = get_settings()
    database = Database(settings.database_url)

    if args.command == "init-db":
        database.create_schema()
        print("schema created")
        return
    if args.command == "register-project":
        extensions = [item.strip() for item in args.supported_extensions.split(",") if item.strip()]
        with database.session() as session:
            project = register_project(
                session,
                ProjectRegistration(
                    provider_project_id=args.provider_project_id,
                    name=args.name,
                    namespace=args.namespace,
                    repository_url=args.repository_url,
                    web_url=args.web_url,
                    default_branch=args.default_branch,
                    webhook_secret_ref=args.webhook_secret_ref,
                    git_token_ref=args.git_token_ref,
                    supported_extensions=extensions,
                    publish_mode=args.publish_mode,
                ),
            )
        print(f"registered project {project.id}: {project.namespace or project.name}")
        return
    if args.command == "list-projects":
        with database.session() as session:
            for project in session.scalars(select(Project).order_by(Project.id)):
                print(project.id, project.provider_project_id, project.namespace or project.name)
        return
    if args.command == "reconcile":
        from mr_review_platform.worker import reconcile_once

        count = reconcile_once(settings)
        print(f"recovered {count} review job deliveries")
        return
    if args.command == "dispatch":
        from mr_review_platform.dispatcher import dispatch_outbox_once

        count = dispatch_outbox_once(settings)
        print(f"published {count} outbox events")
        return
    if args.command == "evaluate":
        from mr_review_platform.evaluation import (
            dataset_summary,
            evaluate_dataset,
            load_predictions,
            load_seed_dataset,
            report_json,
        )

        dataset = load_seed_dataset(args.dataset)
        if args.predictions:
            print(report_json(evaluate_dataset(dataset, load_predictions(args.predictions))))
        else:
            print(json.dumps(dataset_summary(dataset), ensure_ascii=False, indent=2))
        return
    if args.command == "rules-preview":
        from mr_review_platform.project_profile import ProjectProfile
        from mr_review_platform.rule_catalog import RuleCompiler

        profile = ProjectProfile(
            source_ref="cli-preview",
            languages=tuple(sorted(set(args.language))),
            frameworks=tuple(sorted(set(args.framework))),
            workspace_roots=(".",),
            total_chars=0,
        )
        compiled = RuleCompiler().compile(
            file_paths=args.file,
            project_profile=profile,
            enabled_selectors=[item.strip() for item in args.enable.split(",") if item.strip()],
        )
        print(
            json.dumps(
                {
                    **compiled.model_dump(mode="json"),
                    "rule_set_hash": compiled.content_hash,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
