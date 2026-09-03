"""`na-ops dashboard`: the whole tool on one local page, and nothing the CLI lacks.

Every read here is the function a CLI already calls — :func:`status_payload`, the three
review-queue listers, :func:`recent_runs`, ``PlayerCrosswalk`` — so the page cannot drift
from the terminal. The status page in particular renders the payload dict itself rather
than a hand-written selection of it, which means a section added to `na-ops status`
appears here the day it lands and no section can be quietly dropped.

Three deliberate constraints, in the spirit of the complexity budget (§1.6):

* the standard library only — ``http.server`` and one inline stylesheet. No JavaScript,
  no build step, no external asset, no CDN. The one dynamic behaviour on the page is a
  ``<meta http-equiv="refresh">`` that fires only while a lane is running.
* loopback only. The page shows the whole store — every unresolved name, every failure
  text — so it must not be reachable from the network, and a non-loopback bind is refused
  rather than quietly accepted.
* two write actions and one resolve, all POST, all confirmed on the page, and each one a
  call into the same library function the CLI uses.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote, urlsplit

from narrative_alpha.build_cli import DEFAULT_ARTIFACT_DIRECTORY
from narrative_alpha.identity import CrosswalkError, PlayerCrosswalk
from narrative_alpha.ingest.timestamps import ensure_utc, utc_timestamp
from narrative_alpha.narrative import (
    list_execution_leases,
    list_inflight_extractions,
    list_pending_review_flags,
)
from narrative_alpha.narrative.audit import (
    AuditError,
    list_audit_candidates,
    player_audit,
    resolve_audit_player,
)
from narrative_alpha.ops.batch import (
    DEFAULT_DEPENDENCIES,
    BatchDependencies,
    BatchReport,
    run_batch,
)
from narrative_alpha.ops.config import OpsConfig
from narrative_alpha.ops.runs import RecordedRun, StepOutcome, last_run, recent_runs
from narrative_alpha.ops.slate import (
    DEFAULT_SLATE_DEPENDENCIES,
    SlateDependencies,
    SlateReport,
    run_slate,
)
from narrative_alpha.ops.status import collect_ops_status, status_payload
from narrative_alpha.report_cli import DEFAULT_REPORT_DIRECTORY
from narrative_alpha.store import (
    UnresolvedPlayerMatchRow,
    apply_migrations,
    connect_database,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# How many `ops_runs` rows the history page shows. The store keeps every row; this is the
# window an operator reads at a glance, and `na-ops status` remains the full account.
RECENT_RUN_LIMIT = 20

# A form post from this page is a few hundred bytes. Anything larger is not this page.
MAX_REQUEST_BODY_BYTES = 64 * 1024

# The sites the slate lane accepts, in the order the CLI lists them.
SITES = ("dk", "fd")

LANES = ("batch", "slate")

# While a lane runs, the page refreshes itself so `ops_runs` rows appear as the steps
# commit them. HTML, not script: there is nothing here for a JavaScript engine to run.
RUNNING_REFRESH_SECONDS = 5


class DashboardError(RuntimeError):
    """The dashboard refused to start or refused a request, and can say why."""


class LaneBusyError(DashboardError):
    """A lane was asked to start while its own previous run is still going."""


class MisdirectedHostError(DashboardError):
    """A request reached this server under a name that is not its loopback name."""


@dataclass(frozen=True)
class LaneState:
    """What one lane is doing now, and what it did last."""

    lane: str
    running: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    run_id: str | None = None
    ok: bool | None = None
    detail: str | None = None


LaneAction = Callable[[], tuple[str, bool, str]]


class LaneRunner:
    """Runs one lane at a time in a background thread and remembers what it did.

    The refusal is the point. Two concurrent runs of one lane would interleave writes to
    the same rows and produce an `ops_runs` history no reader could untangle, so a second
    start is refused with the time the first one began rather than queued or ignored.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, LaneState] = {lane: LaneState(lane=lane) for lane in LANES}

    def state(self, lane: str) -> LaneState:
        with self._lock:
            return self._states[lane]

    def states(self) -> tuple[LaneState, ...]:
        with self._lock:
            return tuple(self._states[lane] for lane in LANES)

    @property
    def any_running(self) -> bool:
        return any(state.running for state in self.states())

    def start(self, lane: str, action: LaneAction) -> datetime:
        """Begin ``lane`` in its own thread, or refuse because it is already running."""

        if lane not in LANES:  # pragma: no cover - the routes never build another name
            raise DashboardError(f"unknown lane: {lane}")
        started_at = ensure_utc(datetime.now(UTC))
        with self._lock:
            current = self._states[lane]
            if current.running:
                since = "an unknown time" if current.started_at is None else utc_timestamp(
                    current.started_at
                )
                raise LaneBusyError(
                    f"the {lane} lane started at {since} and has not finished; a second "
                    "run would write the same rows at the same time. Wait for it, or read "
                    "its progress on the runs page"
                )
            self._states[lane] = LaneState(lane=lane, running=True, started_at=started_at)
        thread = threading.Thread(
            target=self._run,
            args=(lane, action, started_at),
            name=f"na-ops-{lane}",
            daemon=True,
        )
        thread.start()
        return started_at

    def _run(self, lane: str, action: LaneAction, started_at: datetime) -> None:
        run_id: str | None = None
        ok = False
        detail: str | None = None
        try:
            run_id, ok, detail = action()
        except Exception as error:
            # The lane records its own step failures in `ops_runs`; anything that escapes
            # it is a bug, and a bug must not leave this page saying "running" for ever.
            # It is stated here in full, loudly, and the lane is marked finished.
            detail = (
                f"the lane stopped before it finished: {type(error).__name__}: {error}\n"
                "Nothing after this point ran. The steps it did record are on the runs page."
            )
        finally:
            with self._lock:
                self._states[lane] = LaneState(
                    lane=lane,
                    running=False,
                    started_at=started_at,
                    finished_at=ensure_utc(datetime.now(UTC)),
                    run_id=run_id,
                    ok=ok,
                    detail=detail,
                )


