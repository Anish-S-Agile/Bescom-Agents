import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Tests must never reach a real SMTP/WhatsApp/Ollama endpoint.
for k in ("SMTP_HOST", "SMTP_FROM", "SMTP_USERNAME", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
          "EMAIL_ALLOWED_DOMAINS", "AGENT_SEND_ALLOWED_ROLES", "AGENT_API_KEY", "AGENT_DRY_RUN"):
    os.environ.pop(k, None)
os.environ["AGENT_RATE_LIMIT_PER_MINUTE"] = "0"
# Pure in-memory fake postgres so tests are isolated and leave no files behind.
os.environ["AGENT_DB_MODE"] = "fake"
os.environ["FAKE_POSTGRES_URL"] = "sqlite://"
os.environ["FAKE_POSTGRES_SEED"] = "false"
os.environ["JWT_SECRET_KEY"] = "test-secret-key-not-for-production-use-0123456789"
os.environ["ENVIRONMENT"] = "test"
# Never let a test reach a real provider, whatever the developer's .env contains (main.py loads it on import).
for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "WHATSAPP_DEFAULT_TEMPLATE"):
    os.environ[k] = ""
os.environ["WHATSAPP_PROVIDER"] = "meta"
os.environ["WHATSAPP_META_BASE_URL"] = os.environ["TWILIO_API_BASE_URL"] = "http://127.0.0.1:9"  # nothing listens here
