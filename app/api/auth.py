from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.google_auth import GoogleTokenError, verify_google_id_token
from app.core.rate_limit import LoginRateLimiter
from app.core.security import create_access_token, get_current_user, hash_password, verify_password
from app.models.models import User
from app.schemas.schemas import GoogleLoginIn, TokenOut, UserCredentials, UserOut

auth_router = APIRouter(prefix="/auth", tags=["auth"])

rate_limiter = LoginRateLimiter(
    max_failed_per_email=settings.LOGIN_MAX_FAILED_PER_EMAIL,
    max_attempts_per_ip=settings.LOGIN_MAX_ATTEMPTS_PER_IP,
    window_seconds=settings.LOGIN_WINDOW_MINUTES * 60,
)


def client_ip(request: Request) -> str:
    # Detrás de un proxy (Render), uvicorn debe arrancar con --proxy-headers
    # para que esto sea la IP real del cliente.
    return request.client.host if request.client else "unknown"


@auth_router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def register(credentials: UserCredentials, request: Request, db: AsyncSession = Depends(get_db)):
    """Crea un usuario y devuelve su token (queda con la sesión iniciada)."""
    rate_limiter.check(client_ip(request))

    email = credentials.email.lower()
    existing = await db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ese email ya está registrado")

    user = User(email=email, hashed_password=hash_password(credentials.password))
    db.add(user)
    await db.flush()
    return TokenOut(access_token=create_access_token(user.id))


@auth_router.post("/login", response_model=TokenOut)
async def login(credentials: UserCredentials, request: Request, db: AsyncSession = Depends(get_db)):
    email = credentials.email.lower()
    rate_limiter.check(client_ip(request), email)

    user = await db.scalar(select(User).where(User.email == email))
    if user is None or user.hashed_password is None or not verify_password(credentials.password, user.hashed_password):
        rate_limiter.record_failure(email)
        if user is not None and user.hashed_password is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Esta cuenta usa Google. Entra con el botón de Google",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email o contraseña incorrectos")

    rate_limiter.record_success(email)
    return TokenOut(access_token=create_access_token(user.id))


@auth_router.post("/google", response_model=TokenOut)
async def login_with_google(body: GoogleLoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Recibe el ID token que la app obtiene de Google, lo verifica y devuelve
    nuestro token. Crea la cuenta si no existe, o la vincula si ya había
    una con el mismo email.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Login con Google no configurado")
    rate_limiter.check(client_ip(request))

    try:
        claims = await verify_google_id_token(body.id_token)
    except GoogleTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No se pudo verificar la cuenta de Google")

    google_sub = claims["sub"]
    email = claims["email"].lower()

    user = await db.scalar(select(User).where(User.google_sub == google_sub))
    if user is None:
        # Google garantiza que el email está verificado: es seguro vincularlo
        user = await db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email)
            db.add(user)
        user.google_sub = google_sub
        await db.flush()

    return TokenOut(access_token=create_access_token(user.id))


@auth_router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user