BatchLane = Callable[..., BatchReport]
SlateLane = Callable[..., SlateReport]


@dataclass(frozen=True)
class DashboardDependencies:
    """The lane entry points and their own dependencies, injectable for tests.

    Defaults are the production functions; the page re-implements none of them.
    """

    run_batch: BatchLane = run_batch
    run_slate: SlateLane = run_slate
    batch_dependencies: BatchDependencies = DEFAULT_DEPENDENCIES
    slate_dependencies: SlateDependencies = DEFAULT_SLATE_DEPENDENCIES


DEFAULT_DASHBOARD_DEPENDENCIES = DashboardDependencies()


@dataclass(frozen=True)
class DashboardContext:
    """Everything a request needs; one instance lives on the server."""

    config: OpsConfig
    database: Path
    runner: LaneRunner
    dependencies: DashboardDependencies = DEFAULT_DASHBOARD_DEPENDENCIES
    artifact_directory: Path = DEFAULT_ARTIFACT_DIRECTORY
    report_directory: Path = DEFAULT_REPORT_DIRECTORY
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


class DashboardServer(ThreadingHTTPServer):
    """A loopback-only HTTP server carrying the dashboard's context."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        context: DashboardContext,
    ) -> None:
        self.context = context
        self.bound_host = address[0]
        # `_loopback_host` accepts ::1 as readily as 127.0.0.1, so the socket family has
        # to follow the address it was given; the AF_INET default would refuse to bind it.
        if isinstance(ipaddress.ip_address(address[0]), ipaddress.IPv6Address):
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)

    @property
    def port(self) -> int:
        """The port actually bound, which port 0 only decides at bind time."""

        return int(self.server_address[1])

    @property
    def url(self) -> str:
        host = self.bound_host
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            host = f"[{host}]"
        return f"http://{host}:{self.port}/"


def build_dashboard(
    *,
    config: OpsConfig,
    database: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    dependencies: DashboardDependencies = DEFAULT_DASHBOARD_DEPENDENCIES,
    artifact_directory: Path = DEFAULT_ARTIFACT_DIRECTORY,
    report_directory: Path = DEFAULT_REPORT_DIRECTORY,
    clock: Callable[[], datetime] | None = None,
) -> DashboardServer:
    """Bind the dashboard to a loopback address, or refuse and say why.

    The schema is brought up to date once, here, rather than on every page view: a page
    view is a read, and a read should never wait on the write lock a running lane holds.
    """

    bind_host = _loopback_host(host)
    with connect_database(database) as connection:
        apply_migrations(connection)
    context = DashboardContext(
        config=config,
        database=database,
        runner=LaneRunner(),
        dependencies=dependencies,
        artifact_directory=artifact_directory,
        report_directory=report_directory,
        clock=clock or (lambda: datetime.now(UTC)),
    )
    return DashboardServer((bind_host, port), DashboardHandler, context=context)


def serve_dashboard(server: DashboardServer) -> None:
    """Serve until interrupted, then close the socket."""

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _loopback_host(host: str) -> str:
    """Accept only an address on this machine's loopback interface.

    The page renders the whole store — unresolved player names, failure texts, file paths
    — and offers three writes with no authentication of any kind. Its only defence is that
    nothing off this machine can reach it, so a bind that would break that is refused
    rather than served.
    """

    candidate = "127.0.0.1" if host == "localhost" else host
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as error:
        raise DashboardError(
            f"cannot bind {host!r}: the dashboard binds a loopback IP address "
            "(127.0.0.1, ::1, or localhost) and nothing else"
        ) from error
    if not address.is_loopback:
        raise DashboardError(
            f"refusing to bind {host}: the dashboard serves the whole store and runs the "
            "lanes with no authentication, so it is loopback-only. Use 127.0.0.1, and "
            "reach it from another machine over an SSH tunnel if you must"
        )
    return candidate


# --------------------------------------------------------------------------------------
# Request handling
# --------------------------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    """Five read pages, three POST actions, and a 404 that names the way back."""

    server_version = "na-ops-dashboard"

    @property
    def context(self) -> DashboardContext:
        return cast("DashboardServer", self.server).context

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            self._check_host()
            if path == "/":
                self._html(_status_page(self.context))
            elif path == "/queues":
                self._html(_queues_page(self.context))
            elif path == "/runs":
                self._html(_runs_page(self.context))
            elif path == "/memo":
                self._html(_memo_page(self.context))
            elif path == "/audit":
                self._html(_audit_page(self.context, parse_qs(urlsplit(self.path).query)))
            elif path == "/favicon.ico":
                # The page has no icon, and a 404 HTML body for every page view would
                # bury the request log this tool is read through.
                self._empty(HTTPStatus.NO_CONTENT)
            else:
                self._html(_not_found_page(path), status=HTTPStatus.NOT_FOUND)
        except MisdirectedHostError as error:
            self._html(
                _problem_page("The request was refused", str(error)),
                status=HTTPStatus.MISDIRECTED_REQUEST,
            )
        except AuditError as error:
            self._html(
                _problem_page("The audit could not be read", str(error)),
                status=HTTPStatus.BAD_REQUEST,
            )
        except DASHBOARD_REQUEST_ERRORS as error:
            self._html(
                _problem_page("This page could not be read", str(error)),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            self._check_host()
            self._check_origin()
            form = self._read_form()
            _require_confirmation(form)
            if path == "/actions/batch":
                _start_batch(self.context, form)
                self._redirect("/")
            elif path == "/actions/slate":
                _start_slate(self.context, form)
                self._redirect("/")
            elif path == "/queues/resolve":
                _resolve_identity(self.context, form)
                # Back to the queue, at the section: the row's absence is the receipt.
                self._redirect("/queues#unresolved")
            else:
                self._html(_not_found_page(path), status=HTTPStatus.NOT_FOUND)
        except LaneBusyError as error:
            self._html(_problem_page("That lane is already running", str(error)), status=409)
        except MisdirectedHostError as error:
            self._html(
                _problem_page("The request was refused", str(error)),
                status=HTTPStatus.MISDIRECTED_REQUEST,
            )
        except (CrosswalkError, DashboardError, ValueError) as error:
            self._html(
                _problem_page("The action was refused", str(error)),
                status=HTTPStatus.BAD_REQUEST,
            )
        except DASHBOARD_REQUEST_ERRORS as error:
            self._html(
                _problem_page("The action failed", f"{type(error).__name__}: {error}"),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _check_host(self) -> None:
        """Refuse a request whose ``Host`` names anything but this loopback server.

        The origin check below compares ``Origin`` with ``Host``, and a browser fills in
        both from the name it navigated to. A hostile page can arrange for its own name to
        resolve to 127.0.0.1 after it has loaded (DNS rebinding); the browser then talks
        to this server as if it were that page's origin, reads every page, and the two
        headers agree. The name is the tell: nothing legitimate reaches this server by any
        name but the loopback ones, so any other name is refused before the path is read.
        """

        raw = self.headers.get("Host")
        if raw is None:
            raise MisdirectedHostError(
                "refusing a request that named no Host: every browser sends one, and this "
                "page trusts the loopback name it is served under and nothing else"
            )
        host, port = _split_host(raw)
        bound_port = cast("DashboardServer", self.server).port
        if _is_loopback_name(host) and port == bound_port:
            return
        raise MisdirectedHostError(
            f"refusing a request addressed to {raw!r}: this server answers only to "
            f"127.0.0.1:{bound_port}, localhost:{bound_port}, or [::1]:{bound_port}. A "
            "browser that reached it under another name resolved that name to this "
            "machine, which is what a rebinding attack looks like. Open "
            f"http://127.0.0.1:{bound_port}/ by that address"
        )

    def _check_origin(self) -> None:
        """Refuse a form post that another origin sent to this port.

        A page on the wider web cannot read this one, but it can post a form to
        ``localhost``. Browsers label such a post with an ``Origin`` header, and every
        post from this page carries our own origin, so a mismatch is never legitimate.
        """

        origin = self.headers.get("Origin")
        if origin is None:
            return
        expected = f"http://{self.headers.get('Host', '')}"
        if origin == expected:
            return
        if origin == "null":
            # An opaque origin: the page is inside a sandboxed frame, a `data:` URL, or
            # was opened from the filesystem. A hostile page embedding this one sends the
            # same thing, so it cannot be waved through — say what an operator should do.
            raise DashboardError(
                "refusing a form posted from an opaque origin. This page was not opened "
                "directly, so the browser will not say where the form came from — and a "
                f"hostile page embedding this one looks identical. Open {expected}/ in a "
                "browser tab of its own and submit the form there"
            )
        raise DashboardError(
            f"refusing a form posted from {origin}: this page accepts only its own forms, "
            f"and it is served at {expected}. Nothing else should be posting to it"
        )

    def _read_form(self) -> Mapping[str, list[str]]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or 0)
        except ValueError as error:
            raise DashboardError("the request had no readable Content-Length") from error
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            raise DashboardError(
                f"the request body is {length} bytes; this page posts a form, and a form "
                f"from it is never larger than {MAX_REQUEST_BODY_BYTES} bytes"
            )
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(body, keep_blank_values=True)

    def _html(self, body: str, *, status: HTTPStatus | int = HTTPStatus.OK) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # The page loads no third-party anything; say so, so a browser enforces it too.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


# What a request may raise while reading the store or the filesystem: a bad state, not a
# broken program. Anything else propagates and http.server logs it as the bug it is.
DASHBOARD_REQUEST_ERRORS: tuple[type[BaseException], ...] = (OSError, ValueError)


def _split_host(raw: str) -> tuple[str, int | None]:
    """The name and the port of a ``Host`` header; an IPv6 literal keeps its brackets off."""

    value = raw.strip()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        host, rest = value[1:end], value[end + 1 :]
    else:
        host, _, rest = value.partition(":")
        rest = f":{rest}" if rest else ""
    if not rest:
        return host, 80
    if not rest.startswith(":") or not rest[1:].isdigit():
        return host, None
    return host, int(rest[1:])


def _is_loopback_name(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_confirmation(form: Mapping[str, list[str]]) -> None:
    if _one(form, "confirm") != "yes":
        raise DashboardError(
            "the action was not confirmed. Every write from this page needs its "
            "confirmation box ticked, so a stray click or a stale tab cannot run a lane "
            "or decide an identity"
        )


# --------------------------------------------------------------------------------------
# Actions — each one call into the library function the CLI calls
# --------------------------------------------------------------------------------------


def _start_batch(context: DashboardContext, form: Mapping[str, list[str]]) -> None:
    max_items = _optional_positive(form, "max_items")

    def action() -> tuple[str, bool, str]:
        with connect_database(context.database) as connection:
            apply_migrations(connection)
            report = context.dependencies.run_batch(
                connection,
                config=context.config,
                max_items=max_items or context.config.batch_max_items_per_run,
                dependencies=context.dependencies.batch_dependencies,
            )
        return report.batch_run_id, report.ok, _steps_detail(report.steps)

    context.runner.start("batch", action)


def _start_slate(context: DashboardContext, form: Mapping[str, list[str]]) -> None:
    season = _required_positive(form, "season")
    week = _required_positive(form, "week")
    site = _one(form, "site")
    if site not in SITES:
        raise DashboardError(f"site must be one of {', '.join(SITES)}, not {site!r}")
    lineups = _optional_positive(form, "lineups") or 1

    def action() -> tuple[str, bool, str]:
        with connect_database(context.database) as connection:
            apply_migrations(connection)
            report = context.dependencies.run_slate(
                connection,
                config=context.config,
                database=context.database,
                season=season,
                week=week,
                site=site,
                number_of_lineups=lineups,
                artifact_directory=context.artifact_directory,
                report_directory=context.report_directory,
                dependencies=context.dependencies.slate_dependencies,
            )
        return report.slate_run_id, report.ok, _steps_detail(report.steps)

    context.runner.start("slate", action)


def _resolve_identity(context: DashboardContext, form: Mapping[str, list[str]]) -> None:
    """Decide one unresolved identity exactly as `na-crosswalk resolve` decides it."""

    unresolved_id = _required_positive(form, "unresolved_id")
    decision = _one(form, "decision")
    note = _one(form, "note").strip() or None
    with connect_database(context.database) as connection:
        crosswalk = PlayerCrosswalk(connection)
        if decision == "ignore":
            crosswalk.ignore(unresolved_id, note=note)
        elif decision == "resolve":
            crosswalk.resolve(unresolved_id, _resolved_player_id(form), note=note)
        else:
            raise DashboardError(
                f"decision must be 'resolve' or 'ignore', not {decision!r}; use the "
                "buttons on the queues page"
            )


def _resolved_player_id(form: Mapping[str, list[str]]) -> int:
    """The chosen candidate, or the id typed in when the right player was not offered."""

    typed = _optional_positive(form, "other_player_id")
    if typed is not None:
        return typed
    chosen = _one(form, "player_id")
    if not chosen:
        raise DashboardError(
            "resolving needs a canonical player: choose one of the candidates, or type "
            "the player id when the right player is not among them"
        )
    return _positive(chosen, "player_id")


def _steps_detail(steps: tuple[StepOutcome, ...]) -> str:
    return "\n".join(
        f"{step.step} {step.status} in "
        f"{(step.finished_at - step.started_at).total_seconds():.1f}s"
        + ("" if step.error_text is None else f" — {step.error_text.splitlines()[0]}")
        for step in steps
    )


def _one(form: Mapping[str, list[str]], name: str) -> str:
    values = form.get(name) or []
    return values[0] if values else ""


def _optional_positive(form: Mapping[str, list[str]], name: str) -> int | None:
    raw = _one(form, name).strip()
    return None if not raw else _positive(raw, name)


def _required_positive(form: Mapping[str, list[str]], name: str) -> int:
    value = _optional_positive(form, name)
    if value is None:
        raise DashboardError(f"{name} is required and was not sent")
    return value


def _positive(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise DashboardError(f"{name} must be a whole number, not {raw!r}") from error
    if value <= 0:
        raise DashboardError(f"{name} must be positive, not {value}")
    return value


# --------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------

STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0;
       padding: 0 0 3rem; }
header { padding: 1rem 1.5rem; border-bottom: 2px solid currentColor; }
h1 { font-size: 1.1rem; margin: 0 0 .35rem; letter-spacing: .06em; }
nav a { margin-right: 1.25rem; }
main { padding: 0 1.5rem; max-width: 78rem; }
section { margin: 1.75rem 0; }
h2 { font-size: .95rem; letter-spacing: .08em; text-transform: uppercase;
     border-bottom: 1px solid currentColor; padding-bottom: .25rem; }
h3 { font-size: .9rem; margin: 1rem 0 .35rem; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0; }
th, td { text-align: left; vertical-align: top; padding: .3rem .6rem .3rem 0;
         border-bottom: 1px solid rgba(128,128,128,.35); }
th { font-weight: 600; white-space: nowrap; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: .15rem 1.25rem;
     margin: .5rem 0; }
dt { font-weight: 600; }
dd { margin: 0; }
pre { white-space: pre-wrap; word-break: break-word; margin: .35rem 0;
      padding: .6rem .8rem; border: 1px solid rgba(128,128,128,.45); }
form { margin: .5rem 0; }
fieldset { border: 1px solid rgba(128,128,128,.55); padding: .75rem 1rem; margin: .75rem 0; }
legend { font-weight: 600; padding: 0 .35rem; }
label { display: inline-block; margin-right: 1rem; }
button { font: inherit; padding: .3rem .9rem; margin-right: .5rem; }
input, select { font: inherit; }
.none { opacity: .6; }
.running { font-weight: 600; }
.failed { font-weight: 600; }
.card { border: 1px solid rgba(128,128,128,.55); padding: .75rem 1rem; margin: .75rem 0; }
.note { opacity: .8; margin: .35rem 0; }
"""

