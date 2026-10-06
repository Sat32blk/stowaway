import os

# Set by the image build (from the Git tag); falls back to this for source installs.
VERSION = os.environ.get("STOWAWAY_VERSION") or "1.3.0"
# Where "Report a problem" opens a new issue. Change when publishing your own copy.
REPO_URL = os.environ.get("STOWAWAY_REPO", "https://github.com/Sat32blk/Stowaway").rstrip("/")
