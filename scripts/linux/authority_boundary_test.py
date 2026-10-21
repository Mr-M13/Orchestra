#!/usr/bin/env python3
"""Disposable Linux boundary test for the shadow authority service.

Run as root inside a throwaway Linux container:

    python3 scripts/linux/authority_boundary_test.py

It creates real Unix users/groups, starts the authority under its own UID and
drives it through a Unix socket as coordinator, approver, verifier, worker,
operator and an unconfigured rogue identity.  It makes no network calls and
touches no production service.
"""

from __future__ import annotations

import json
import os
import grp
import pwd
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestra.authority import ExecutionAudience  # noqa: E402
from orchestra.authority_service import ServiceConfig  # noqa: E402
from orchestra.contracts import (  # noqa: E402
    ArtifactReference,
    EvidenceReference,
    ExecutionReport,
    FileScope,
    IsolationRequirements,
    NetworkPolicy,
    ResourceLimits,
    TaskSpec,
    TestEvidence,
    ValidationOutcome,
    VerificationRecord,
)


BASE_COMMIT = "a" * 40
ADAPTER = "fake-local-adapter"
RUNTIME = "local_process"

CLIENTS_GROUP = "orchestra-clients"
AUTHORITY_GROUP = "orchestra-authority"
CLIENTS_GID = 2100
AUTHORITY_GID = 2101

SERVICE_UID = 2101
COORDINATOR_UID = 2102
APPROVER_UID = 2103
VERIFIER_UID = 2104
WORKER_UID = 2105
OPERATOR_UID = 2106
ROGUE_UID = 2107

USER_NAMES = {
    SERVICE_UID: "orchestra-authority",
    COORDINATOR_UID: "orchestra-coordinator",
    APPROVER_UID: "orchestra-approver",
    VERIFIER_UID: "orchestra-verifier",
    WORKER_UID: "orchestra-worker",
    OPERATOR_UID: "orchestra-operator",
    ROGUE_UID: "orchestra-rogue",
}

INSTALL_DIR = Path("/opt/orchestra-phase3a")
STATE_DIR = Path("/var/lib/orchestra-authority")
RUNTIME_DIR = Path("/run/orchestra-authority")
CONFIG_DIR = Path("/etc/orchestra")
CONFIG_PATH = CONFIG_DIR / "authority.json"
DB_PATH = STATE_DIR / "authority.sqlite3"
SOCKET_PATH = RUNTIME_DIR / "authority.sock"
LOCK_PATH = RUNTIME_DIR / "authority.lock"
SERVICE_LOG = Path("/tmp/orchestra-authority-boundary.log")
PYTHON = sys.executable
PASSED: list[str] = []


class BoundaryFailure(RuntimeError):
    pass


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        print(f"FAIL {name}: {detail}")
        raise BoundaryFailure(name)
    PASSED.append(name)
    print(f"PASS {name}")


def _uid_env() -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": str(INSTALL_DIR),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
        "HOME": "/nonexistent",
    }


def as_uid_command(uid: int, argv: list[str]) -> list[str]:
    primary_gid = AUTHORITY_GID if uid == SERVICE_UID else CLIENTS_GID
    return [
        "/usr/bin/setpriv",
        f"--reuid={uid}",
        f"--regid={primary_gid}",
        "--groups", str(CLIENTS_GID),
        "--no-new-privs",
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--reset-env",
        "/usr/bin/env",
        "-i",
        *[f"{key}={value}" for key, value in _uid_env().items()],
        *argv,
    ]


