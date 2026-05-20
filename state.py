import threading
from datetime import date, datetime

active_recordings: dict = {}
resume_after: dict[str, datetime] = {}
idle_reason: dict[str, str] = {}
shutdown = threading.Event()
config_lock = threading.Lock()

# (username, date) -> file count for rollover_max_files tracking across restarts
daily_file_counts: dict[tuple[str, date], int] = {}