NAV = (
    ("/", "status"),
    ("/queues", "review queues"),
    ("/runs", "run history"),
    ("/memo", "latest memo"),
)


def _page(title: str, body: str, *, refresh_seconds: int | None = None) -> str:
    refresh = (
        ""
        if refresh_seconds is None
        else f'<meta http-equiv="refresh" content="{refresh_seconds}">'
    )
    links = " ".join(f'<a href="{href}">{escape(label)}</a>' for href, label in NAV)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{refresh}<title>{escape(title)}</title><style>{STYLE}</style></head><body>"
        f"<header><h1>NARRATIVE ALPHA — {escape(title.upper())}</h1>"
        f"<nav>{links}</nav></header><main>{body}</main></body></html>\n"
    )


def _status_page(context: DashboardContext) -> str:
    with connect_database(context.database) as connection:
        status = collect_ops_status(
            connection,
            config=context.config,
            database=context.database,
            now=context.clock(),
        )
    payload = status_payload(status)
    week = status.snapshot_week
    body = [
        _lane_section(context),
        _actions_section(
            context,
            season=None if week is None else week.season,
            week=None if week is None else week.week,
        ),
    ]
    # Rendered from the payload itself, so a section added to `na-ops status` shows up
    # here without anyone remembering to add it, and none can be silently dropped.
    body.extend(
        f"<section><h2>{escape(_label(key))}</h2>{_render(value)}</section>"
        for key, value in payload.items()
    )
    return _page(
        "operator status",
        "".join(body),
        refresh_seconds=RUNNING_REFRESH_SECONDS if context.runner.any_running else None,
    )


