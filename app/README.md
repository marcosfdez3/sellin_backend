# InsiderTrack — Backend

API en FastAPI que sincroniza Form 4 de la SEC/EDGAR y genera señales de compra/venta de insiders.

## Estructura del proyecto

```
backend/
├── main.py                    # Punto de entrada FastAPI
├── requirements.txt
├── .env.example               # Variables de entorno (copiar como .env)
└── app/
    ├── core/
    │   ├── config.py          # Settings con pydantic-settings
    │   └── database.py        # Engine SQLAlchemy async
    ├── models/
    │   └── models.py          # Tablas: Company, Insider, Transaction, Signal
    ├── schemas/
    │   └── schemas.py         # Schemas Pydantic para la API
    ├── services/
    │   ├── edgar_fetcher.py   # Cliente HTTP para EDGAR
    │   ├── form4_parser.py    # Parser del XML Form 4
    │   ├── signal_engine.py   # Lógica de scoring y señales
    │   └── sync_service.py    # Orquestador del pipeline
    └── api/
        ├── signals.py         # GET /api/v1/signals
        └── transactions.py    # GET /api/v1/transactions, /companies
```

## Setup rápido

```bash
# 1. Crear entorno virtual
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Configurar variables de entorno
cp .env.example .env
# Editar .env con tu User-Agent para la SEC

# 4. Arrancar
uvicorn main:app --reload
```

La API estará en `http://localhost:8000`
Documentación interactiva en `http://localhost:8000/docs`

## Endpoints principales

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/api/v1/signals` | Feed de señales (para la app) |
| GET | `/api/v1/signals?signal_type=buy` | Solo compras |
| GET | `/api/v1/signals?strength=strong&cluster_only=true` | Señales fuertes |
| GET | `/api/v1/transactions` | Transacciones crudas |
| GET | `/api/v1/companies/AAPL/transactions` | Transacciones de Apple |
| GET | `/api/v1/companies/search?q=apple` | Buscar empresa |
| POST | `/api/v1/admin/sync` | Trigger sync manual |
| GET | `/health` | Health check |

## Pipeline de datos

```
EDGAR RSS Feed
    ↓
edgar_fetcher.py  →  Descarga Form 4 XML
    ↓
form4_parser.py   →  Extrae empresa, insiders, transacciones
    ↓
sync_service.py   →  Guarda en BD (dedup por accession number)
    ↓
signal_engine.py  →  Genera señales con score 0-100
    ↓
API REST          →  Sirve datos a la app Flutter
```

## Notas importantes

- La SEC permite máximo 10 req/segundo. El fetcher usa 0.11s de delay.
- El `User-Agent` en `.env` debe ser identificable (nombre + email real).
- SQLite es suficiente para desarrollo. Para producción usa PostgreSQL.
- Los Form 4 se presentan hasta 2 días hábiles después de la transacción.
