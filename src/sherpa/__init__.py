import os

__version__ = "0.2.0"
# What is actually running, e.g. "0.2.0+a1b2c3d" when the image was built with SHERPA_REV.
BUILD = __version__ + (f"+{os.environ['SHERPA_REV']}" if os.environ.get("SHERPA_REV") else "")
