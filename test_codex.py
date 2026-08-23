import subprocess
import json
import os
import time

def run():
    p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    
    def send(msg):
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "test", "version": "1.0"}}})
    
    send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    
    send({
        "jsonrpc": "2.0", 
        "id": 2, 
        "method": "thread/start", 
        "params": {
            "cwd": os.getcwd(),
            "approvalPolicy": "untrusted",
            "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False}
        }
    })
    
    thread_id = None
    while True:
        line = p.stdout.readline()
        if not line: break
        msg = json.loads(line)
        if msg.get("id") == 2:
            print("THREAD:", msg)
            thread_id = msg.get("result", {}).get("thread", {}).get("id")
            break

    if not thread_id:
        return
        
    send({
        "jsonrpc": "2.0", 
        "id": 3, 
        "method": "turn/start", 
        "params": {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "Run 'echo hello' and then append 'test' to test.txt using a file editing tool."}]
        }
    })
    
    while True:
        line = p.stdout.readline()
        if not line: break
        msg = json.loads(line)
        method = msg.get("method", "")
        if "requestApproval" in method:
            print("APPROVAL REQUESTED:", json.dumps(msg, indent=2))
            send({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "result": {"decision": "accept"}
            })
        elif method == "turn/completed":
            print("TURN COMPLETED")
            break

run()