def run_as_uid(uid: int, argv: list[str], *, timeout: float = 20.0):
    return subprocess.run(
        as_uid_command(uid, argv),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def setup_identities() -> None:
    for name, gid in (
        (CLIENTS_GROUP, CLIENTS_GID),
        (AUTHORITY_GROUP, AUTHORITY_GID),
    ):
        try:
            grp.getgrnam(name)
        except KeyError:
            subprocess.run(
                ["/usr/sbin/groupadd", "--gid", str(gid), name],
                check=True, capture_output=True, text=True,
            )
    for uid, name in USER_NAMES.items():
        try:
            pwd.getpwnam(name)
            continue
        except KeyError:
            pass
        primary = AUTHORITY_GROUP if uid == SERVICE_UID else CLIENTS_GROUP
        cmd = [
            "/usr/sbin/useradd",
            "--uid", str(uid),
            "--gid", primary,
            "--no-create-home",
            "--home-dir", "/nonexistent",
            "--shell", "/usr/sbin/nologin",
        ]
        if uid == SERVICE_UID:
            cmd += ["--groups", CLIENTS_GROUP]
        cmd.append(name)
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    for uid, name in USER_NAMES.items():
        info = pwd.getpwnam(name)
        check(
            f"real OS identity {name} uses uid {uid}",
            info.pw_uid == uid,
            f"got {info.pw_uid}",
        )


def copy_source() -> None:
    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR)
    INSTALL_DIR.mkdir(mode=0o755)
    shutil.copytree(
        ROOT / "orchestra", INSTALL_DIR / "orchestra",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(ROOT / "scripts", INSTALL_DIR / "scripts")
    for path in [INSTALL_DIR, *INSTALL_DIR.rglob("*")]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    check(
        "source is readable by all separated identities",
        (INSTALL_DIR / "orchestra" / "authority_service.py").is_file(),
    )


def setup_directories() -> None:
    for path in (STATE_DIR, RUNTIME_DIR, CONFIG_DIR):
        if path.exists():
            shutil.rmtree(path)
    STATE_DIR.mkdir(mode=0o700, parents=True)
    RUNTIME_DIR.mkdir(mode=0o750, parents=True)
    CONFIG_DIR.mkdir(mode=0o755, parents=True)
    os.chown(STATE_DIR, SERVICE_UID, AUTHORITY_GID)
    os.chown(RUNTIME_DIR, SERVICE_UID, CLIENTS_GID)
    os.chmod(STATE_DIR, 0o700)
    os.chmod(RUNTIME_DIR, 0o750)
    os.chmod(CONFIG_DIR, 0o755)


def make_task_object(task_id: str, request_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        request_id=request_id,
        repository="owner/linux-boundary",
        base_commit=BASE_COMMIT,
        agent_id="fake-coding-agent",
        model_provider="fake-provider",
        model_id="fake-model",
        coding_harness="fake-harness",
        permitted_runtimes=(RUNTIME,),
        authorized_operations=(
            "read_repository", "edit_workspace", "run_tests",
        ),
        file_scope=FileScope(read_paths=(".",), write_paths=("src",)),
        network=NetworkPolicy("denied"),
        isolation=IsolationRequirements(
            filesystem="ephemeral_workspace",
            credentials="broker_only",
            git_metadata="read_only",
            sandbox="ephemeral_runner",
        ),
        resource_limits=ResourceLimits(max_runtime_seconds=120),
        approval_class="routine",
        consequence_class="routine_development",
        instructions=f"Disposable Linux boundary task {task_id}.",
    )


def task_payload(task_id: str, request_id: str) -> dict:
    return make_task_object(task_id, request_id).to_dict()


def report_payload(task: TaskSpec, attempt_id: str) -> dict:
    return ExecutionReport(
        report_id=f"report-{task.task_id}",
        task_id=task.task_id,
        proposal_digest=task.digest,
        attempt_id=attempt_id,
        provider_run_id=f"fake-run-{task.task_id}",
        adapter_id=ADAPTER,
        runtime=RUNTIME,
        agent_id=task.agent_id,
        model_provider=task.model_provider,
        model_id=task.model_id,
        coding_harness=task.coding_harness,
        outcome="succeeded",
        artifacts=(
            ArtifactReference(
                uri=f"artifact:{task.task_id}", sha256="b" * 64,
                media_type="text/plain", size_bytes=18,
            ),
        ),
        commit_sha=BASE_COMMIT,
        test_evidence=(
            TestEvidence("boundary check", "passed", f"artifact:{task.task_id}"),
        ),
        started_at="2026-10-21T00:00:00+00:00",
        finished_at="2026-10-21T00:00:03+00:00",
    ).to_dict()


def verification_payload(task: TaskSpec, attempt_id: str) -> dict:
    return VerificationRecord(
        verification_id=f"verification-{task.task_id}",
        task_id=task.task_id,
        proposal_digest=task.digest,
        attempt_id=attempt_id,
        verifier_id=f"uid:{VERIFIER_UID}",
        method="independent-recompute",
        evidence_checked=(
            EvidenceReference("artifact", f"artifact:{task.task_id}", "b" * 64),
        ),
        outcomes=(
            ValidationOutcome("recomputed artifact digest", "pass"),
        ),
        status="pass",
        verified_at="2026-10-21T00:00:05+00:00",
    ).to_dict()


def config_payload() -> dict:
    from orchestra.authority_service import PeerIdentity

    probe = make_task_object("audience-probe", "audience-probe")
    audience = ExecutionAudience(
        adapter_id=ADAPTER,
        runtime=RUNTIME,
        agent_id=probe.agent_id,
        model_provider=probe.model_provider,
        model_id=probe.model_id,
        coding_harness=probe.coding_harness,
    )
    config = ServiceConfig(
        authority_id="authority:linux-boundary",
        service_uid=SERVICE_UID,
        database_path=str(DB_PATH),
        socket_path=str(SOCKET_PATH),
        socket_gid=CLIENTS_GID,
        peers=(
            PeerIdentity(COORDINATOR_UID, "coordinator", "coordinator"),
            PeerIdentity(APPROVER_UID, "human", "approver"),
            PeerIdentity(VERIFIER_UID, "verifier", "verifier"),
            PeerIdentity(WORKER_UID, "worker", "worker"),
            PeerIdentity(OPERATOR_UID, "operator", "operator"),
        ),
        audiences=(audience,),
        standing_authorizations=(),
        grant_ttl_seconds=120,
        approval_ttl_seconds=300,
        stale_attempt_seconds=60,
        max_message_bytes=1_048_576,
        socket_timeout_seconds=30,
    )
    return config.to_dict()

def write_config() -> None:
    payload = config_payload()
    CONFIG_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chown(CONFIG_PATH, 0, AUTHORITY_GID)
    os.chmod(CONFIG_PATH, 0o640)
    check(
        "config is root-owned and unreadable by coordinator/worker",
        stat.S_IMODE(CONFIG_PATH.stat().st_mode) == 0o640,
    )


def start_service_process():
    log = open(SERVICE_LOG, "ab", buffering=0)
    proc = subprocess.Popen(
        as_uid_command(
            SERVICE_UID,
            [PYTHON, "-m", "orchestra.authority_service", "--config",
             str(CONFIG_PATH)],
        ),
        stdout=log,
        stderr=log,
        cwd="/",
    )
    proc._orchestra_log = log  # type: ignore[attr-defined]
    return proc


def log_tail(lines: int = 40) -> str:
    try:
        return "\n".join(
            SERVICE_LOG.read_text(errors="replace").splitlines()[-lines:]
        )
    except FileNotFoundError:
        return ""


def wait_for_ready(proc) -> dict:
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise BoundaryFailure(
                f"authority service exited with {proc.returncode}\n{log_tail()}"
            )
        if SOCKET_PATH.exists():
            response = probe(COORDINATOR_UID, "health")
            if response.get("ok"):
                return response["result"]
        time.sleep(0.05)
    raise BoundaryFailure(f"authority service did not become ready\n{log_tail()}")


def stop_service(proc, *, sig=signal.SIGTERM, timeout=10) -> int:
    if proc.poll() is None:
        proc.send_signal(sig)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait(timeout=5)
    log = getattr(proc, "_orchestra_log", None)
    if log is not None:
        log.close()
    return code


def probe(uid: int, operation: str, payload: dict | None = None,
          request_id: str | None = None) -> dict:
    command = [
        PYTHON,
        str(INSTALL_DIR / "scripts" / "linux" / "authority_peer_probe.py"),
        "--socket", str(SOCKET_PATH),
        "--operation", operation,
    ]
    if payload is not None:
        command += ["--payload", json.dumps(payload, sort_keys=True)]
    if request_id is not None:
        command += ["--request-id", request_id]
    result = run_as_uid(uid, command, timeout=20)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise BoundaryFailure(
            f"probe uid={uid} op={operation} produced no JSON: "
            f"rc={result.returncode} stderr={result.stderr[-1000:]}"
        )
    try:
        response = json.loads(lines[-1])
    except json.JSONDecodeError:
        raise BoundaryFailure(
            f"probe uid={uid} op={operation} produced invalid JSON: {lines[-1]}"
        ) from None
    return response


def expect_ok(response: dict, name: str):
    check(
        name, response.get("ok") is True,
        json.dumps(response, sort_keys=True)[:1500],
    )
    return response.get("result")


def expect_error(response: dict, code: str, name: str) -> None:
    actual = response.get("error", {}).get("code") if not response.get("ok") else None
    check(
        name, actual == code,
        f"expected {code}, got {actual}: "
        f"{json.dumps(response, sort_keys=True)[:1500]}",
    )


def run_boundary_checks() -> None:
    setup_identities()
    copy_source()
    setup_directories()
    write_config()

    proc = start_service_process()
    try:
        health = wait_for_ready(proc)
        check(
            "authority service runs under the dedicated uid",
            health["service_uid"] == SERVICE_UID,
            str(health),
        )
        check(
            "coordinator peer identity is derived by the server",
            health["peer_uid"] == COORDINATOR_UID
            and health["peer_role"] == "coordinator",
            str(health),
        )
        check(
            "state directory is owner-only",
            stat.S_IMODE(STATE_DIR.stat().st_mode) == 0o700
            and STATE_DIR.stat().st_uid == SERVICE_UID,
            f"{oct(stat.S_IMODE(STATE_DIR.stat().st_mode))}",
        )
        check(
            "database is owner-only",
            stat.S_IMODE(DB_PATH.stat().st_mode) == 0o600
            and DB_PATH.stat().st_uid == SERVICE_UID,
            f"{oct(stat.S_IMODE(DB_PATH.stat().st_mode))}",
        )
        check(
            "runtime directory is traversable only by the client group",
            stat.S_IMODE(RUNTIME_DIR.stat().st_mode) == 0o750
            and RUNTIME_DIR.stat().st_gid == CLIENTS_GID,
            f"{oct(stat.S_IMODE(RUNTIME_DIR.stat().st_mode))}",
        )
        check(
            "socket is group-accessible but not writable by others",
            stat.S_IMODE(SOCKET_PATH.stat().st_mode) == 0o660
            and SOCKET_PATH.stat().st_gid == CLIENTS_GID,
            f"{oct(stat.S_IMODE(SOCKET_PATH.stat().st_mode))}",
        )

        # -- unauthorized peers -------------------------------------------
        rogue = probe(ROGUE_UID, "health")
        expect_error(rogue, "unauthorized_peer", "unconfigured socket peer is rejected")
        service_events = expect_ok(
            probe(OPERATOR_UID, "service_events", {"limit": 50}, "audit-1"),
            "operator can read the service audit trail",
        )
        check(
            "service audit records startup and peer rejection",
            any(item["event"] == "service_started" for item in service_events)
            and any(item["event"] == "peer_rejected" for item in service_events),
            json.dumps(service_events[-3:], sort_keys=True)[:1000],
        )
        expect_error(
            probe(COORDINATOR_UID, "service_events", {}, "audit-coordinator"),
            "operation_not_permitted",
            "coordinator cannot read service-level audit",
        )

        # -- separate identity operations ---------------------------------
        task_a = make_task_object("task-a", "request-a")
        payload_a = task_payload("task-a", "request-a")
        proposal_a = expect_ok(
            probe(COORDINATOR_UID, "propose", {"task": payload_a}, "A-propose"),
            "coordinator may register a proposal",
        )
        check(
            "proposal digest is canonical TaskSpec identity",
            proposal_a["proposal_digest"] == task_a.digest,
        )
        expect_error(
            probe(
                COORDINATOR_UID, "decide_approval",
                {"proposal_digest": proposal_a["proposal_digest"],
                 "decision": "approved"},
                "A-coordinator-approve",
            ),
            "operation_not_permitted",
            "coordinator cannot impersonate the approver",
        )
        expect_error(
            probe(
                COORDINATOR_UID, "record_verification",
                {"verification": verification_payload(
                    task_a, "attempt-does-not-exist"
                )},
                "A-coordinator-verify",
            ),
            "operation_not_permitted",
            "coordinator cannot impersonate the verifier",
        )
        expect_error(
            probe(
                COORDINATOR_UID, "propose",
                {"task": payload_a, "role": "human"},
                "A-identity-injection",
            ),
            "invalid_request",
            "caller-supplied role is rejected",
        )
        expect_error(
            probe(
                COORDINATOR_UID, "propose",
                {"task": payload_a, "standing_authorizations": []},
                "A-policy-injection",
            ),
            "invalid_request",
            "coordinator cannot inject policy fields",
        )
        expect_error(
            probe(COORDINATOR_UID, "update_policy", {}, "A-update-policy"),
            "unknown_operation",
            "there is no policy-mutation operation",
        )
        expect_error(
            probe(COORDINATOR_UID, "sweep", {}, "A-coordinator-sweep"),
            "operation_not_permitted",
            "coordinator cannot perform operator recovery",
        )
        expect_ok(
            probe(
                APPROVER_UID, "decide_approval",
                {"proposal_digest": proposal_a["proposal_digest"],
                 "decision": "approved", "reason": "separate approver"},
                "A-approve",
            ),
            "independent approver can approve over the socket",
        )

        # -- coordinator cannot read or modify protected state -------------
        direct = run_as_uid(
            COORDINATOR_UID,
            [PYTHON, "-c",
             "import sys\n"
             "try:\n"
             "    open(sys.argv[1],'rb').read(1); print('READ')\n"
             "except PermissionError:\n"
             "    print('DENIED')\n"
             "except Exception as e:\n"
             "    print(type(e).__name__)\n",
             str(DB_PATH)],
        )
        check(
            "coordinator cannot open the authority database",
            direct.stdout.strip() == "DENIED",
            direct.stdout.strip() or direct.stderr[-500:],
        )
        db_write = run_as_uid(
            COORDINATOR_UID,
            [PYTHON, "-c",
             "import sys\n"
             "try:\n"
             "    open(sys.argv[1],'ab').write(b'x'); print('WROTE')\n"
             "except PermissionError:\n"
             "    print('DENIED')\n"
             "except Exception as e:\n"
             "    print(type(e).__name__)\n",
             str(DB_PATH)],
        )
        check(
            "coordinator cannot modify the authority database",
            db_write.stdout.strip() == "DENIED",
            db_write.stdout.strip() or db_write.stderr[-500:],
        )
        worker_db = run_as_uid(
            WORKER_UID,
            [PYTHON, "-c",
             "import sqlite3, sys\n"
             "try:\n"
             "    sqlite3.connect(sys.argv[1]); print('OPEN')\n"
             "except Exception as e:\n"
             "    print(type(e).__name__)\n",
             str(DB_PATH)],
        )
        check(
            "worker cannot open the authority database",
            worker_db.stdout.strip() in {"OperationalError", "PermissionError"},
            worker_db.stdout.strip() or worker_db.stderr[-500:],
        )
        config_read = run_as_uid(
            COORDINATOR_UID,
            [PYTHON, "-c",
             "import sys\n"
             "try:\n"
             "    open(sys.argv[1],'rb').read(1); print('READ')\n"
             "except PermissionError:\n"
             "    print('DENIED')\n",
             str(CONFIG_PATH)],
        )
        check(
            "coordinator cannot read authority policy config",
            config_read.stdout.strip() == "DENIED",
            config_read.stdout.strip(),
        )
        worker_config_read = run_as_uid(
            WORKER_UID,
            [PYTHON, "-c",
             "import sys\n"
             "try:\n"
             "    open(sys.argv[1],'rb').read(1); print('READ')\n"
             "except PermissionError:\n"
             "    print('DENIED')\n",
             str(CONFIG_PATH)],
        )
        check(
            "worker cannot read authority policy config",
            worker_config_read.stdout.strip() == "DENIED",
            worker_config_read.stdout.strip(),
        )
        config_write = run_as_uid(
            COORDINATOR_UID,
            [PYTHON, "-c",
             "import sys\n"
             "try:\n"
             "    open(sys.argv[1],'ab').write(b'x'); print('WROTE')\n"
             "except PermissionError:\n"
             "    print('DENIED')\n"
             "except Exception as e:\n"
             "    print(type(e).__name__)\n",
             str(CONFIG_PATH)],
        )
        check(
            "coordinator cannot modify authority policy config",
            config_write.stdout.strip() == "DENIED",
            config_write.stdout.strip() or config_write.stderr[-500:],
        )
        dir_list = run_as_uid(
            COORDINATOR_UID,
            [PYTHON, "-c",
             "import os, sys\n"
             "try:\n"
             "    os.listdir(sys.argv[1]); print('LISTED')\n"
             "except PermissionError:\n"
             "    print('DENIED')\n",
             str(STATE_DIR)],
        )
        check(
            "coordinator cannot list the authority state directory",
            dir_list.stdout.strip() == "DENIED",
            dir_list.stdout.strip(),
        )
        socket_unlink = run_as_uid(
            COORDINATOR_UID,
            [PYTHON, "-c",
             "import os, sys\n"
             "try:\n"
             "    os.unlink(sys.argv[1]); print('UNLINKED')\n"
             "except PermissionError:\n"
             "    print('DENIED')\n",
             str(SOCKET_PATH)],
        )
        check(
            "coordinator cannot unlink the authority socket",
            socket_unlink.stdout.strip() == "DENIED",
            socket_unlink.stdout.strip(),
        )
        env_read = run_as_uid(
            WORKER_UID,
            [PYTHON, "-c",
             "import sys\n"
             "try:\n"
             "    open(sys.argv[1],'rb').read(1); print('READ')\n"
             "except PermissionError:\n"
             "    print('DENIED')\n",
             f"/proc/{health['pid']}/environ"],
        )
        check(
            "worker cannot read the authority process environment",
            env_read.stdout.strip() == "DENIED",
            env_read.stdout.strip(),
        )
        service_env = run_as_uid(
            SERVICE_UID,
            [PYTHON, "-c",
             "import sys\n"
             "data=open(sys.argv[1],'rb').read()\n"
             "names=(b'OPENAI_API_KEY',b'DEEPSEEK_API_KEY',b'GITHUB_TOKEN',"
             "b'AWS_SECRET_ACCESS_KEY',b'ORCHESTRA_')\n"
             "bad=[n.decode() for n in names if n in data]\n"
             "print('CLEAN' if not bad else 'BAD:' + ','.join(bad))\n",
             f"/proc/{health['pid']}/environ"],
        )
        check(
            "authority process carries no provider credentials",
            service_env.stdout.strip() == "CLEAN",
            service_env.stdout.strip() or service_env.stderr[-800:],
        )

        # -- full lifecycle and replay ------------------------------------
        task_b = make_task_object("task-b", "request-b")
        payload_b = task_payload("task-b", "request-b")
        proposal_b = expect_ok(
            probe(COORDINATOR_UID, "propose", {"task": payload_b}, "B-propose"),
            "proposal B registered",
        )
        replay_b = expect_ok(
            probe(COORDINATOR_UID, "propose", {"task": payload_b}, "B-propose"),
            "identical proposal request is idempotent",
        )
        check(
            "idempotent replay returns the same proposal",
            replay_b["proposal_digest"] == proposal_b["proposal_digest"],
        )
        expect_error(
            probe(
                COORDINATOR_UID, "propose",
                {"task": task_payload("task-b-other", "request-b")},
                "B-propose",
            ),
            "request_id_conflict",
            "conflicting replay is rejected",
        )
        expect_ok(
            probe(
                APPROVER_UID, "decide_approval",
                {"proposal_digest": proposal_b["proposal_digest"],
                 "decision": "approved"},
                "B-approve",
            ),
            "approver B decision recorded",
        )
        exec_payload = {
            "task": payload_b, "adapter_id": ADAPTER, "runtime": RUNTIME,
            "idempotency_key": "execution-b",
        }
        issued_b = expect_ok(
            probe(COORDINATOR_UID, "request_execution", exec_payload, "B-exec"),
            "coordinator obtains an audience-bound grant",
        )
        replay_issued_b = expect_ok(
            probe(COORDINATOR_UID, "request_execution", exec_payload, "B-exec"),
            "execution request replay is idempotent",
        )
        check(
            "grant replay returns the same grant",
            replay_issued_b["grant"]["grant_id"]
            == issued_b["grant"]["grant_id"],
        )
        expect_error(
            probe(
                COORDINATOR_UID, "request_execution",
                {**exec_payload, "idempotency_key": "execution-b-other"},
                "B-exec",
            ),
            "request_id_conflict",
            "execution request id cannot be reused for different parameters",
        )
        attempt_id = issued_b["attempt"]["attempt_id"]
        grant = issued_b["grant"]
        consume_payload = {
            "task": payload_b, "grant": grant,
            "adapter_id": ADAPTER, "runtime": RUNTIME,
        }
        consumed_b = expect_ok(
            probe(COORDINATOR_UID, "consume_grant", consume_payload, "B-consume"),
            "grant consumption succeeds exactly once",
        )
        replay_consumed = expect_ok(
            probe(COORDINATOR_UID, "consume_grant", consume_payload, "B-consume"),
            "same-request consumption replay is served from the response cache",
        )
        check(
            "cached consumption did not consume again",
            replay_consumed["consumed_at"] == consumed_b["consumed_at"],
        )
        expect_error(
            probe(COORDINATOR_UID, "consume_grant", consume_payload, "B-consume-new-rpc"),
            "grant_replay",
            "new request id replay of a consumed grant is rejected",
        )
        expect_ok(
            probe(
                WORKER_UID, "record_dispatch",
                {"attempt_id": attempt_id, "provider_run_id": "fake-run-task-b",
                 "adapter_id": ADAPTER, "runtime": RUNTIME},
                "B-dispatch",
            ),
            "worker records the provider-run identity",
        )
        reported_b = expect_ok(
            probe(
                WORKER_UID, "record_report",
                {"report": report_payload(task_b, attempt_id)}, "B-report",
            ),
            "worker report is accepted as untrusted evidence",
        )
        check(
            "worker success does not automatically verify",
            reported_b["state"] == "reported" and reported_b["verified"] is False,
            json.dumps(reported_b, sort_keys=True)[:800],
        )
        expect_error(
            probe(
                WORKER_UID, "record_verification",
                {"verification": verification_payload(task_b, attempt_id)},
                "B-worker-verify",
            ),
            "operation_not_permitted",
            "worker cannot forge verification",
        )
        expect_error(
            probe(
                APPROVER_UID, "record_verification",
                {"verification": verification_payload(task_b, attempt_id)},
                "B-approver-verify",
            ),
            "operation_not_permitted",
            "approver cannot also act as verifier",
        )
        decision_b = expect_ok(
            probe(
                VERIFIER_UID, "record_verification",
                {"verification": verification_payload(task_b, attempt_id)},
                "B-verify",
            ),
            "independent verifier records a passing result",
        )
        check(
            "worker success required independent verification",
            decision_b["applied"] is True
            and decision_b["final_state"] == "verified",
            json.dumps(decision_b, sort_keys=True)[:800],
        )
        final_b = expect_ok(
            probe(COORDINATOR_UID, "attempt", {"attempt_id": attempt_id}, "B-attempt"),
            "coordinator can read the final verified attempt",
        )
        check("verified attempt is durable", final_b["verified"] is True)

        # -- interrupted request recovery ---------------------------------
        task_c = make_task_object("task-c", "request-c")
        payload_c = task_payload("task-c", "request-c")
        proposal_c = expect_ok(
            probe(COORDINATOR_UID, "propose", {"task": payload_c}, "C-propose"),
            "proposal C registered",
        )
        expect_ok(
            probe(
                APPROVER_UID, "decide_approval",
                {"proposal_digest": proposal_c["proposal_digest"],
                 "decision": "approved"},
                "C-approve",
            ),
            "approver C decision recorded",
        )
        issued_c = expect_ok(
            probe(
                COORDINATOR_UID, "request_execution",
                {"task": payload_c, "adapter_id": ADAPTER, "runtime": RUNTIME,
                 "idempotency_key": "execution-c"},
                "C-exec",
            ),
            "grant C issued",
        )
        attempt_c = issued_c["attempt"]["attempt_id"]
        grant_c = issued_c["grant"]
        consume_c_payload = {
            "task": payload_c, "grant": grant_c,
            "adapter_id": ADAPTER, "runtime": RUNTIME,
        }
        consumed_c = expect_ok(
            probe(
                COORDINATOR_UID, "consume_grant", consume_c_payload,
                "C-consume",
            ),
            "grant C consumed before simulated crash",
        )

        # Simulate a crash between the durable consumption and the cached
        # response: stop the service and mark its RPC row in progress.
        check("service stops cleanly before recovery test", stop_service(proc) == 0)
        connection = sqlite3.connect(DB_PATH)
        connection.execute(
            "UPDATE rpc_requests SET status='in_progress', response_json=NULL "
            "WHERE peer_uid=? AND request_id='C-consume'",
            (COORDINATOR_UID,),
        )
        connection.commit()
        connection.close()
        proc = start_service_process()
        health = wait_for_ready(proc)
        check(
            "restart preserves the verified task",
            expect_ok(
                probe(
                    COORDINATOR_UID, "attempt",
                    {"attempt_id": attempt_id}, "B-attempt-after-restart",
                ),
                "verified attempt survives restart",
            )["verified"] is True,
        )
        expect_error(
            probe(
                COORDINATOR_UID, "consume_grant", consume_c_payload, "C-consume",
            ),
            "request_outcome_unknown",
            "interrupted request is not silently re-executed",
        )
        status_c = expect_ok(
            probe(
                COORDINATOR_UID, "request_status",
                {"target_request_id": "C-consume"}, "C-status",
            ),
            "request status is durable",
        )
        check(
            "interrupted request is marked unknown",
            status_c["status"] == "unknown",
            json.dumps(status_c, sort_keys=True)[:800],
        )
        attempt_c_state = expect_ok(
            probe(COORDINATOR_UID, "attempt", {"attempt_id": attempt_c}, "C-attempt"),
            "interrupted attempt remains queryable",
        )
        check(
            "interrupted consumption did not create ambiguous authorisation",
            attempt_c_state["state"] == "dispatching"
            and attempt_c_state["verified"] is False,
            json.dumps(attempt_c_state, sort_keys=True)[:800],
        )
        grant_c_state = expect_ok(
            probe(COORDINATOR_UID, "grant", {"grant_id": grant_c["grant_id"]}, "C-grant"),
            "interrupted grant remains consumed exactly once",
        )
        check(
            "grant consumption timestamp is unchanged",
            grant_c_state["consumed_at"] == consumed_c["consumed_at"],
            json.dumps(grant_c_state, sort_keys=True)[:800],
        )

        # -- second instance and socket replacement ----------------------
        second = start_service_process()
        try:
            second_code = second.wait(timeout=10)
        except subprocess.TimeoutExpired:
            second.kill()
            second_code = -9
        log = getattr(second, "_orchestra_log", None)
        if log is not None:
            log.close()
        check(
            "second authority instance is refused",
            second_code != 0,
            f"unexpected exit {second_code}\n{log_tail()}",
        )

        coordinator_second = probe(COORDINATOR_UID, "health")
        expect_ok(coordinator_second, "original service remains healthy")
        check("service stops cleanly after live-instance test", stop_service(proc) == 0)
        check("graceful shutdown removes the socket", not SOCKET_PATH.exists())
        check("graceful shutdown releases the lock", not LOCK_PATH.exists())

        # A regular file at the socket path must not be deleted.
        SOCKET_PATH.write_text("not-a-socket", encoding="utf-8")
        proc = start_service_process()
        code = proc.wait(timeout=10)
        log = getattr(proc, "_orchestra_log", None)
        if log is not None:
            log.close()
        check(
            "non-socket replacement is refused",
            code != 0 and SOCKET_PATH.read_text(encoding="utf-8") == "not-a-socket",
            f"exit={code}\n{log_tail()}",
        )
        SOCKET_PATH.unlink()

        # A symlink at the socket path must not be followed or deleted.
        target = Path("/tmp/authority-symlink-target")
        target.write_text("target", encoding="utf-8")
        SOCKET_PATH.symlink_to(target)
        proc = start_service_process()
        code = proc.wait(timeout=10)
        log = getattr(proc, "_orchestra_log", None)
        if log is not None:
            log.close()
        check(
            "symlink socket replacement is refused",
            code != 0 and SOCKET_PATH.is_symlink()
            and target.read_text(encoding="utf-8") == "target",
            f"exit={code}\n{log_tail()}",
        )
        SOCKET_PATH.unlink()
        target.unlink()

        # A stale socket left by an unclean exit is replaced.
        stale_code = (
            "import socket, sys; s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); "
            "s.bind(sys.argv[1]); s.close()"
        )
        result = run_as_uid(SERVICE_UID, [PYTHON, "-c", stale_code, str(SOCKET_PATH)])
        check(
            "stale socket fixture created by the authority identity",
            result.returncode == 0 and SOCKET_PATH.exists(),
            result.stderr[-500:],
        )
        proc = start_service_process()
        wait_for_ready(proc)
        check(
            "stale socket is replaced safely",
            proc.poll() is None and SOCKET_PATH.exists(),
        )
        # Simulate an unclean authority crash; the next start must recover.
        proc.kill()
        proc.wait(timeout=5)
        log = getattr(proc, "_orchestra_log", None)
        if log is not None:
            log.close()
        check("unclean crash leaves a stale socket fixture", SOCKET_PATH.exists())
        proc = start_service_process()
        wait_for_ready(proc)
        check(
            "restart after unclean crash recovers durable state",
            expect_ok(
                probe(COORDINATOR_UID, "attempt", {"attempt_id": attempt_id}),
                "durable attempt readable after crash restart",
            )["verified"] is True,
        )
        check("final service stops cleanly", stop_service(proc) == 0)
        check("final shutdown removes the socket", not SOCKET_PATH.exists())
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        log = getattr(proc, "_orchestra_log", None)
        if log is not None:
            try:
                log.close()
            except OSError:
                pass


def main() -> int:
    if not sys.platform.startswith("linux"):
        print("BLOCKED: the separate-UID boundary test requires Linux", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("BLOCKED: run this test as root inside a disposable Linux container",
              file=sys.stderr)
        return 2
    if SERVICE_LOG.exists():
        SERVICE_LOG.unlink()
    try:
        run_boundary_checks()
    except BoundaryFailure as error:
        print(f"\nBOUNDARY TEST FAILED: {error}", file=sys.stderr)
        print("\n--- authority log tail ---", file=sys.stderr)
        print(log_tail(80), file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001
        print(f"\nBOUNDARY TEST ERROR: {type(error).__name__}: {error}",
              file=sys.stderr)
        print("\n--- authority log tail ---", file=sys.stderr)
        print(log_tail(80), file=sys.stderr)
        return 1
    print(f"\nPASS: {len(PASSED)} separate-UID boundary checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