def _lane_section(context: DashboardContext) -> str:
    rows = []
    for state in context.runner.states():
        if state.running:
            since = "" if state.started_at is None else utc_timestamp(state.started_at)
            outcome = f'<span class="running">RUNNING since {escape(since)}</span>'
        elif state.finished_at is None:
            outcome = '<span class="none">not started from this page</span>'
        else:
            word = "all steps ok" if state.ok else "FAILED"
            css = "" if state.ok else ' class="failed"'
            outcome = (
                f"<span{css}>{word}</span> at {escape(utc_timestamp(state.finished_at))}"
                f" — run {escape(state.run_id or 'not recorded')}"
            )
        detail = "" if not state.detail else f"<pre>{escape(state.detail)}</pre>"
        rows.append(f"<tr><th>{escape(state.lane)}</th><td>{outcome}{detail}</td></tr>")
    note = (
        '<p class="note">This page reports only the lanes started from it; every lane run, '
        "however started, is on the run history page.</p>"
    )
    return f"<section><h2>Lanes</h2><table>{''.join(rows)}</table>{note}</section>"


def _actions_section(
    context: DashboardContext,
    *,
    season: int | None,
    week: int | None,
) -> str:
    batch = f"""
    <form method="post" action="/actions/batch">
      <fieldset><legend>Run the batch lane now</legend>
        <p class="note">Collect, purge, extract, and check the roster refresh — exactly
        what <code>na-ops batch</code> runs, and it may submit items to Stage 1 and spend
        against the monthly budget.</p>
        <label>max items <input type="number" name="max_items" min="1" step="1"
          placeholder="{escape(str(context.config.batch_max_items_per_run or 'config'))}"></label>
        <label><input type="checkbox" name="confirm" value="yes"> yes, run it</label>
        <button type="submit">Run batch now</button>
      </fieldset>
    </form>
    """
    if season is None or week is None:
        slate = """
    <fieldset><legend>Run the slate lane now</legend>
      <p class="note">No snapshot week is initialized, so there is no current week to run.
      Capture this week's snapshots first (<code>na-snapshot</code>), or run
      <code>na-ops slate --season N --week N --site dk</code> from a terminal to name the
      week yourself. This page will not guess one.</p>
    </fieldset>
        """
        return f"<section><h2>Actions</h2>{batch}{slate}</section>"
    sites = "".join(f'<option value="{site}">{site}</option>' for site in SITES)
    slate = f"""
    <form method="post" action="/actions/slate">
      <fieldset><legend>Run the slate lane now</legend>
        <p class="note">Ingest the captures, build episodes and features at this instant,
        freeze the decision, write the memo — exactly what <code>na-ops slate</code> runs,
        for {season} week {week:02d}, the newest initialized snapshot week.</p>
        <input type="hidden" name="season" value="{season}">
        <input type="hidden" name="week" value="{week}">
        <label>site <select name="site">{sites}</select></label>
        <label>lineups <input type="number" name="lineups" min="1" step="1" value="1"></label>
        <label><input type="checkbox" name="confirm" value="yes"> yes, run it</label>
        <button type="submit">Run slate now</button>
      </fieldset>
    </form>
    """
    return f"<section><h2>Actions</h2>{batch}{slate}</section>"


