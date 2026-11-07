"""Purpose: `vmigrate-platform` — run and administer the platform services. Each service is its
own process (Coriolis-style split); in dev you can run all three with `all`.

Usage:
    vmigrate-platform gen-key                                  # print a new secret-store key
    vmigrate-platform init-db                                  # create tables
    vmigrate-platform create-user admin --role admin           # prompts for the password
    vmigrate-platform api [--host 0.0.0.0 --port 8080]
    vmigrate-platform conductor
    vmigrate-platform worker [--id NAME]
    vmigrate-platform all [--port 8080]                        # dev only: api + conductor + worker threads
Config comes from the environment (see vmigrate.platform.settings).
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import threading

from ..security.secretstore import generate_key


def _services():
    from ..platform.factory import Services
    from ..platform.settings import Settings
    return Services.from_settings(Settings.from_env())


def _export_agent(svc, args) -> int:  # noqa: ANN001
    import socket

    import uvicorn

    from ..export.agent import ExportAgent, cert_fingerprint, ensure_self_signed, heartbeat_loop, make_agent_app
    from ..export.codec import derive_key
    host = socket.getfqdn()
    tls = svc.settings.data_dir / "export-tls"
    cert, key = args.cert or str(tls / "cert.pem"), args.key or str(tls / "key.pem")
    if not args.cert:
        ensure_self_signed(cert, key, host)
    url = args.url or f"https://{host}:{args.port}"
    k = derive_key(svc.settings.secret_key)
    agent = ExportAgent(svc, k)
    stop = threading.Event()
    region = os.environ.get("VMIGRATE_WORKER_REGION") or None
    threading.Thread(target=heartbeat_loop, name="export-heartbeat", daemon=True,
                     kwargs={"services": svc, "agent": agent, "agent_id": f"export-{host}-{args.port}", "url": url,
                             "fingerprint": cert_fingerprint(cert), "region": region, "stop": stop}).start()
    logging.getLogger("vmigrate.export").info("export agent %s (region %s, cert sha256 %s)", url, region,
                                              cert_fingerprint(cert))
    uvicorn.run(make_agent_app(svc, k, agent), host=args.host, port=args.port, ssl_certfile=cert, ssl_keyfile=key,
                log_level=args.log_level.lower())
    stop.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vmigrate-platform")
    ap.add_argument("--log-level", default=os.environ.get("VMIGRATE_LOG_LEVEL", "INFO"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-key")
    sub.add_parser("init-db")
    cu = sub.add_parser("create-user")
    cu.add_argument("username")
    cu.add_argument("--role", choices=["viewer", "operator", "admin"], default="operator")
    cu.add_argument("--project", default="default")
    cu.add_argument("--password-env", help="read the password from this env var instead of prompting")
    for name in ("api", "all"):
        p = sub.add_parser(name)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8080)
    sub.add_parser("conductor")
    w = sub.add_parser("worker")
    w.add_argument("--id")
    ea = sub.add_parser("export-agent", help="serve this site's source disks to workers at other sites (HTTPS)")
    ea.add_argument("--host", default="0.0.0.0")
    ea.add_argument("--port", type=int, default=8443)
    ea.add_argument("--url", help="URL other sites use (default https://<hostname>:<port>)")
    ea.add_argument("--cert", help="TLS certificate (default: self-signed in <data_dir>/export-tls/, pinned by peers)")
    ea.add_argument("--key")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s")

    if args.cmd == "gen-key":
        print(generate_key())
        return 0
    from ..core.errors import ConfigError
    try:
        svc = _services()
    except ConfigError as e:
        print(f"vmigrate-platform: {e}", file=sys.stderr)
        return 2
    if args.cmd == "init-db":
        svc.repo.init_schema()
        print("schema ready")
        return 0
    if args.cmd == "create-user":
        from ..security.auth import hash_password
        pw = os.environ.get(args.password_env, "") if args.password_env else getpass.getpass("password: ")
        if len(pw) < 12:
            print("password must be at least 12 characters", file=sys.stderr)
            return 2
        if svc.repo.get_user_by_name(args.username):
            print("user exists", file=sys.stderr)
            return 2
        svc.repo.create_user(args.username, hash_password(pw), args.role, args.project)
        print(f"created {args.role} {args.username}")
        return 0
    if args.cmd == "conductor":
        from ..conductor.service import Conductor
        Conductor(svc).run_forever()
        return 0
    if args.cmd == "worker":
        from ..worker.service import Worker
        Worker(svc, args.id).run_forever()
        return 0
    if args.cmd == "export-agent":
        return _export_agent(svc, args)
    import uvicorn

    from ..api.app import create_app
    if args.cmd == "all":
        from ..conductor.service import Conductor
        from ..worker.service import Worker
        threading.Thread(target=Conductor(svc).run_forever, name="conductor", daemon=True).start()
        threading.Thread(target=Worker(svc).run_forever, name="worker", daemon=True).start()
    uvicorn.run(create_app(svc), host=args.host, port=args.port, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
