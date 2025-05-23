from __future__ import annotations

import asyncio
import pathlib
import signal
import sys
import threading

import click

from .. import utils
from ..log import logger
from ..plugin import Plugin
from ..types import NOT_GIVEN, NotGivenOr
from ..worker import JobExecutorType, Worker, WorkerOptions
from . import proto
from .log import setup_logging

CLI_ARGUMENTS: proto.CliArgs | None = None


def _esc(*codes: int) -> str:
    return "\033[" + ";".join(str(c) for c in codes) + "m"


def run_app(opts: WorkerOptions, *, hot_reload: NotGivenOr[bool] = NOT_GIVEN) -> None:
    """Run the CLI to interact with the worker"""
    cli = click.Group()

    @cli.command(help="Start the worker in production mode.")
    @click.option(
        "--log-level",
        default="INFO",
        type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
        help="Set the logging level",
    )
    @click.option(
        "--url",
        envvar="LIVEKIT_URL",
        help="LiveKit server or Cloud project's websocket URL",
    )
    @click.option(
        "--api-key",
        envvar="LIVEKIT_API_KEY",
        help="LiveKit server or Cloud project's API key",
    )
    @click.option(
        "--api-secret",
        envvar="LIVEKIT_API_SECRET",
        help="LiveKit server or Cloud project's API secret",
    )
    @click.option(
        "--drain-timeout",
        default=60,
        help="Time in seconds to wait for jobs to finish before shutting down",
    )
    def start(log_level: str, url: str, api_key: str, api_secret: str, drain_timeout: int) -> None:
        opts.ws_url = url or opts.ws_url
        opts.api_key = api_key or opts.api_key
        opts.api_secret = api_secret or opts.api_secret
        args = proto.CliArgs(
            opts=opts,
            log_level=log_level,
            devmode=False,
            asyncio_debug=False,
            register=True,
            watch=False,
            drain_timeout=drain_timeout,
        )
        run_worker(args)

    @cli.command(help="Start the worker in development mode")
    @click.option(
        "--log-level",
        default="DEBUG",
        type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
        help="Set the logging level",
    )
    @click.option(
        "--url",
        envvar="LIVEKIT_URL",
        help="LiveKit server or Cloud project's websocket URL",
    )
    @click.option(
        "--api-key",
        envvar="LIVEKIT_API_KEY",
        help="LiveKit server or Cloud project's API key",
    )
    @click.option(
        "--api-secret",
        envvar="LIVEKIT_API_SECRET",
        help="LiveKit server or Cloud project's API secret",
    )
    @click.option(
        "--asyncio-debug/--no-asyncio-debug",
        default=False,
        help="Enable debugging feature of asyncio",
    )
    @click.option(
        "--watch/--no-watch",
        default=hot_reload if utils.is_given(hot_reload) else True,
        help="Watch for changes in the current directory and plugins in editable mode",
    )
    def dev(
        log_level: str,
        url: str,
        api_key: str,
        api_secret: str,
        asyncio_debug: bool,
        watch: bool,
    ) -> None:
        opts.ws_url = url or opts.ws_url
        opts.api_key = api_key or opts.api_key
        opts.api_secret = api_secret or opts.api_secret
        args = proto.CliArgs(
            opts=opts,
            log_level=log_level,
            devmode=True,
            asyncio_debug=asyncio_debug,
            watch=watch,
            drain_timeout=0,
            register=True,
        )

        _run_dev(args)

    @cli.command(help="Start a new chat")
    @click.option(
        "--url",
        envvar="LIVEKIT_URL",
        help="LiveKit server or Cloud project's websocket URL",
    )
    @click.option(
        "--api-key",
        envvar="LIVEKIT_API_KEY",
        help="LiveKit server or Cloud project's API key",
    )
    @click.option(
        "--api-secret",
        envvar="LIVEKIT_API_SECRET",
        help="LiveKit server or Cloud project's API secret",
    )
    def console(
        url: str,
        api_key: str,
        api_secret: str,
    ) -> None:
        # keep everything inside the same process when using the chat mode
        opts.job_executor_type = JobExecutorType.THREAD
        opts.ws_url = url or opts.ws_url
        opts.api_key = api_key or opts.api_key
        opts.api_secret = api_secret or opts.api_secret

        args = proto.CliArgs(
            opts=opts,
            log_level="DEBUG",
            devmode=True,
            asyncio_debug=False,
            watch=False,
            console=True,
            drain_timeout=0,
            register=False,
            simulate_job=proto.SimulateJobArgs(
                room="mock-console",
            ),
        )
        run_worker(args)

    @cli.command(help="Connect to a specific room")
    @click.option(
        "--log-level",
        default="DEBUG",
        type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
        help="Set the logging level",
    )
    @click.option(
        "--url",
        envvar="LIVEKIT_URL",
        help="LiveKit server or Cloud project's websocket URL",
    )
    @click.option(
        "--api-key",
        envvar="LIVEKIT_API_KEY",
        help="LiveKit server or Cloud project's API key",
    )
    @click.option(
        "--api-secret",
        envvar="LIVEKIT_API_SECRET",
        help="LiveKit server or Cloud project's API secret",
    )
    @click.option(
        "--asyncio-debug/--no-asyncio-debug",
        default=False,
        help="Enable debugging feature of asyncio",
    )
    @click.option(
        "--watch/--no-watch",
        default=True,
        help="Watch for changes in the current directory and plugins in editable mode",
    )
    @click.option("--room", help="Room name to connect to", required=True)
    @click.option("--participant-identity", help="Participant identity (JobType.JT_PUBLISHER)")
    def connect(
        log_level: str,
        url: str,
        api_key: str,
        api_secret: str,
        asyncio_debug: bool,
        watch: bool,
        room: str,
        participant_identity: str,
    ) -> None:
        opts.ws_url = url or opts.ws_url
        opts.api_key = api_key or opts.api_key
        opts.api_secret = api_secret or opts.api_secret
        args = proto.CliArgs(
            opts=opts,
            log_level=log_level,
            devmode=True,
            register=False,
            asyncio_debug=asyncio_debug,
            watch=watch,
            drain_timeout=0,
            simulate_job=proto.SimulateJobArgs(
                room=room,
                participant_identity=participant_identity,
            ),
        )

        _run_dev(args)

    @cli.command(help="Download plugin dependency files")
    @click.option(
        "--log-level",
        default="DEBUG",
        type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
        help="Set the logging level",
    )
    def download_files(log_level: str) -> None:
        setup_logging(log_level, True, False)

        for plugin in Plugin.registered_plugins:
            logger.info(f"Downloading files for {plugin}")
            plugin.download_files()
            logger.info(f"Finished downloading files for {plugin}")

    cli()


