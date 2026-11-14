"""Deploy on Modal (https://modal.com): one container with MCP + REST + approval UI.

    modal setup                                   # once: log in (opens the browser)
    modal secret create trusted-trade-agents \\
        VALIO_API_KEY=... STAFF_KEY=... OFFER_SIGNING_SECRET=... \\
        GEMINI_API_KEY=... ELEVENLABS_API_KEY=...
    modal deploy modal_app.py                     # prints the public https URL

Valio's data files are never uploaded from your machine: the container downloads them with VALIO_API_KEY into
a Modal Volume on first start. Orders and the audit log live on the same volume, so they survive restarts.
"""
import subprocess
import threading
import time

import modal

APP_NAME = "trusted-trade-agents"
app = modal.App(APP_NAME)
volume = modal.Volume.from_name(f"{APP_NAME}-data", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements("requirements.txt")
    .env({
        "PYTHONUNBUFFERED": "1",
        "PORT": "8000",
        "PUBLIC_DEPLOY": "1",                 # public mode
        "OPEN_DEMO": "1",                     # hackathon demo: approval UI opens without a staff key (set "0" to require STAFF_KEY)
        "VALIO_DATA_DIR": "/data",            # Valio files + catalog DB on the volume
        "ORDERS_DB": "/data/orders.db",       # orders + audit log on the volume
        "AI_CROSSCHECK": "1",
        "GEMINI_MODEL": "gemini-4-hydrogen",
        "ELEVENLABS_STT_MODEL": "scribe_v2",
        "ELEVENLABS_REALTIME_MODEL": "scribe_v2_realtime",
    })
    # Code only: never secrets, local databases or Valio's data.
    .add_local_dir(".", "/app", ignore=[
        ".env", ".env.*", ".venv", ".git", "**/__pycache__", "**/*.pyc", "data/raw", "**/*.db",
        "**/*.db-journal", ".pytest_cache", ".vscode", ".idea", "**/.DS_Store",
    ])
)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name(APP_NAME)],
    volumes={"/data": volume},
    min_containers=1,        # stay warm during judging: no cold start, orders stay in one place
    max_containers=1,        # one writer for the SQLite order store
    timeout=3600,
)
@modal.concurrent(max_inputs=200)
@modal.web_server(port=8000, startup_timeout=300, label=APP_NAME)
def web():
    subprocess.Popen(["sh", "deploy/start.sh"], cwd="/app")

    def persist():           # write orders / downloaded data back to the volume regularly
        while True:
            time.sleep(30)
            try:
                volume.commit()
            except Exception as e:  # noqa: BLE001
                print(f"[volume] commit skipped: {e}")

    threading.Thread(target=persist, daemon=True).start()
