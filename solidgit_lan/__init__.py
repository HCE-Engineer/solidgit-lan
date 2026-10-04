"""SolidGit LAN — LAN-only version control and file locking for SolidWorks assemblies."""

__version__ = "0.1.0"

# Wire protocol version. Bumped whenever a change would make two app versions talk past
# each other; peers refuse to connect across a mismatch rather than misbehave.
PROTOCOL_VERSION = 1
