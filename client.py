"""Small client for submitting and observing concurrent Coding Brain tasks."""
import argparse
import json
import os
import httpx

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["submit", "delegate", "group", "status", "events", "execute",
                                      "accept", "cancel", "retry", "sync-memory", "workers", "memory",
                                      "semantic-memory", "context", "evaluation", "learning", "routes",
                                      "mcp-tools", "approvals", "approve-tool", "deny-tool", "trace",
                                      "global-events"])
parser.add_argument("--repository")
parser.add_argument("--goal")
parser.add_argument("--id")
parser.add_argument("--digest")
parser.add_argument("--summary")
parser.add_argument("--kind", choices=["episodic", "semantic", "procedural"], default="episodic")
args = parser.parse_args()

token = os.environ.get("BRAIN_API_TOKEN")
if not token:
    parser.error("Set BRAIN_API_TOKEN")
base = os.environ.get("BRAIN_API_URL", "http://127.0.0.1:8000")
with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token}, timeout=30) as client:
    if args.action in {"submit", "delegate"}:
        if not args.repository or not args.goal:
            parser.error("submit requires --repository and --goal")
        response = client.post("/orchestrations" if args.action == "delegate" else "/tasks",
                               json={"repository": args.repository, "goal": args.goal})
    elif args.action == "group":
        if not args.id:
            parser.error("group requires --id")
        response = client.get("/orchestrations/" + args.id)
    elif args.action in {"status", "events", "execute", "accept", "cancel", "retry", "sync-memory"}:
        if not args.id:
            parser.error("This action requires --id")
        path = "/tasks/" + args.id
        if args.action == "status":
            response = client.get(path)
        elif args.action == "events":
            response = client.get(path + "/events")
        elif args.action == "execute":
            if not args.digest:
                parser.error("execute requires the current --digest")
            response = client.post(path + "/execute", json={"digest": args.digest})
        elif args.action == "accept":
            if not args.summary:
                parser.error("accept requires --summary")
            response = client.post(path + "/accept", json={"summary": args.summary, "kind": args.kind})
        else:
            endpoint = "/memory/sync" if args.action == "sync-memory" else "/" + args.action
            response = client.post(path + endpoint)
    elif args.action == "workers":
        response = client.get("/workers")
    elif args.action == "approvals":
        response = client.get("/tool-approvals", params={"task_id": args.id} if args.id else {})
    elif args.action in {"approve-tool", "deny-tool"}:
        if not args.id:
            parser.error("tool approval actions require --id REQUEST_ID")
        response = client.post(f"/tool-approvals/{args.id}/" +
                               ("approve" if args.action == "approve-tool" else "deny"))
    elif args.action == "trace":
        if not args.id:
            parser.error("trace requires --id TRACE_ID")
        response = client.get("/traces/" + args.id)
    elif args.action == "global-events":
        response = client.get("/events")
    elif args.action in {"memory", "semantic-memory"}:
        if not args.repository:
            parser.error("memory requires --repository")
        if args.action == "semantic-memory" and not args.goal:
            parser.error("semantic-memory requires --goal as the search query")
        endpoint = "/semantic-memories/" if args.action == "semantic-memory" else "/memories/"
        response = client.get(endpoint + args.repository, params={"query": args.goal or ""})
    elif args.action == "context":
        if not args.repository:
            parser.error("context requires --repository")
        response = client.get("/repositories/" + args.repository + "/context",
                              params={"query": args.goal or ""})
    elif args.action == "evaluation":
        response = client.get("/evaluation")
    elif args.action == "learning":
        response = client.get("/learning/export")
    elif args.action == "routes":
        response = client.get("/models/routes")
    else:
        response = client.get("/mcp/tools")
    if response.is_error:
        raise SystemExit(f"HTTP {response.status_code}: {response.text}")
    if args.action in {"events", "learning"}:
        print(response.text, end="")
    else:
        print(json.dumps(response.json(), indent=2))
