"""Run Uvicorn in this interpreter with bounded, redacted service output."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.launcher_process import BoundedLogStream, service_logger
from scripts.service_ports import API_BIND_FAILED, bind_server_socket
from classcue.asr_storage import log_directory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-dir", type=Path, default=log_directory(PROJECT_ROOT))
    args = parser.parse_args()
    logger = service_logger(args.log_dir, "api")
    sys.stdout = BoundedLogStream(logger, logging.INFO)
    sys.stderr = BoundedLogStream(logger, logging.ERROR)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        server_logger.handlers = list(logger.handlers)
        server_logger.propagate = False
        server_logger.setLevel(logging.INFO)
    try:
        import uvicorn

        try:
            bound = bind_server_socket(args.host, args.port)
        except OSError:
            logger.exception("service=api phase=bind port=%s", args.port)
            return API_BIND_FAILED
        with bound:
            config = uvicorn.Config(
                "api.main:app", host=args.host, port=args.port,
                log_config=None, access_log=False,
            )
            uvicorn.Server(config).run(sockets=[bound])
        return 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code:
            logger.exception("service=api phase=server exitcode=%s", code)
        return code
    except Exception:
        logger.exception("service=api phase=server")
        return 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
