import threading
from datetime import datetime

active_recordings: dict = {}
resume_after: dict[str, datetime] = {}
idle_reason: dict[str, str] = {}
shutdown = threading.Event()
config_lock = threading.Lock()