def _queues_page(context: DashboardContext) -> str:
    with connect_database(context.database) as connection:
        flags = list_pending_review_flags(connection)
        inflight = list_inflight_extractions(connection)
        leases = list_execution_leases(connection)
        unresolved = PlayerCrosswalk(connection).list_unresolved()
        player_rows = int(connection.execute("SELECT count(*) FROM players").fetchone()[0])
    sections = [
        f"<section><h2>Pending review flags</h2>{_render(list(flags))}</section>",
        f"<section><h2>Extraction attempts in flight</h2>{_render(list(inflight))}"
        '<p class="note">A stuck attempt is cleared with '
        "<code>na-extract abandon --extraction-id &lt;id&gt; --reason '&lt;why&gt;'</code>."
        "</p></section>",
        f"<section><h2>Held execution leases</h2>{_render(list(leases))}"
        '<p class="note">A held lease whose owner run is still <code>running</code> with no '
        "live process is released with <code>na-extract release --run-id &lt;id&gt;</code>."
        "</p></section>",
        _unresolved_section(unresolved, player_rows=player_rows),
    ]
    return _page("review queues", "".join(sections))


def _unresolved_section(
    unresolved: tuple[UnresolvedPlayerMatchRow, ...],
    *,
    player_rows: int,
) -> str:
    if not unresolved:
        return (
            '<section id="unresolved"><h2>Unresolved identities</h2>'
            "<p>None. Nothing is blocking a build on identity.</p></section>"
        )
    cards = "".join(_unresolved_card(row, player_rows=player_rows) for row in unresolved)
    note = (
        '<p class="note">Each decision here is the same call '
        "<code>na-crosswalk resolve</code> makes: an alias and an external-id mapping, "
        "recorded as a manual override.</p>"
    )
    return (
        f'<section id="unresolved"><h2>Unresolved identities ({len(unresolved)})</h2>'
        f"{note}{cards}</section>"
    )