def _run_dev(
    args: proto.CliArgs,
):
    if args.watch:
        from .watcher import WatchServer

        setup_logging(args.log_level, args.devmode, args.console)
        main_file = pathlib.Path(sys.argv[0]).parent

        async def _run_loop():
            server = WatchServer(run_worker, main_file, args, loop=asyncio.get_event_loop())
            await server.run()

        try:
            asyncio.run(_run_loop())
        except KeyboardInterrupt:
            pass
    else:
        run_worker(args)


def run_worker(args: proto.CliArgs) -> None:
    global CLI_ARGUMENTS
    CLI_ARGUMENTS = args

    setup_logging(args.log_level, args.devmode, args.console)
    args.opts.validate_config(args.devmode)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if args.console:
        print(_esc(34) + "=" * 50 + _esc(0))
        print(_esc(34) + "     Livekit Agents - Console" + _esc(0))
        print(_esc(34) + "=" * 50 + _esc(0))
        print("Press [Ctrl+B] to toggle between Text/Audio mode, [Q] to quit.\n")

    worker = Worker(args.opts, devmode=args.devmode, register=args.register, loop=loop)

    loop.set_debug(args.asyncio_debug)
    loop.slow_callback_duration = 0.1  # 100ms
    utils.aio.debug.hook_slow_callbacks(2)

    @worker.once("worker_started")
    def _worker_started():
        if args.simulate_job and args.reload_count == 0:
            # logger.info("connecting to room %s", args.simulate_job.room)
            loop.create_task(
                worker.simulate_job(args.simulate_job.room, args.simulate_job.participant_identity)
            )

        if args.devmode:
            logger.info(
                f"{_esc(1)}see tracing information at http://localhost:{worker.worker_info.http_port}/debug{_esc(0)}"
            )
        else:
            logger.info(
                f"see tracing information at http://localhost:{worker.worker_info.http_port}/debug"
            )

    try:

        def _signal_handler():
            raise KeyboardInterrupt

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _signal_handler)

    except NotImplementedError:
        # TODO(theomonnom): add_signal_handler is not implemented on win
        pass

    async def _worker_run(worker: Worker) -> None:
        try:
            await worker.run()
        except Exception:
            logger.exception("worker failed")

    watch_client = None
    if args.watch:
        from .watcher import WatchClient

        watch_client = WatchClient(worker, args, loop=loop)
        watch_client.start()

    try:
        main_task = loop.create_task(_worker_run(worker), name="agent_runner")
        try:
            loop.run_until_complete(main_task)
        except KeyboardInterrupt:
            pass

        try:
            if not args.devmode:
                loop.run_until_complete(worker.drain(timeout=args.drain_timeout))

            loop.run_until_complete(worker.aclose())

            if watch_client:
                loop.run_until_complete(watch_client.aclose())
        except KeyboardInterrupt:
            logger.warning("exiting forcefully")
            import os

            os._exit(1)  # TODO(theomonnom): add aclose(force=True) in worker
    finally:
        try:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()

            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()
