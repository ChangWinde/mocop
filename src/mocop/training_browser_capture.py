"""Mac-only read bridge: the browser retains its credential and returns metadata."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .platform_origin import ORIGIN
from .training_browser import OBSERVATIONS

STATE = Path.home() / ".local/state/mocop"
HELPER = STATE / "chrome-metadata-reader"
SOURCE = Path(__file__).with_name("mac_browser.swift")

JAVASCRIPT = r"""
(() => {
  if (location.origin !== __MOCOP_ORIGIN__) return 'wrong_origin';
  if (window.__mocopMetadataRead?.state === 'pending' && Date.now()-window.__mocopMetadataRead.at < 90000) return 'pending';
  window.__mocopMetadataRead = {state:'pending',at:Date.now()};
  (async () => {
    try {
      const src = [...document.scripts].map(s=>new URL(s.src,location.href)).find(u=>u.origin===location.origin && /^\/assets\/index-[\w-]+\.js$/.test(u.pathname));
      if (!src) throw new Error('unavailable');
      const module = await import(src.pathname);
      // Resolve the current official GET client; minified export names change on releases.
      const entry = await (await fetch(src.pathname, {redirect:'error'})).text();
      const asset = entry.match(/JobsView-[\w-]+\.js/);
      if (!asset) throw new Error('unavailable');
      const view = await (await fetch('/assets/'+asset[0], {redirect:'error'})).text();
      const caller = view.match(/([\w$]+)\(['"]\/pt\/api\/job-summaries['"]/);
      const imports = view.match(/import\{([^}]+)\}from['"]\.\/index-[\w-]+\.js['"]/);
      const binding = imports?.[1].split(',').map(x=>x.trim().split(/\s+as\s+/)).find(x=>(x[1] || x[0])===caller?.[1]);
      const get = module[binding?.[0]];
      if (typeof get !== 'function') throw new Error('unavailable');
      const jobs = [];
      let complete = false;
      for (let page=1; page<=20; page++) {
        const response = await get('/pt/api/job-summaries', {scope:'mine',page,page_size:100}, {skipAuthRedirect:true,timeoutMs:10000});
        if (!response.ok) throw new Error('unavailable');
        const payload = response.data;
        const rows = Array.isArray(payload?.items) ? payload.items : Array.isArray(payload?.jobs) ? payload.jobs : null;
        if (!rows) throw new Error('unavailable');
        for (const row of rows) {
          const requests = Array.isArray(row.resource_requests) ? row.resource_requests : [];
          jobs.push({vendor:row.vendor,job_id:row.job_id,status:row.status,gpu_count:row.gpu_count ?? null,gpu_model:row.gpu_model ?? null,
            started_at:row.started_at ?? null,runtime_seconds:row.runtime_seconds ?? null,
            resource_requests:requests.slice(0,64).map(r=>({gpu_type:r.gpu_type,gpu_count:r.gpu_count,replicas:r.replicas}))});
        }
        if (rows.length<100 || jobs.length>=payload.total) {complete=true;break;}
      }
      window.__mocopMetadataRead = {state:'done',status:'ok',sampled_at:Date.now(),jobs,complete};
    } catch { window.__mocopMetadataRead = {state:'done',status:'unavailable',sampled_at:Date.now(),jobs:[]}; }
  })();
  return 'started';
})()
""".replace("__MOCOP_ORIGIN__", json.dumps(ORIGIN))


def tabs():
    rows = subprocess.run(
        ["ps", "-axo", "pid=,comm="],
        capture_output=True,
        text=True,
        timeout=8,
        check=True,
    ).stdout.splitlines()
    for row in rows:
        parts = row.strip().split(None, 1)
        if (
            len(parts) != 2
            or parts[1]
            != "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        ):
            continue
        pid = int(parts[0])
        source = r"""ObjC.import('ScriptingBridge');
var a=$.SBApplication.applicationWithProcessIdentifier(PID),w=a.valueForKey('windows'),out=[];
for(var i=0;i<w.count;i++){var v=w.objectAtIndex(i),t=v.valueForKey('tabs');for(var j=0;j<t.count;j++){var x=t.objectAtIndex(j),u=ObjC.unwrap(x.valueForKey('URL'));if(u && new URL(u).origin===PLATFORM_ORIGIN)out.push({pid:PID,window:ObjC.unwrap(v.valueForKey('id')),tab:ObjC.unwrap(x.valueForKey('id'))});}} JSON.stringify(out);
""".replace("PID", str(pid)).replace("PLATFORM_ORIGIN", json.dumps(ORIGIN))
        try:
            result = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", source],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0:
            yield from json.loads(result.stdout)


def execute(tab, javascript):
    result = subprocess.run(
        [str(HELPER), str(tab["pid"]), str(tab["window"]), str(tab["tab"])],
        input=javascript,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        raise ValueError("browser_read_disabled_or_unavailable")
    return result.stdout.strip()


def capture():
    if sys.platform != "darwin":
        return {"status": "unsupported"}
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not HELPER.exists() or SOURCE.stat().st_mtime > HELPER.stat().st_mtime:
        subprocess.run(
            ["swiftc", str(SOURCE), "-o", str(HELPER)],
            capture_output=True,
            timeout=60,
            check=True,
        )
        HELPER.chmod(0o700)
    candidates = list(tabs())
    if not candidates:
        return {"status": "platform_tab_closed"}
    for tab in candidates[:16]:
        try:
            execute(tab, JAVASCRIPT)
            break
        except (ValueError, subprocess.TimeoutExpired):
            continue
    else:
        return {"status": "browser_read_disabled_or_unavailable"}
    for _ in range(30):
        time.sleep(1)
        raw = execute(
            tab,
            "JSON.stringify(window.__mocopMetadataRead?.state === 'done' ? window.__mocopMetadataRead : {state:'pending'})",
        )
        data = json.loads(raw)
        if data.get("state") != "done":
            continue
        if data.get("status") != "ok":
            return {"status": "session_or_schema_unavailable"}
        # Only the metadata projection above crosses out of the browser.
        tmp = OBSERVATIONS.with_name(OBSERVATIONS.name + ".tmp")
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(data, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, OBSERVATIONS)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"status": "ok", "jobs": len(data["jobs"]), "complete": data["complete"]}
    return {"status": "browser_read_timed_out"}


if __name__ == "__main__":
    try:
        result = capture()
    except (OSError, ValueError, subprocess.SubprocessError):
        result = {"status": "browser_read_disabled_or_unavailable"}
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_path = STATE / "training-browser-status.json"
    status = {**result, "sampled_at": time.time() * 1000}
    descriptor = os.open(
        status_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(status, stream)
    print(json.dumps(result))