def _unresolved_card(row: UnresolvedPlayerMatchRow, *, player_rows: int) -> str:
    site = "" if row.site is None else escape(f" ({row.site})")
    seen = (
        f"once, at {escape(utc_timestamp(row.first_observed_at))}"
        if row.occurrences == 1
        else f"{row.occurrences} times, between "
        f"{escape(utc_timestamp(row.first_observed_at))} and "
        f"{escape(utc_timestamp(row.last_observed_at))}"
    )
    if row.candidates_json:
        options = "".join(
            f'<option value="{int(candidate["player_id"])}">'
            f"{escape(_candidate_label(candidate))}</option>"
            for candidate in row.candidates_json
        )
        chooser = f'<label>candidate <select name="player_id">{options}</select></label>'
    elif player_rows == 0:
        chooser = (
            '<p class="note">No candidate is offered because no canonical player is '
            "seeded at all. Seed the nflverse roster (<code>na-crosswalk seed</code>) "
            "before deciding anything here.</p>"
        )
    else:
        chooser = (
            '<p class="note">No candidate scored well enough to offer. Look the player up '
            "and type the canonical player id below, or ignore the row.</p>"
        )
    return f"""
    <div class="card">
      <h3>{escape(row.name_raw)} — {escape(row.team)} {escape(row.position or "?")}</h3>
      <dl>
        <dt>unresolved id</dt><dd>{row.unresolved_id}</dd>
        <dt>source</dt><dd>{escape(row.source)}{site}</dd>
        <dt>seen</dt><dd>{seen}</dd>
      </dl>
      <form method="post" action="/queues/resolve">
        <input type="hidden" name="unresolved_id" value="{row.unresolved_id}">
        {chooser}
        <label>or player id <input type="number" name="other_player_id" min="1" step="1"
          placeholder="not listed"></label>
        <label>note <input type="text" name="note" maxlength="200"></label>
        <label><input type="checkbox" name="confirm" value="yes"> yes, record it</label>
        <button type="submit" name="decision" value="resolve">Resolve</button>
        <button type="submit" name="decision" value="ignore">Ignore</button>
      </form>
    </div>
    """


def _candidate_label(candidate: Mapping[str, object]) -> str:
    parts = [
        f"{candidate.get('player_id')}",
        f"{candidate.get('canonical_name')}",
        f"({candidate.get('team')} {candidate.get('position') or '?'})",
    ]
    score = candidate.get("score")
    if isinstance(score, int | float):
        parts.append(f"score {float(score):.2f}")
    return " ".join(parts)


