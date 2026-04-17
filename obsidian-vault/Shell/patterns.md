---
type: reference
tags: [shell, railway, docker, commands, patterns]
date: 2026-04-17
---

# Shell Patterns & Proven Commands

## Supervisorctl (inside Railway/Docker container)
```bash
supervisorctl status                     # all services
supervisorctl restart uvicorn            # restart FastAPI
supervisorctl restart code-server        # restart VS Code
supervisorctl tail -f uvicorn            # live logs
```

## Health Checks
```bash
curl -s http://127.0.0.1:8001/health     # super-agent API
curl -s http://127.0.0.1:3001/           # VS Code server
curl -s http://127.0.0.1:5678/           # n8n (if local)
ps aux | grep uvicorn                    # confirm process running
df -h && free -h                         # disk + memory
```

## Railway CLI
```bash
railway service list
railway logs --service super-agent --lines 100
railway variables list --service super-agent
railway up --service super-agent         # trigger redeploy
```

## Git Operations
```bash
git clone https://gelson12:<PAT>@github.com/gelson12/<repo>.git /workspace/<repo>
cd /workspace/<repo> && git status
git add -A && git commit -m "message" && git push origin main
```

## Python / Pip
```bash
pip install <package> --quiet
pip show <package>
python -c "import <module>; print('ok')"
pip freeze | grep <name>
```

## Flutter Build (proven commands)
```bash
/opt/flutter/bin/flutter pub get
/opt/flutter/bin/flutter build apk --release
# APK output: build/app/outputs/flutter-apk/app-release.apk
```

## File Operations
```bash
find /workspace -name "*.py" -newer /tmp/marker
wc -l /workspace/app/main.py
head -n 30 /workspace/app/routing/dispatcher.py
```

## Port Debugging
```bash
ss -tlnp | grep <port>           # what's listening
curl -v http://127.0.0.1:<port>/ # test connection
```

## Known Good Invocations
(Auto-populated by shell agent after discovering proven solutions)
