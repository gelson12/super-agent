# Super Agent Backend

Multi-model AI agent with **semantic routing** — automatically sends each request to the cheapest adequate model.

| Model | Used for |
|-------|----------|
| **Gemini Flash** | Classification, extraction, translation, short Q&A |
| **DeepSeek Chat** | Coding, debugging, math, structured reasoning |
| **Claude Sonnet** | Writing, summarization, email drafting, nuanced tasks |

---

## Quick Start (local, no Docker)

```bash
# 1. Clone and enter the project
cd super-agent

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate      # macOS/Linux
# .venv\Scripts\activate       # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set your API keys
cp .env.example .env
# Edit .env and fill in ANTHROPIC_API_KEY, GEMINI_API_KEY, DEEPSEEK_API_KEY

# 5. Run the server
uvicorn app.main:app --reload --port 8000
```

Open **http://localhost:8000/docs** for the interactive Swagger UI.

---

## Quick Start (Docker)

```bash
cp .env.example .env   # fill in your keys
docker compose up --build
```

---

## API Endpoints

### `POST /chat`
Auto-routes to the best model via semantic classifier.

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Draft a follow-up email after a sales call.", "session_id": "user_123"}'
```

Response:
```json
{
  "response": "...",
  "model_used": "CLAUDE",
  "routed_by": "classifier",
  "session_id": "user_123"
}
```

### `POST /chat/direct`
Force a specific model (skip classifier).

```bash
curl -X POST http://localhost:8000/chat/direct \
  -H "Content-Type: application/json" \
  -d '{"message": "Write a Python quicksort", "model": "DEEPSEEK", "session_id": "dev"}'
```

### `GET /history/{session_id}`
Retrieve conversation history for a session.

### `DELETE /history/{session_id}`
Clear conversation history for a session.

### `GET /health`
Liveness check.

---

## Running Tests

```bash
pytest tests/ -v
```

All tests are mocked — no API keys required to run them.

---

## Project Structure

```
super-agent/
├── app/
│   ├── main.py              # FastAPI app + endpoints
│   ├── config.py            # Pydantic settings (reads .env)
│   ├── prompts.py           # System prompts + routing prompt
│   ├── models/
│   │   ├── claude.py        # Anthropic SDK wrapper
│   │   ├── gemini.py        # Google GenAI SDK wrapper
│   │   └── deepseek.py      # OpenAI-compat wrapper → DeepSeek
│   ├── routing/
│   │   ├── classifier.py    # Semantic router (Gemini Flash)
│   │   └── dispatcher.py    # Routes message to correct model
│   ├── tools/
│   │   └── base_tools.py    # LangChain @tool wrappers
│   └── memory/
│       └── session.py       # SQLite-backed session memory
├── tests/
│   ├── test_models.py
│   ├── test_routing.py
│   └── test_api.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Cost-Routing Policy

```
cheap → Gemini Flash    (classification, extraction, short tasks)
mid   → DeepSeek Chat   (reasoning, code)
top   → Claude Sonnet   (writing, polish, nuanced output)
```

The classifier itself runs on Gemini Flash to minimise cost.

---

## Deploying to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add environment variables from `.env.example` in the Railway dashboard
4. Railway auto-detects the `Dockerfile` and deploys

---

## Connecting to n8n / WhatsApp

Point your n8n HTTP Request node at `POST /chat`:

```json
{
  "message": "{{ $json.body.data.message.conversation }}",
  "session_id": "{{ $json.body.data.key.remoteJid }}"
}
```

This integrates directly with the Evolution API → n8n WhatsApp workflow.

---

## Phase Roadmap

| Phase | What |
|-------|------|
| ✅ 1 | FastAPI backend + 3-model routing |
| 🔜 2 | LangChain tool use (Gmail draft, Sheets) |
| 🔜 3 | LangGraph (approval nodes, retries, state) |
| 🔜 4 | Alexa Custom Skill voice interface |
| 🔜 5 | Android App Actions voice interface |
