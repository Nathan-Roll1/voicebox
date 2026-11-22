"""Select/download Fourier via the PR's UI and dictate through Windows WebView2."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
import wave

from playwright.sync_api import sync_playwright

ROOT = Path(os.environ["RUNNER_TEMP"])
ORIGIN = "http://127.0.0.1:17493"
MODEL = "oruk-fourier-2b"
REFERENCE = "and so my fellow americans ask not what your country can do for you ask what you can do for your country"
JFK = "https://raw.githubusercontent.com/ggml-org/whisper.cpp/b0a11594aec50892a02cd8d129eee2dfe93a8bb8/samples/jfk.wav"
MSI = "https://github.com/jamiepine/voicebox/releases/download/v0.6.0/Voicebox_0.6.0_x64_en-US.msi"


def get(path):
    with urllib.request.urlopen(ORIGIN + path, timeout=15) as request:
        return json.load(request)


def wait_for(fn, description, seconds=90):
    end = time.monotonic() + seconds
    error = None
    while time.monotonic() < end:
        try:
            result = fn()
            if result:
                return result
        except Exception as exc:
            error = str(exc)
        time.sleep(1)
    raise RuntimeError(f"{description}: {error}")


def install():
    msi = ROOT / "Voicebox.msi"
    subprocess.run(["curl.exe", "-fLsS", "--retry", "2", MSI, "-o", str(msi)], check=True)
    with msi.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    assert digest == "4d97cd976671c6f321c115b55d90bfe6aa6dd049bc9fb1c1c20361eb710453bd"
    result = subprocess.run(["msiexec.exe", "/i", str(msi), "/qn", "/norestart", "/L*v", str(ROOT / "voicebox-install.log")])
    if result.returncode not in (0, 3010):
        print((ROOT / "voicebox-install.log").read_text(errors="replace")[-5000:])
        result.check_returncode()
    for parent in ("PROGRAMFILES", "LOCALAPPDATA", "ProgramFiles(x86)"):
        directory = Path(os.environ[parent])
        for pattern in ("Voicebox/*.exe", "Programs/Voicebox/*.exe"):
            for binary in directory.glob(pattern):
                if binary.stem.lower() == "voicebox":
                    print("MSI_INSTALLED", digest, binary, flush=True)
                    signature = subprocess.run(["powershell.exe","-NoProfile","-Command",
                        f"(Get-AuthenticodeSignature -LiteralPath '{msi}').Status"], capture_output=True, text=True)
                    print("WINDOWS_AUTHENTICODE", signature.stdout.strip(), flush=True)
                    return binary
    raise RuntimeError("Installed executable missing")


def fixture():
    original = ROOT / "jfk.wav"
    urllib.request.urlretrieve(JFK, original)
    fake = ROOT / "fourier-virtual-microphone.wav"
    with wave.open(str(original)) as src, wave.open(str(fake), "wb") as dst:
        dst.setparams(src.getparams())
        silent_second = b"\x00" * src.getframerate() * src.getsampwidth() * src.getnchannels()
        dst.writeframes(silent_second + src.readframes(src.getnframes()) + silent_second * 4)
    return fake


def choose_and_download(page):
    page.goto(ORIGIN)
    page.locator('a[href="/captures"]').first.click(timeout=60000)
    page.locator('a[href="/settings/captures"]').first.click()
    combo = page.get_by_role("combobox").filter(has_text=re.compile("Whisper", re.I))
    combo.wait_for(timeout=60000)
    combo.click()
    page.get_by_role("option", name=re.compile("Oruk Fourier 2B")).click()
    wait_for(lambda: get("/settings/captures")["stt_model"] == MODEL, "model selection")
    toggle = page.locator("#autoRefine")
    if toggle.get_attribute("aria-checked") == "true":
        toggle.click()
    wait_for(lambda: not get("/settings/captures")["auto_refine"], "raw dictation mode")
    page.locator('a[href="/captures"]').first.click()
    assert not get("/capture/readiness")["stt"]["ready"]
    page.get_by_role("button", name="Download", exact=True).first.click(timeout=30000)
    wait_for(lambda: get("/capture/readiness")["stt"]["ready"], "complete original-card model download", 450)
    page.get_by_role("button", name="Dictate", exact=True).wait_for(timeout=45000)
    wait_for(lambda: all(row["model_name"] != MODEL for row in get("/tasks/active")["downloads"]), "download toast closes")
    print("UI_COLD_INSTALL selector/download/readiness/Dictate", flush=True)


def record(page, environment):
    before = get("/captures")["total"]
    page.get_by_role("button", name="Dictate", exact=True).click(timeout=45000)
    stop = page.get_by_role("button", name="Stop", exact=True).last
    stop.wait_for(timeout=25000)
    time.sleep(13)
    stop.click()
    wait_for(lambda: get("/captures")["total"] > before, "new captured microphone recording", 210)
    captures = get("/captures")
    rows = captures.get("items") or captures.get("captures")
    item = get("/captures/" + rows[0]["id"])
    text = re.sub("[^a-z ]", "", item["transcript_raw"].lower()).strip()
    assert text == REFERENCE, item["transcript_raw"]
    assert item["source"] == "dictation" and item["stt_model"] == MODEL
    assert 10000 <= item["duration_ms"] <= 16500, item["duration_ms"]
    page.get_by_text(re.compile("my fellow americans", re.I)).first.wait_for(timeout=30000)
    print("MICROPHONE_RESULT", json.dumps(dict(environment=environment, capture_source=item["source"], words=len(text.split()), model=item["stt_model"], duration_ms=item["duration_ms"], transcript=item["transcript_raw"])), flush=True)


def native_page(browser):
    for context in browser.contexts:
        for page in context.pages:
            if "tauri.localhost" in page.url:
                return page
    return None


def main():
    binary = install()
    audio = fixture()
    flags = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required", "--use-file-for-fake-audio-capture=" + str(audio) + "%noloop"]
    env = os.environ.copy()
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = subprocess.list2cmdline(["--remote-debugging-port=9223", *flags])
    backend = open(ROOT / "voicebox-server.log", "w", buffering=1)
    server = subprocess.Popen([sys.executable, "-m", "backend.main", "--port", "17493", "--data-dir", str(ROOT / "voicebox-data")], stdout=backend, stderr=subprocess.STDOUT)
    desktop = None
    try:
        wait_for(lambda: get("/health"), "production FastAPI server", 90)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", args=flags)
            web = browser.new_context(permissions=["microphone"]).new_page()
            try:
                choose_and_download(web)
                record(web, "PR production web build, Chrome on Windows")
            except Exception:
                print("WEB_SCREEN", web.locator("body").inner_text()[:3000], flush=True)
                raise
            finally:
                browser.close()
            desktop = subprocess.Popen([str(binary)], env=env)
            def cdp():
                import urllib.request
                return urllib.request.urlopen("http://127.0.0.1:9223/json/version", timeout=2).read()
            wait_for(cdp, "native WebView2 debug endpoint", 75)
            native = p.chromium.connect_over_cdp("http://127.0.0.1:9223")
            page = wait_for(lambda: native_page(native), "native Tauri window")
            try:
                from urllib.parse import urlsplit
                origin = urlsplit(page.url)
                page.context.grant_permissions(["microphone"], origin=f"{origin.scheme}://{origin.netloc}")
                page.locator('a[href="/captures"]').first.click(timeout=45000)
                record(page, "signed Voicebox 0.6.0 MSI + current PR external backend, Microsoft WebView2")
            except Exception:
                print("NATIVE_URL", page.url, "NATIVE_SCREEN", page.locator("body").inner_text()[:3000], flush=True)
                raise
    finally:
        if desktop:
            desktop.terminate()
        server.terminate()
        backend.close()
        print("BACKEND_TAIL", (ROOT/"voicebox-server.log").read_text(errors="replace")[-4000:], flush=True)


if __name__ == "__main__":
    main()
