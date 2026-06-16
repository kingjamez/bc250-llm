#!/usr/bin/env python3
import http.server, socketserver, urllib.request, urllib.parse, json, os, re, html, datetime, shutil, subprocess
OLLAMA="http://127.0.0.1:11434"; ROOT=os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE=os.path.join(ROOT,"default.txt"); MAX_STEPS=6
def base_url():
    try:
        for ln in open("/etc/bc250/update.conf"):
            if ln.startswith("BASE_URL="): return ln.split("=",1)[1].strip()
    except Exception: pass
    return "https://bc250.jaamcp.com"
def _get(u,t=20): return urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"bc250"}),timeout=t)
def _ollama(p,body=None,method="POST"):
    return urllib.request.urlopen(urllib.request.Request(OLLAMA+p,data=(json.dumps(body).encode() if body is not None else None),
                                  headers={"Content-Type":"application/json"},method=method),timeout=600)
def _strip(t): return html.unescape(re.sub(r"<[^>]+>","",t)).strip()
def feed():
    try: return json.load(_get(base_url()+"/models.json")).get("models",[])
    except Exception: return []
def installed():
    try: return [m["name"] for m in json.load(_ollama("/api/tags",method="GET")).get("models",[])]
    except Exception: return []
def free_gb(): return round(shutil.disk_usage("/").free/1e9,1)
def reboot_in(sec=3):
    subprocess.Popen(["sudo","systemd-run","--on-active=%d"%sec,"--timer-property=AccuracySec=100ms","systemctl","reboot"])
def unload_all(emit=None):
    try:
        for m in json.load(_ollama("/api/ps",method="GET")).get("models",[]):
            if emit: emit({"type":"tool","name":"pull_model","status":"Freeing memory…"})
            _ollama("/api/generate",{"model":m["name"],"keep_alive":0})
    except Exception: pass

def web_search(a,emit):
    try:
        page=_get("https://html.duckduckgo.com/html/?q="+urllib.parse.quote(a.get("query","")),15).read().decode("utf-8","ignore")
        pairs=re.findall(r'result__a[^>]*>(.*?)</a>.*?result__snippet[^>]*>(.*?)</a>',page,re.S)
        return "\n".join(f"{i}. {_strip(t)} — {_strip(s)}" for i,(t,s) in enumerate(pairs[:5],1)) or "No results."
    except Exception as e: return f"search error: {e}"
def get_recommendations(a,emit):
    f=feed(); inst=set(installed())
    if not f: return "Could not reach the recommended-models list."
    return "Approved models:\n"+"\n".join(f"- {m['name']} ({m['size_gb']}GB): {m['desc']}"+(" [INSTALLED]" if m['name'] in inst else "") for m in f)
def list_installed(a,emit): return "Installed: "+(", ".join(installed()) or "none")
def disk_free(a,emit): return f"{free_gb()} GB free"
def set_default(a,emit):
    n=a.get("name","")
    if n not in installed(): return f"'{n}' isn't installed."
    open(DEFAULT_FILE,"w").write(n); return f"Default model set to '{n}'."
def remove_model(a,emit):
    n=a.get("name","")
    if n not in installed(): return f"'{n}' isn't installed."
    try: cur=open(DEFAULT_FILE).read().strip()
    except Exception: cur=""
    if n==cur: return f"'{n}' is the current default; set a different default first."
    _ollama("/api/delete",{"name":n},"DELETE"); return f"Removed '{n}'."
def pull_model(a,emit):
    n=a.get("name","")
    entry=next((m for m in feed() if m["name"]==n),None)
    if not entry: return f"'{n}' is not in the approved list. Call get_recommendations for options."
    if n in installed(): return f"'{n}' is already installed."
    if free_gb()<entry.get("size_gb",14)+3: return f"Not enough disk. Remove a model first."
    ref=entry["ref"]
    try:
        unload_all(emit)   # free system RAM before pulling (resident model lives in GTT=system RAM)
        r=_ollama("/api/pull",{"name":ref,"stream":True}); last=-1
        for line in r:
            line=line.decode("utf-8","ignore").strip()
            if not line: continue
            try: j=json.loads(line)
            except Exception: continue
            tot=j.get("total"); comp=j.get("completed")
            if tot and comp:
                pct=int(comp*100/tot)
                if pct>=last+5: last=pct; emit({"type":"tool","name":"pull_model","status":f"Downloading {n}… {pct}%"})
        _ollama("/api/copy",{"source":ref,"destination":n}); _ollama("/api/delete",{"name":ref},"DELETE")
        return f"Installed '{n}' successfully. It is now available in the model list."
    except Exception as e: return f"install error: {e}"
