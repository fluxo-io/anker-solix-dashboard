import os

# Isolated defaults must be present before importing the Dash module.
os.environ["AUTH_ENABLED"] = "false"
os.environ["API_URL"] = "http://api:8001"
os.environ["API_TOKEN"] = "test-token-" + "a" * 32
os.environ["SECRET_KEY"] = "test-secret-" + "b" * 32
os.environ["CANONICAL_ORIGIN"] = ""
