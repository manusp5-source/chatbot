"""De qué IP se fía el rate-limit (core/ratelimit.py). Sin BD ni Redis.

Dos fallos reales que cubren estos tests:

1. `X-Real-IP` se devolvía A CIEGAS cuando no venía `X-Forwarded-For`. Esa
   cabecera es un valor suelto, sin lógica de posición: si el proxy no la
   reescribe la pone el cliente, y rotándola se salta el límite de login.

2. Con DOS proxies delante y `TRUSTED_PROXY_COUNT=1` (el valor por defecto), la
   posición elegida caía en la IP interna del contenedor: la misma para todo el
   mundo → un solo cubo para toda la API y cinco peticiones tumbando el login
   de todos los usuarios.
"""
from __future__ import annotations


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Lo mínimo que mira `client_ip_key`: headers y client.host."""

    def __init__(self, headers: dict | None = None, peer: str = "10.0.0.9") -> None:
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = _FakeClient(peer)


def test_x_real_ip_no_se_acepta_a_ciegas(monkeypatch):
    from app.core import ratelimit
    from app.core.config import settings

    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 1)
    monkeypatch.setattr(ratelimit, "TRUST_X_REAL_IP", False)

    req = _FakeRequest({"x-real-ip": "9.9.9.9"}, peer="1.2.3.4")
    assert ratelimit.client_ip_key(req) == "1.2.3.4", (
        "una cabecera X-Real-IP falsificada sigue decidiendo el cubo del "
        "rate-limit: rotándola se salta el límite de login"
    )


def test_x_real_ip_solo_si_se_declara_y_hay_un_unico_proxy(monkeypatch):
    from app.core import ratelimit
    from app.core.config import settings

    monkeypatch.setattr(ratelimit, "TRUST_X_REAL_IP", True)

    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 1)
    req = _FakeRequest({"x-real-ip": "9.9.9.9"}, peer="1.2.3.4")
    assert ratelimit.client_ip_key(req) == "9.9.9.9"

    # Con dos proxies la cabecera la pone el de fuera: no vale.
    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 2)
    assert ratelimit.client_ip_key(req) == "1.2.3.4"


def test_se_cuenta_desde_la_derecha_y_no_se_cree_lo_que_manda_el_cliente(monkeypatch):
    from app.core import ratelimit
    from app.core.config import settings

    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 1)
    # El cliente inventa la primera entrada; nuestro proxy añade la real.
    # (Ojo con las IPs de ejemplo: para `ipaddress` los rangos de documentación
    # como 203.0.113.0/24 son PRIVADOS, así que aquí van IPs públicas de verdad.)
    req = _FakeRequest({"x-forwarded-for": "6.6.6.6, 5.6.7.8"})
    assert ratelimit.client_ip_key(req) == "5.6.7.8"


def test_con_mas_proxies_de_los_declarados_no_cae_todo_en_el_mismo_cubo(monkeypatch):
    """El caso que tumba el login de todos: dos proxies y el contador a 1."""
    from app.core import ratelimit
    from app.core.config import settings

    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 1)
    ratelimit._last_misconfig_warn = 0.0

    # cliente_real → proxy externo → proxy interno (IP privada del contenedor).
    a = _FakeRequest({"x-forwarded-for": "8.8.8.8, 172.18.0.4"})
    b = _FakeRequest({"x-forwarded-for": "1.1.1.1, 172.18.0.4"})
    ka, kb = ratelimit.client_ip_key(a), ratelimit.client_ip_key(b)

    assert ka != kb, (
        "dos clientes distintos comparten cubo de rate-limit: cinco intentos "
        "fallidos bloquearían el login de todo el mundo"
    )
    assert ka == "8.8.8.8" and kb == "1.1.1.1"


def test_sin_proxies_se_usa_la_ip_del_socket(monkeypatch):
    from app.core import ratelimit
    from app.core.config import settings

    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 0)
    req = _FakeRequest({"x-forwarded-for": "6.6.6.6", "x-real-ip": "7.7.7.7"}, peer="1.2.3.4")
    assert ratelimit.client_ip_key(req) == "1.2.3.4"


def test_el_arranque_avisa_si_nadie_ha_decidido_el_numero_de_proxies(monkeypatch):
    from app.core import ratelimit
    from app.core.config import settings

    monkeypatch.delenv("TRUSTED_PROXY_COUNT", raising=False)
    monkeypatch.setattr(settings, "APP_ENV", "production")
    avisos = ratelimit.check_proxy_config()
    assert any("TRUSTED_PROXY_COUNT" in a for a in avisos), (
        "en producción nadie avisa de que el nº de proxies es el valor por "
        "defecto, que es justo lo que convierte el límite en global"
    )

    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "2")
    assert not any("no está definido" in a for a in ratelimit.check_proxy_config())


def test_cupo_diario_por_usuario_corta_al_pasarse(monkeypatch):
    """El tope por usuario y día para lo que llama al modelo (B2)."""
    import asyncio
    import uuid

    from app.core import ratelimit

    memoria: dict[str, int] = {}

    class _Pipe:
        def __init__(self):
            self.ops = []

        def incr(self, key):
            self.ops.append(("incr", key))

        def expire(self, key, ttl):
            self.ops.append(("expire", key))

        async def execute(self):
            out = []
            for op, key in self.ops:
                if op == "incr":
                    memoria[key] = memoria.get(key, 0) + 1
                    out.append(memoria[key])
                else:
                    out.append(True)
            return out

    class _Redis:
        def pipeline(self):
            return _Pipe()

    monkeypatch.setattr("app.core.redis.get_redis", lambda: _Redis())

    uid = uuid.uuid4()

    async def _run():
        for i in range(3):
            allowed, used = await ratelimit.consume_user_daily_quota("refine", uid, 3)
            assert allowed, f"la petición {i + 1} de 3 no debería cortarse"
            assert used == i + 1
        allowed, used = await ratelimit.consume_user_daily_quota("refine", uid, 3)
        assert not allowed, "el cupo diario por usuario no corta al pasarse"

    asyncio.run(_run())
