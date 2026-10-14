"""
rate_limit.py
─────────────
Límite de intentos de login en memoria (suficiente con una sola instancia
del backend; con varias habría que moverlo a Redis o a la BD).

- Por email: tras N fallos seguidos, ese email queda bloqueado un tiempo.
  Un login correcto reinicia el contador.
- Por IP: máximo de intentos (correctos o no) en la ventana, para frenar
  a quien prueba muchos emails distintos.
"""
import time
from collections import defaultdict, deque

from fastapi import HTTPException, status


class LoginRateLimiter:
    def __init__(self, max_failed_per_email: int, max_attempts_per_ip: int, window_seconds: int):
        self.max_failed_per_email = max_failed_per_email
        self.max_attempts_per_ip = max_attempts_per_ip
        self.window = window_seconds
        self._failed_by_email: dict[str, deque[float]] = defaultdict(deque)
        self._attempts_by_ip: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, events: deque[float], now: float) -> None:
        while events and now - events[0] > self.window:
            events.popleft()

    def _reject(self, events: deque[float], now: float, detail: str) -> None:
        retry_after = max(1, int(self.window - (now - events[0])))
        minutes = -(-retry_after // 60)  # redondeo hacia arriba
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"{detail}. Inténtalo de nuevo en {minutes} min",
            headers={"Retry-After": str(retry_after)},
        )

    def check(self, ip: str, email: str | None = None) -> None:
        """Lanza 429 si la IP o el email están bloqueados; si no, cuenta el intento."""
        now = time.monotonic()

        ip_events = self._attempts_by_ip[ip]
        self._prune(ip_events, now)
        if len(ip_events) >= self.max_attempts_per_ip:
            self._reject(ip_events, now, "Demasiados intentos desde este dispositivo")

        if email is not None:
            email_events = self._failed_by_email[email]
            self._prune(email_events, now)
            if len(email_events) >= self.max_failed_per_email:
                self._reject(email_events, now, "Demasiados intentos fallidos con este email")

        ip_events.append(now)

    def record_failure(self, email: str) -> None:
        self._failed_by_email[email].append(time.monotonic())

    def record_success(self, email: str) -> None:
        self._failed_by_email.pop(email, None)
