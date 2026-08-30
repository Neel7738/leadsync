#!/usr/bin/env python3
"""
LeadSync — One-command launcher
python main.py

Starts everything: .env bootstrap, DB init, FastAPI (8000) + Streamlit (8501) + browser.
All other setup via Web UI: http://localhost:8501 → Settings → Gmail / LLM / Air-gapped

Usage:
  python main.py                 # auto
  python main.py --no-browser    # don't open browser
  python main.py --api-port 8001 --ui-port 8502
"""
import os, sys, time, subprocess, webbrowser, pathlib, argparse, signal, shutil

ROOT = pathlib.Path(__file__).parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

def ensure_env():
    if not ENV_PATH.exists() and ENV_EXAMPLE.exists():
        shutil.copy(ENV_EXAMPLE, ENV_PATH)
        print(f"[main] Created {ENV_PATH} from .env.example — configure via Web UI: Settings")
    # ensure data dir
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "exports").mkdir(exist_ok=True)

def ensure_deps():
    # lightweight check — if core import fails, hint pip install
    try:
        import fastapi, uvicorn, streamlit  # noqa
    except ImportError as e:
        print(f"[main] Missing dep: {e}")
        print("[main] Installing… pip install -e .")
        subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=ROOT)
    # ensure prod deps for air-gapped QR etc.
    for pkg in ["bcrypt", "passlib", "qrcode", "segno"]:
        try: __import__(pkg)
        except ImportError:
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", pkg, "--quiet"], cwd=ROOT, timeout=60)
            except: pass

def wait_for(url, timeout=30):
    import httpx
    start=time.time()
    while time.time()-start < timeout:
        try:
            r=httpx.get(url, timeout=2)
            if r.status_code < 500:
                return True
        except: pass
        time.sleep(1)
    return False

def main():
    ap=argparse.ArgumentParser(description="LeadSync one-command launcher")
    ap.add_argument("--api-port", type=int, default=int(os.environ.get("PORT","8000")))
    ap.add_argument("--ui-port", type=int, default=8501)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    args=ap.parse_args()

    ensure_env()
    ensure_deps()

    # Load .env for this process (pydantic-settings will reload)
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except: pass

    api_cmd=[sys.executable,"-m","uvicorn","api.app:app", "--host", args.host, "--port", str(args.api_port)]
    ui_cmd=[sys.executable,"-m","streamlit","run", str(ROOT/"ui/streamlit/app.py"), "--server.port", str(args.ui_port), "--server.headless", "true", "--server.runOnSave", "false"]

    print(f"[main] Starting API → http://{args.host}:{args.api_port}  docs → http://{args.host}:{args.api_port}/docs")
    print(f"[main] Starting UI  → http://{args.host}:{args.ui_port}")
    api=subprocess.Popen(api_cmd, cwd=ROOT)
    ui=subprocess.Popen(ui_cmd, cwd=ROOT)

    def cleanup(sig=None,frame=None):
        print("\n[main] Shutting down…")
        for p in (api,ui):
            try:
                if p.poll() is None:
                    p.terminate()
                    try: p.wait(timeout=5)
                    except: p.kill()
            except: pass
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # wait then open browser
    if not args.no_browser:
        def open_browser():
            time.sleep(3)
            # wait for UI
            ok=wait_for(f"http://{args.host}:{args.ui_port}", timeout=40)
            url=f"http://{args.host}:{args.ui_port}"
            print(f"[main] Opening {url} {'(ready)' if ok else '(still starting…)'}")
            try: webbrowser.open(url)
            except: pass
            # also hint API docs
            print(f"[main] API docs: http://{args.host}:{args.api_port}/docs")
        import threading
        threading.Thread(target=open_browser, daemon=True).start()
        # also auto-open setup if .env still mock
        if os.environ.get("IMAP_USERNAME","") in ("", "test@leadsync.local"):
            print("[main] Tip: Go to UI → Settings for Gmail setup, or run python scripts/test_offline_pipeline.py for offline demo")

    # block until one dies
    try:
        while True:
            if api.poll() is not None:
                print(f"[main] API exited with {api.returncode} — restarting in 2s")
                time.sleep(2)
                api=subprocess.Popen(api_cmd, cwd=ROOT)
            if ui.poll() is not None:
                print(f"[main] UI exited with {ui.returncode} — restarting in 2s")
                time.sleep(2)
                ui=subprocess.Popen(ui_cmd, cwd=ROOT)
            time.sleep(1)
    except KeyboardInterrupt:
        cleanup()

if __name__=="__main__":
    main()
