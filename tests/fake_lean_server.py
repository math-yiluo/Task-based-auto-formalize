"""A stand-in for `lean --server` that speaks just enough LSP for the Lean service's sessions.

It records what it receives in the log file given as its argument. A line containing
`error:` is an error diagnostic, `sorry` a warning; it publishes diagnostics only when there
are some, as a server may for a clean version; a text containing `never ends` is never
answered, like an elaboration that does not end, after the diagnostics of the lines before
it and a progress report that work goes on at that line.
"""
import json
import sys
from pathlib import Path

log = Path(sys.argv[1])
documents: dict[str, tuple[int, str]] = {}


def send(message: dict) -> None:
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def record(event: dict) -> None:
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def read() -> dict | None:
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode("ascii").partition(":")
        if name.strip().lower() == "content-length":
            length = int(value)
    return json.loads(sys.stdin.buffer.read(length))


def diagnostics(text: str) -> list[dict]:
    items = []
    for number, line in enumerate(text.splitlines()):
        span = {"start": {"line": number, "character": 0}, "end": {"line": number, "character": len(line)}}
        if "error:" in line:
            items.append({"range": span, "severity": 1, "message": line.split("error:", 1)[1].strip()})
        if "sorry" in line:
            items.append({"range": span, "severity": 2, "message": "declaration uses 'sorry'"})
    return items


while (message := read()) is not None:
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        record({"event": "initialize", "root": params.get("rootUri")})
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})
        send({"jsonrpc": "2.0", "id": "server-1", "method": "client/registerCapability", "params": {}})
    elif method == "textDocument/didOpen":
        document = params["textDocument"]
        documents[document["uri"]] = (document["version"], document["text"])
        record({"event": "open", "uri": document["uri"], "version": document["version"]})
    elif method == "textDocument/didChange":
        document = params["textDocument"]
        documents[document["uri"]] = (document["version"], params["contentChanges"][-1]["text"])
        record({"event": "change", "uri": document["uri"], "version": document["version"]})
    elif method == "textDocument/didClose":
        documents.pop(params["textDocument"]["uri"], None)
        record({"event": "close", "uri": params["textDocument"]["uri"]})
    elif method == "textDocument/waitForDiagnostics":
        version, text = documents[params["uri"]]
        if "never ends" in text:
            record({"event": "elaborating", "uri": params["uri"]})
            lines = text.splitlines()
            busy = next(number for number, line in enumerate(lines) if "never ends" in line)
            found = diagnostics("\n".join(lines[:busy]))
            if found:
                send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                      "params": {"uri": params["uri"], "version": version, "diagnostics": found}})
            send({"jsonrpc": "2.0", "method": "$/lean/fileProgress", "params": {
                "textDocument": {"uri": params["uri"], "version": version},
                "processing": [{"range": {"start": {"line": busy, "character": 0},
                                          "end": {"line": busy + 1, "character": 0}}, "kind": 1}]}})
            continue  # An elaboration that does not end: no answer.
        found = diagnostics(text)
        if found:
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": params["uri"], "version": version, "diagnostics": found}})
        send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
    elif method == "shutdown":
        record({"event": "shutdown"})
        send({"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        break
