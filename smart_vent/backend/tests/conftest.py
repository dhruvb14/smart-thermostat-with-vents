import os
import time

os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()
