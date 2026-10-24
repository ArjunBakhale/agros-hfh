from __future__ import annotations

import argparse
import json
import os
import secrets
import sys

import psycopg
from psycopg.rows import dict_row

from .config import get_settings

FIXTURE_CONTACTS = [
    ("FX-0001", "+13135550101", {"adult": True, "on_or_preparing_for_dialysis": True}),
    ("FX-0002", "+13135550102", {"adult": True, "on_or_preparing_for_dialysis": True}),
    ("FX-0003", "+13135550103", {"adult": True}),
    ("FX-0004", "+13135550104", {}),
    ("FX-0005", "+13135550105", {"adult": True, "on_or_preparing_for_dialysis": True}),
    ("FX-0006", "+13135550106", {}),
]


def seed(admin_url: str, password: str) -> dict[str, str]:
    """Idempotently create a demo tenant (+ an isolation-check tenant), users and fixture contacts."""
    from .audit import append_audit_event
    from .auth import hash_password

    s = get_settings()
    out: dict[str, str] = {}
    with psycopg.connect(admin_url, row_factory=dict_row) as conn:
        for tname, prefix in (
            ("Agros HFH pilot sandbox (synthetic)", ""),
            ("Isolation check tenant", "other+"),
        ):
            t = conn.execute("select id from tenants where name=%s", (tname,)).fetchone()
            if not t:
                t = conn.execute(
                    "insert into tenants(name) values (%s) returning id", (tname,)
                ).fetchone()
            assert t
            tid = str(t["id"])
            out[tname] = tid
            local, domain = s.seed_admin_email.split("@")
            for role in ("admin", "manager", "cta", "viewer"):
                email = f"{prefix}{local}{'' if role == 'admin' else '+' + role}@{domain}".lower()
                conn.execute(
                    """insert into users (tenant_id, email, password_hash, role, display_name)
                       values (%s,%s,%s,%s,%s) on conflict (email) do update set password_hash=excluded.password_hash""",
                    (tid, email, hash_password(password), role, f"Demo {role}"),
                )
            camp = conn.execute(
                "select id from voice_campaigns where tenant_id=%s and name=%s",
                (tid, "Dialysis education demo"),
            ).fetchone()
            if not camp:
                camp = conn.execute(
                    """insert into voice_campaigns (tenant_id, name, script_id, status, max_attempts)
                       values (%s,'Dialysis education demo','hfh_dialysis_education_demo','active',3)
                       returning id""",
                    (tid,),
                ).fetchone()
                assert camp
                append_audit_event(
                    conn,
                    tenant_id=tid,
                    actor_type="system",
                    actor_id=None,
                    action="voice_campaign.seeded",
                    object_type="voice_campaign",
                    object_id=camp["id"],
                    payload={"fixture": True},
                )
            for token, phone, flags in FIXTURE_CONTACTS:
                conn.execute(
                    """insert into voice_contacts (tenant_id, campaign_id, contact_token, phone_e164,
                         eligibility_flags, is_fixture)
                       values (%s,%s,%s,%s,%s::jsonb,true) on conflict do nothing""",
                    (
                        tid,
                        camp["id"],
                        f"{prefix.upper().strip('+')}{token}",
                        phone,
                        json.dumps(flags),
                    ),
                )
        conn.commit()
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="agros-voice")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("seed")
    sub.add_parser("bootstrap", help="migrate + seed (used by the AWS migrate task)")
    api = sub.add_parser("api")
    api.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container entrypoint
    api.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    sub.add_parser("worker")
    sub.add_parser("agent", help="LiveKit agent worker (pass LiveKit CLI args after --)")
    args, rest = ap.parse_known_args(argv)
    s = get_settings()
    if args.cmd in ("migrate", "seed", "bootstrap"):
        admin = s.admin_database_url or sys.exit("ADMIN_DATABASE_URL is required")
        if args.cmd in ("migrate", "bootstrap"):
            from .db import migrate

            pw = s.app_db_password or sys.exit("APP_DB_PASSWORD is required")
            print("applied:", migrate(admin, pw))
        if args.cmd in ("seed", "bootstrap"):
            pw = s.seed_password or secrets.token_urlsafe(12)
            print("seeded tenants:", seed(admin, pw))
            if not s.seed_password:
                print("generated seed password (shown once):", pw)
    elif args.cmd == "api":
        import uvicorn

        uvicorn.run(
            "agros_voice.api:app",
            host=args.host,
            port=args.port,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    elif args.cmd == "worker":
        from .worker import main as worker_main

        worker_main()
    elif args.cmd == "agent":
        from .agent.livekit_agent import run

        run(rest or ["start"])


if __name__ == "__main__":
    main()
