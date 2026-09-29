import logging
import sys
import contextvars
from pathlib import Path

_worker_prefix: contextvars.ContextVar[str] = contextvars.ContextVar('worker_prefix', default='')

# Console Windows mac dinh la cp1252. Ep UTF-8 de log tieng Viet/JP khong bi loi.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass

def set_worker_prefix(prefix: str):
    _worker_prefix.set(prefix)

def get_worker_prefix() -> str:
    return _worker_prefix.get('')

class WorkerPrefixFilter(logging.Filter):
    def filter(self, record):
        prefix = get_worker_prefix()
        record.worker_prefix = f" [{prefix}]" if prefix else ""
        return True

_file_handler = None
_extra_sink = None  # handler phu gan cho moi app logger

def register_sink(handler: logging.Handler):
    """Gan them mot handler (vd: QueueHandler cua UI) vao tat ca app logger.

    App logger deu co propagate=False nen khong tu lan len root; ham nay gan
    truc tiep cho cac logger dang co, va get_logger se gan cho logger tao sau.
    """
    global _extra_sink
    _extra_sink = handler
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger) and not logger.propagate:
            if handler not in logger.handlers:
                logger.addHandler(handler)

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    # Check if colorlog is available
    try:
        import colorlog
        handler = colorlog.StreamHandler(sys.stdout)
        handler.addFilter(WorkerPrefixFilter())
        handler.setFormatter(
            colorlog.ColoredFormatter(
                fmt="%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(cyan)s%(name)s%(reset)s — %(worker_prefix)s%(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
                log_colors={
                    "DEBUG":    "white",
                    "INFO":     "green",
                    "WARNING":  "yellow",
                    "ERROR":    "red",
                    "CRITICAL": "bold_red",
                },
            )
        )
    except ImportError:
        handler = logging.StreamHandler(sys.stdout)
        handler.addFilter(WorkerPrefixFilter())
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s — %(worker_prefix)s%(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            )
        )

    logger.addHandler(handler)
    
    # Automatically attach the file handler if it has already been initialized
    if _file_handler:
        logger.addHandler(_file_handler)

    # Gan sink phu (panel log UI) neu da dang ky
    if _extra_sink:
        logger.addHandler(_extra_sink)

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger

def add_file_handler(log_file: str):
    global _file_handler
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.addFilter(WorkerPrefixFilter())
    fh.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s — %(worker_prefix)s%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _file_handler = fh

    # Attach to all existing custom loggers
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger) and not logger.propagate:
            if fh not in logger.handlers:
                logger.addHandler(fh)

log = get_logger("yamada-reg")