def _runs_page(context: DashboardContext) -> str:
    with connect_database(context.database) as connection:
        runs = recent_runs(connection, limit=RECENT_RUN_LIMIT)
    if not runs:
        body = (
            "<section><h2>Run history</h2><p>No lane has recorded a step yet. "
            "Run <code>na-ops batch</code> or <code>na-ops slate</code>, from the status "
            "page or a terminal.</p></section>"
        )
        return _page("run history", body)
    rows = "".join(_run_row(run) for run in runs)
    header = (
        "<tr><th>started</th><th>step</th><th>status</th><th>seconds</th>"
        "<th>run</th><th>detail</th></tr>"
    )
    note = (
        f'<p class="note">The last {RECENT_RUN_LIMIT} recorded steps of all lanes, newest '
        "first. The store keeps every row; this is the window, not the whole history.</p>"
    )
    return _page(
        "run history",
        f"<section><h2>Run history</h2>{note}<table>{header}{rows}</table></section>",
        refresh_seconds=RUNNING_REFRESH_SECONDS if context.runner.any_running else None,
    )


def _run_row(run: RecordedRun) -> str:
    seconds = (run.finished_at - run.started_at).total_seconds()
    css = ' class="failed"' if run.status == "failed" else ""
    detail = "" if run.error_text is None else f"<pre>{escape(run.error_text)}</pre>"
    summary = (
        ""
        if not run.summary
        else f"<pre>{escape(json.dumps(run.summary, indent=2, sort_keys=True))}</pre>"
    )
    return (
        f"<tr><td>{escape(utc_timestamp(run.started_at))}</td>"
        f"<td>{escape(run.step)}</td>"
        f"<td{css}>{escape(run.status)}</td>"
        f"<td>{seconds:.1f}</td>"
        f"<td>{escape(run.batch_run_id)}</td>"
        f"<td>{detail}{summary}</td></tr>"
    )