def reboot_system(a,emit):
    reboot_in(3); return "Restarting the box now — it will be back in about a minute."

TOOLS_DEF=[
 ("web_search","Search the web for current/recent/factual info.",{"query":{"type":"string"}},["query"]),
 ("get_recommendations","List approved models the user can install/upgrade to.",{},[]),
 ("list_installed","List installed models.",{},[]),
 ("disk_free","Free disk space in GB.",{},[]),
 ("pull_model","Download & install an approved model by name.",{"name":{"type":"string"}},["name"]),
 ("set_default","Set the default model.",{"name":{"type":"string"}},["name"]),
 ("remove_model","Remove an installed model.",{"name":{"type":"string"}},["name"]),
 ("reboot_system","Restart/reboot this box.",{},[]),
]
TOOLS=[{"type":"function","function":{"name":n,"description":d,"parameters":{"type":"object","properties":p,"required":r}}} for n,d,p,r in TOOLS_DEF]
IMPL={"web_search":web_search,"get_recommendations":get_recommendations,"list_installed":list_installed,"disk_free":disk_free,
      "pull_model":pull_model,"set_default":set_default,"remove_model":remove_model,"reboot_system":reboot_system}

def run_agent(model,messages,emit):
    today=datetime.date.today().isoformat()
    sysmsg={"role":"system","content":
      f"You are a friendly home AI assistant. Today is {today}. "
      "Use web_search for current/factual questions you are unsure of. "
      "To change models: call get_recommendations, suggest one, then pull_model, then set_default. "
      "Confirm once before pull_model, remove_model, set_default, or reboot_system; if the user already said yes or to do it, proceed immediately. "
      "Only install approved models. Be concise and friendly."}
    msgs=[sysmsg]+messages
    for _ in range(MAX_STEPS):
        r=json.load(_ollama("/api/chat",{"model":model,"messages":msgs,"tools":TOOLS,"stream":False,"think":False}))
        m=r.get("message",{})
        if m.get("tool_calls"):
            msgs.append(m)
            for tc in m["tool_calls"]:
                fn=tc["function"]["name"]; args=tc["function"].get("arguments",{}) or {}
                emit({"type":"tool","name":fn,"args":args})
                msgs.append({"role":"tool","content":str(IMPL.get(fn,lambda a,e:"unknown")(args,emit))})
            continue
        emit({"type":"final","content":m.get("content","")}); return
    emit({"type":"final","content":"(stopped after several steps)"})

class H(http.server.BaseHTTPRequestHandler):
    def _proxy(self,body=None):
        with urllib.request.urlopen(urllib.request.Request(OLLAMA+self.path,data=body,headers={"Content-Type":"application/json"},method=self.command)) as r:
            self.send_response(r.status); self.send_header("Content-Type","application/json"); self.end_headers()
            while True:
                c=r.read(4096)
                if not c: break
                self.wfile.write(c); self.wfile.flush()
    def do_GET(self):
        if self.path.startswith("/api/"): return self._proxy()
        if self.path=="/default":
            d=""
            try: d=open(DEFAULT_FILE).read().strip()
            except Exception: pass
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(json.dumps({"default":d}).encode()); return
        self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers(); self.wfile.write(open(os.path.join(ROOT,"index.html"),"rb").read())
    def do_POST(self):
        if self.path.startswith("/api/"):
            n=int(self.headers.get("Content-Length",0)); return self._proxy(self.rfile.read(n))
        if self.path=="/reboot":
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(b'{"ok":true}')
            reboot_in(3); return
        if self.path=="/agent":
            n=int(self.headers.get("Content-Length",0)); data=json.loads(self.rfile.read(n) or "{}")
            self.send_response(200); self.send_header("Content-Type","application/x-ndjson"); self.end_headers()
            def emit(ev):
                try: self.wfile.write((json.dumps(ev)+"\n").encode()); self.wfile.flush()
                except Exception: pass
            try: run_agent(data.get("model","qwen3"),data.get("messages",[]),emit)
            except Exception as e: emit({"type":"final","content":f"error: {e}"})
            return
        self.send_error(404)
    def log_message(self,*a): pass
socketserver.ThreadingTCPServer.allow_reuse_address=True
socketserver.ThreadingTCPServer(("0.0.0.0",8080),H).serve_forever()
