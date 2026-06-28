import threading
from datetime import date, datetime

active_recordings: dict = {}
resume_after: dict[str, datetime] = {}
resume_reason: dict[str, str] = {}  # why resume_after was set: "cooldown", "stop_for_day", "rollover_limit"
idle_reason: dict[str, str] = {}
stop_recording_events: dict[str, threading.Event] = {}
shutdown = threading.Event()
config_lock = threading.Lock()

# (username, date) -> file count for rollover_max_files tracking across restarts
daily_file_counts: dict[tuple[str, date], int] = {}