def _memo_page(context: DashboardContext) -> str:
    with connect_database(context.database) as connection:
        run = last_run(connection, step="slate_memo", status="succeeded")
    if run is None:
        return _page(
            "latest memo",
            "<section><h2>Latest slate memo</h2><p>No memo has been written. "
            "<code>na-ops slate</code> writes one as its last step, and it has not "
            "succeeded yet.</p></section>",
        )
    raw_path = run.summary.get("memo_path")
    if not isinstance(raw_path, str) or not raw_path:
        return _page(
            "latest memo",
            "<section><h2>Latest slate memo</h2><p>The last successful memo step recorded "
            f"no path in its summary (run <code>{escape(run.batch_run_id)}</code>). "
            "Nothing here can point at a file it was never told about.</p></section>",
        )
    path = Path(raw_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return _page(
            "latest memo",
            "<section><h2>Latest slate memo</h2>"
            f"<p>The memo was written to <code>{escape(str(path))}</code> at "
            f"{escape(utc_timestamp(run.finished_at))}, and it cannot be read now: "
            f"{escape(str(error))}</p></section>",
        )
    decision = run.summary.get("decision_snapshot_id")
    head = (
        "<dl>"
        f"<dt>written</dt><dd>{escape(utc_timestamp(run.finished_at))}</dd>"
        f"<dt>decision</dt><dd>{escape(str(decision or 'not recorded'))}</dd>"
        f"<dt>file</dt><dd>{escape(str(path))}</dd>"
        "</dl>"
    )
    audit_link = (
        ""
        if not isinstance(decision, str) or not decision
        else (
            "<p><a href=\"/audit?decision="
            f"{escape(quote(decision, safe=''))}\">signal and evidence audit for this "
            "decision</a> — every episode, claim, and excerpt behind one player's "
            "ownership number.</p>"
        )
    )
    return _page(
        "latest memo",
        f"<section><h2>Latest slate memo</h2>{head}{audit_link}"
        f"<pre>{escape(text)}</pre></section>",
    )


def _audit_page(context: DashboardContext, query: Mapping[str, list[str]]) -> str:
    """`/audit?decision=<id>&player=<id|name>` — the same model `na-report signals` renders.

    Read-only, like every GET here: it calls :func:`player_audit` and renders what comes
    back. With no player it lists the decision's candidates, because an operator arrives
    here from the memo knowing the decision and not yet the player id.
    """

    decision = _query_value(query, "decision")
    named_by_caller = decision is not None
    with connect_database(context.database) as connection:
        if decision is None:
            run = last_run(connection, step="slate_memo", status="succeeded")
            raw = None if run is None else run.summary.get("decision_snapshot_id")
            decision = raw if isinstance(raw, str) and raw else None
        if decision is None:
            return _page(
                "signal audit",
                "<section><h2>Signal and evidence audit</h2><p>No decision was named and "
                "no memo step has recorded one. Open this page from the "
                '<a href="/memo">latest memo</a>, or add '
                "<code>?decision=&lt;decision_snapshot_id&gt;</code>.</p></section>",
            )
        selector = _query_value(query, "player")
        if selector is None:
            try:
                return _page("signal audit", _audit_player_index(connection, decision))
            except AuditError as error:
                if named_by_caller:
                    raise
                # The memo step pointed at a decision the store no longer holds. The
                # caller asked for nothing wrong, so say what happened instead of 400.
                return _page(
                    "signal audit",
                    "<section><h2>Signal and evidence audit</h2><p>The latest memo names "
                    f"decision <code>{escape(decision)}</code>, and the store cannot audit "
                    f"it: {escape(str(error))}. Add <code>?decision=&lt;id&gt;</code> to "
                    "audit another.</p></section>",
                )
        # An unknown decision or player, or an ambiguous name, is the caller's error and
        # is answered as one: `do_GET` turns an AuditError into a 400 problem page.
        player_id = resolve_audit_player(
            connection, selector=selector, decision_snapshot_id=decision
        )
        audit = player_audit(connection, player_id=player_id, decision_snapshot_id=decision)

    payload = audit.model_dump(mode="json")
    ownership = audit.ownership
    head = (
        "<dl>"
        f"<dt>player</dt><dd>{escape(audit.player_name)} "
        f"(id {audit.player_id})</dd>"
        f"<dt>decision</dt><dd>{escape(audit.decision_snapshot_id)}</dd>"
        f"<dt>as of</dt><dd>{escape(utc_timestamp(audit.decision_at))}</dd>"
        f"<dt>slate</dt><dd>{audit.slate_id} {escape(audit.site)} "
        f"{escape(audit.slate_type)} {audit.season} week {audit.week:02d}</dd>"
        "<dt>ownership source</dt><dd>"
        + ("scenario model" if ownership.applied else "vendor baseline")
        + "</dd>"
        f"<dt>why</dt><dd>{escape(ownership.reason)}</dd>"
        "</dl>"
    )
    # Rendered from the model itself, section by section, so a field added to the audit
    # appears here without anyone remembering to add it and none can be quietly dropped.
    sections = "".join(
        f"<section><h2>{escape(_label(key))}</h2>{_render(payload[key])}</section>"
        for key in ("ownership", "features", "episodes", "notes")
    )
    return _page(
        "signal audit",
        f"<section><h2>Signal and evidence audit</h2>{head}"
        f'<p><a href="/audit?decision={escape(quote(audit.decision_snapshot_id, safe=""))}">'
        "another player on this decision</a> · "
        '<a href="/memo">back to the memo</a></p></section>' + sections,
    )


def _audit_player_index(connection: sqlite3.Connection, decision: str) -> str:
    """List the decision's salaried players as links; naming no player is not an error."""

    rows = list_audit_candidates(connection, decision_snapshot_id=decision)
    if not rows:
        return (
            "<section><h2>Signal and evidence audit</h2><p>Decision "
            f"<code>{escape(decision)}</code> has no salaried player visible at its "
            "cutoff, so there is nobody to audit.</p></section>"
        )
    encoded = escape(quote(decision, safe=""))
    items = "".join(
        f'<li><a href="/audit?decision={encoded}&amp;player={player_id}">'
        f"{escape(name)}</a> {escape(position or '')}</li>"
        for player_id, name, position in rows
    )
    return (
        "<section><h2>Signal and evidence audit</h2>"
        f"<p>Decision <code>{escape(decision)}</code>. Choose a player to see every "
        "episode, claim, excerpt, and feature value behind their ownership number.</p>"
        f"<ul>{items}</ul></section>"
    )


def _query_value(query: Mapping[str, list[str]], key: str) -> str | None:
    """The first non-blank value for a query key, or ``None`` — absence is not an error."""

    value = _one(query, key).strip()
    return value or None


def _not_found_page(path: str) -> str:
    links = "".join(f'<li><a href="{href}">{escape(label)}</a></li>' for href, label in NAV)
    return _page(
        "not found",
        f"<section><h2>No such page</h2><p>There is no <code>{escape(path)}</code>. "
        f"The dashboard has these pages:</p><ul>{links}"
        '<li><a href="/audit">signal and evidence audit</a> — reached from a memo, for '
        "one player at one decision</li></ul></section>",
    )


def _problem_page(title: str, detail: str) -> str:
    return _page(
        "refused",
        f"<section><h2>{escape(title)}</h2><pre>{escape(detail)}</pre>"
        '<p><a href="/">back to the status page</a></p></section>',
    )


# --------------------------------------------------------------------------------------
# Generic rendering of the status payload
# --------------------------------------------------------------------------------------


def _label(key: str) -> str:
    return key.replace("_", " ")


def _render(value: object) -> str:
    """Render a JSON-shaped value. Nothing is summarized away and nothing is truncated."""

    if value is None:
        return '<span class="none">none</span>'
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return _render_mapping(value)
    if isinstance(value, list):
        return _render_list(value)
    text = str(value)
    return f"<pre>{escape(text)}</pre>" if "\n" in text else escape(text)


def _render_mapping(value: Mapping[str, object]) -> str:
    if not value:
        return '<span class="none">empty</span>'
    items = "".join(
        f"<dt>{escape(_label(key))}</dt><dd>{_render(item)}</dd>" for key, item in value.items()
    )
    return f"<dl>{items}</dl>"


def _render_list(value: list[object]) -> str:
    if not value:
        return '<span class="none">none</span>'
    if all(isinstance(item, dict) for item in value):
        return _render_table([cast("dict[str, object]", item) for item in value])
    items = "".join(f"<li>{_render(item)}</li>" for item in value)
    return f"<ul>{items}</ul>"


def _render_table(rows: list[dict[str, object]]) -> str:
    columns: list[str] = []
    for row in rows:
        columns.extend(key for key in row if key not in columns)
    header = "".join(f"<th>{escape(_label(column))}</th>" for column in columns)
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{_render(row[column]) if column in row else '<span class=\"none\">—</span>'}</td>"
            for column in columns
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><tr>{header}</tr>{body}</table>"
