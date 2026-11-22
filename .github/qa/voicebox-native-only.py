"""Bounded Windows-only installed-shell test; no repeated Chrome dictation."""
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

import psutil
from playwright.sync_api import sync_playwright
from pywinauto import Desktop

spec = importlib.util.spec_from_file_location("common", Path(__file__).with_name("voicebox-microphone.py"))
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


def put(path, data, method):
    req = urllib.request.Request(qa.ORIGIN + path, data=json.dumps(data).encode(), method=method, headers={"Content-Type":"application/json"})
    return urllib.request.urlopen(req, timeout=20).read()


def prepare_model():
    put("/settings/captures", {"stt_model": qa.MODEL, "language":"en", "auto_refine":False}, "PUT")
    put("/models/download", {"model_name": qa.MODEL}, "POST")
    def ready():
        for record in qa.get("/tasks/active")["downloads"]:
            if record["model_name"]==qa.MODEL and record["status"]=="error":
                print("DOWNLOAD_ERROR", record, flush=True)
                raise SystemExit(2)
        return qa.get("/capture/readiness")["stt"]["ready"]
    qa.wait_for(ready,"pinned Fourier runtime",250)


def window(pid):
    all_windows = Desktop(backend="uia").windows()
    for w in all_windows:
        if w.process_id() == pid:
            return w
    return None


def dump(w):
    return [(c.element_info.control_type,c.window_text()[:74]) for c in w.descendants()[:100] if c.window_text()]


def control(w, labels, types):
    for child in w.descendants():
        if child.element_info.control_type in types and child.window_text().strip().lower() in labels:
            return child
    return None


def click(c):
    try:
        c.iface_invoke.Invoke()
    except Exception:
        c.click_input()


def inside_native_accessibility(w):
    print("NATIVE_ACCESSIBILITY",dump(w),flush=True)
    captures=qa.wait_for(lambda:control(w,{"captures"},{"Hyperlink","Button","Text"}),"Captures navigation in native accessibility tree",20)
    click(captures)
    prepare_model()
    button=qa.wait_for(lambda:control(w,{"dictate"},{"Button"}),"native accessibility Dictate",35)
    before=qa.get("/captures")["total"]
    click(button)
    stop=qa.wait_for(lambda:control(w,{"stop","stop recording"},{"Button"}),"native accessibility Stop",25)
    time.sleep(13)
    click(stop)
    qa.wait_for(lambda:qa.get("/captures")["total"]>before, "capture from installed Windows shell",160)
    entry=qa.get("/captures")["items"][0]
    words=" ".join(re.findall("[a-z]+",entry["transcript_raw"].lower()))
    assert words==qa.REFERENCE,entry["transcript_raw"]
    assert entry["source"]=="dictation" and entry["stt_model"]==qa.MODEL
    print("NATIVE_MICROPHONE",json.dumps({k:entry[k] for k in ["duration_ms","source","stt_model","transcript_raw"]}),flush=True)


def main():
    binary=qa.install()
    wave=qa.fixture()
    env=dict(os.environ)
    env["WEBVIEW2_USER_DATA_FOLDER"]=tempfile.mkdtemp(prefix="voicebox-webview2-")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    flags=[f"--remote-debugging-port={port}","--force-renderer-accessibility","--use-fake-ui-for-media-stream","--use-fake-device-for-media-stream","--autoplay-policy=no-user-gesture-required","--use-file-for-fake-audio-capture="+str(wave)+"%noloop"]
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"]=subprocess.list2cmdline(flags)
    output=open(qa.ROOT/"voicebox-native-server.log","w",buffering=1)
    server=subprocess.Popen([sys.executable,"-m","backend.main","--port","17493","--data-dir",str(qa.ROOT/"voicebox-native-data")],stdout=output,stderr=subprocess.STDOUT)
    native=None; w=None
    try:
        qa.wait_for(lambda:qa.get("/health"),"Voicebox production server",90)
        native=subprocess.Popen([str(binary)],env=env)
        w=qa.wait_for(lambda:window(native.pid),"Windows installer app window",45)
        time.sleep(7)
        print("NATIVE_WINDOW",native.pid,w.window_text(),w.class_name(),flush=True)
        names=("remote-debugging-port","use-file-for-fake-audio-capture","force-renderer-accessibility")
        for proc in psutil.process_iter(["pid","name","cmdline"]):
            if (proc.info.get("name") or "").lower()!="msedgewebview2.exe":continue
            args=proc.info.get("cmdline") or []
            checks={n:next((arg.split("=")[0] if n!="remote-debugging-port" else arg for arg in args if arg.startswith("--"+n)),None) for n in names}
            if any(checks.values()):
                print("WEBVIEW_ARGUMENTS",proc.pid,checks,flush=True)
        url=f"http://127.0.0.1:{port}"
        direct=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        ws=None
        try:
            ws=qa.wait_for(lambda:json.load(direct.open(url+"/json/version",timeout=2))["webSocketDebuggerUrl"], "documented WebView2/debug endpoint",12)
        except RuntimeError as exc:
            print("CDP_UNAVAILABLE",exc,flush=True)
        if ws:
            with sync_playwright() as p:
                shell=p.chromium.connect_over_cdp(ws,timeout=15000)
                page=qa.wait_for(lambda:qa.native_page(shell),"installed Tauri WebView2 page",20)
                print("NATIVE_UI_URL",page.url,flush=True)
                origin=urlsplit(page.url)
                try: page.context.grant_permissions(["microphone"],origin=f"{origin.scheme}://{origin.netloc}")
                except Exception as exc: print("GRANT",str(exc)[:150])
                page.locator('a[href="/captures"]').first.click(timeout=30000)
                prepare_model()
                qa.record(page,"installed Voicebox 0.6.0 on Windows, Microsoft WebView2 and PR source backend")
        else:
            inside_native_accessibility(w)
    except Exception:
        if w:
            print("NATIVE_TREE_END",dump(w),flush=True)
        raise
    finally:
        if native:native.terminate()
        server.terminate();output.close()
        print("BACKEND_TAIL",(qa.ROOT/"voicebox-native-server.log").read_text(errors="replace")[-1000:],flush=True)


if __name__=="__main__":
    main()
