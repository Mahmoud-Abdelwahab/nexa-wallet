# Nexa Wallet API

A digital wallet API built with FastAPI, SQLAlchemy, and PostgreSQL.

## Features

- User authentication with JWT tokens
- Wallet management (create, view, transfer)
- Transaction history
- PostgreSQL database with SQLAlchemy ORM
- JWT authentication with bcrypt password hashing

## Tech Stack

- **FastAPI** - Modern, fast web framework
- **SQLAlchemy 2.0** - Async ORM
- **PostgreSQL** - Database
- **Pydantic** - Data validation
- **python-jose** - JWT handling
- **passlib** - Password hashing
- **Uvicorn** - ASGI server

## Getting Started

### Prerequisites

- Python 3.13+
- PostgreSQL 15+
- uv (recommended) or pip

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd nexa-wallet
```

2. Create and activate virtual environment:
```bash
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. Install dependencies:
```bash
uv pip install -e .
```

4. Set up environment variables:
```bash
cp .env.example .env
# Edit .env with your configuration
```

5. Run database migrations (when implemented):
```bash
# alembic upgrade head
```

6. Start the development server:
```bash
uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`

### API Documentation

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Project Structure

```
nexa-wallet/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entry point
│   ├── config.py            # Configuration settings
│   ├── api/
│   │   ├── __init__.py
│   │   └── router.py        # API routes
│   ├── models/              # SQLAlchemy models (to be added)
│   ├── schemas/             # Pydantic schemas (to be added)
│   ├── services/            # Business logic (to be added)
│   └── core/                # Core utilities (to be added)
├── tests/                   # Tests (to be added)
├── .env                     # Environment variables
├── .env.example             # Example environment variables
├── .gitignore
├── pyproject.toml
└── README.md
```

## API Endpoints

### Wallets
- `GET /api/v1/wallets` - List all wallets
- `POST /api/v1/wallets` - Create a new wallet
- `GET /api/v1/wallets/{wallet_id}` - Get wallet details

### Health
- `GET /health` - Health check endpoint

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `APP_NAME` | Application name | "Nexa Wallet API" |
| `DEBUG` | Debug mode | True |
| `DATABASE_URL` | PostgreSQL connection string | postgresql+psycopg://postgres:postgres@localhost:5432/nexa_wallet |
| `SECRET_KEY` | JWT secret key | (change in production) |
| `ALGORITHM` | JWT algorithm | HS256 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token expiry | 30 |

## Development

### Running Tests
```bash
pytest
```

### Code Formatting
```bash
ruff format .
ruff check .
```

## License

MIT License