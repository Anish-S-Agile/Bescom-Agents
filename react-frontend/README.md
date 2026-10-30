# React frontend

Vite + React 18 + React Router 7 + TypeScript. No UI framework.

```bash
npm install
npm start          # http://localhost:5173  (API at VITE_API_URL, default http://localhost:8000)
npm test           # vitest
npm run build      # typecheck + build -> dist (served by the FastAPI app)
```

`VITE_API_URL` (see `.env.example`) is baked in at build time; leave it empty for same-origin deployments.

## Screens
| Route | What |
|---|---|
| `/login`, `/register` | Auth against `POST /api/auth/login` (form) and `/api/auth/register` (JSON) |
| `/assistant` | **Bescom Assistant**: chat with the orchestrator (Ollama mistral -> Email / WhatsApp agents), Contacts, History, live service status |
| `/admin` | Admin dashboard (admins only): service status, agent counters, everyone's recent sends |

The chat shows a preview card and asks for confirmation before anything is sent; text written by the AI is flagged.

## Backend endpoints used (`src/components/agent/api.ts`)
`POST /api/agent/chat`, `DELETE /api/agent/conversation/{id}`, `GET /api/agent/health`,
`GET|POST /api/agent/contacts`, `PUT|DELETE /api/agent/contacts/{id}`, `GET /api/agent/messages[/stats]`.
All but `health` need the Bearer token. A 401 signs the user out.

## Notes
- Model output is rendered as text (`richText.tsx`), never as HTML.
- Admin UI tabs are cosmetic; the backend must enforce permissions on scrape/upload/clear endpoints.
