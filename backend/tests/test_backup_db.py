"""Tests de la tarea de backup de Postgres (app/tasks/backup_db.py).

Todo en local y sin pg_dump real (subprocess mockeado):
  - parse_database_url: driver +asyncpg, puerto no estándar, sin puerto,
    user/password con caracteres especiales percent-encoded.
  - apply_retention: tmp_path con mtimes variados; no toca ficheros ajenos.
  - has_enough_disk / guardia de disco de la task (disk_usage monkeypatcheado):
    sin espacio NO se ejecuta pg_dump y se alerta.
  - build_pg_dump_command: la password JAMÁS aparece en argv (va por env).
  - Task completa (eager): éxito (dump > 1KB, PGPASSWORD en env, retención
    aplicada), fallo rc!=0 (borra parcial + alerta) y dump sospechosamente
    pequeño (rc 0 pero <= 1KB → fallo).
"""
from __future__ import annotations

import os
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

from app.services.backups import StreamResult as _StreamResult
from app.tasks import backup_db as mod
from app.tasks.backup_db import (
    apply_retention,
    build_pg_dump_command,
    build_pg_dump_stdout_command,
    dump_prefix,
    has_enough_disk,
    parse_database_url,
)

GB = 1024**3
_DiskUsage = namedtuple("usage", "total used free")


# ---------------------------------------------------------------------------
# parse_database_url
# ---------------------------------------------------------------------------


def test_parse_database_url_con_driver_asyncpg():
    db = parse_database_url("postgresql+asyncpg://chatbot:chatbot@db:5432/chatbot")
    assert db == {
        "host": "db",
        "port": 5432,
        "user": "chatbot",
        "password": "chatbot",
        "dbname": "chatbot",
    }


def test_parse_database_url_puerto_no_estandar():
    db = parse_database_url("postgresql+asyncpg://u:p@10.0.0.7:15432/mydb")
    assert db["host"] == "10.0.0.7"
    assert db["port"] == 15432
    assert db["dbname"] == "mydb"


def test_parse_database_url_sin_puerto_usa_5432():
    db = parse_database_url("postgresql://u:p@db/mydb")
    assert db["port"] == 5432


def test_parse_database_url_password_con_caracteres_especiales():
    # SQLAlchemy percent-encodea los caracteres especiales en la URL;
    # el parser debe devolverlos DECODIFICADOS (es lo que espera libpq).
    db = parse_database_url(
        "postgresql+asyncpg://user%40corp:p%40ss%2Fw0rd%21%3A@db:5432/name"
    )
    assert db["user"] == "user@corp"
    assert db["password"] == "p@ss/w0rd!:"
    assert db["dbname"] == "name"


# ---------------------------------------------------------------------------
# build_pg_dump_command — la password nunca en argv
# ---------------------------------------------------------------------------


def test_build_pg_dump_command_sin_password_en_argv():
    db = {
        "host": "db",
        "port": 5432,
        "user": "chatbot",
        "password": "super-secreta-123",
        "dbname": "chatbot",
    }
    cmd = build_pg_dump_command(db, Path("/data/audios/backups/chatbot_x.dump"))
    joined = " ".join(cmd)
    assert "super-secreta-123" not in joined
    assert cmd[0] == "pg_dump"
    assert "--format=custom" in cmd
    assert "--no-password" in cmd
    assert cmd[cmd.index("-p") + 1] == "5432"  # port como string
    assert cmd[cmd.index("-f") + 1].endswith("chatbot_x.dump")


def test_build_pg_dump_stdout_command_no_lleva_fichero():
    # Sin -f, pg_dump escribe por stdout: es lo que permite cifrar y subir sin
    # pasar por el disco.
    db = {"host": "db", "port": 5432, "user": "u", "password": "p", "dbname": "n"}
    cmd = build_pg_dump_stdout_command(db)
    assert "-f" not in cmd
    assert "--format=custom" in cmd
    assert "p" not in cmd[1:]  # la password sigue sin aparecer en argv


# ---------------------------------------------------------------------------
# dump_prefix — prefijo por env var o derivado del nombre de la BD
# ---------------------------------------------------------------------------


def test_dump_prefix_se_deriva_del_nombre_de_la_bd(monkeypatch):
    monkeypatch.setattr(mod.settings, "BACKUP_FILE_PREFIX", "")
    monkeypatch.setattr(
        mod.settings, "DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/mi_chatbot"
    )
    assert dump_prefix() == "mi_chatbot"


def test_dump_prefix_env_var_manda_sobre_la_bd(monkeypatch):
    monkeypatch.setattr(mod.settings, "BACKUP_FILE_PREFIX", "copias")
    monkeypatch.setattr(
        mod.settings, "DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/mi_chatbot"
    )
    assert dump_prefix() == "copias"


def test_dump_prefix_fallback_sin_nombre_de_bd(monkeypatch):
    monkeypatch.setattr(mod.settings, "BACKUP_FILE_PREFIX", "")
    monkeypatch.setattr(mod.settings, "DATABASE_URL", "postgresql://u:p@db:5432/")
    assert dump_prefix() == "backup"


# ---------------------------------------------------------------------------
# apply_retention
# ---------------------------------------------------------------------------


def _touch(path: Path, age_days: float, now: float) -> None:
    path.write_bytes(b"x" * 10)
    mtime = now - age_days * 86400
    os.utime(path, (mtime, mtime))


def test_apply_retention_borra_solo_dumps_viejos(tmp_path):
    now = time.time()
    _touch(tmp_path / "chatbot_20260101_050000.dump", 30, now)   # viejo → borrar
    _touch(tmp_path / "chatbot_20260603_050000.dump", 8, now)    # viejo → borrar
    _touch(tmp_path / "chatbot_20260610_050000.dump", 1, now)    # reciente → queda
    _touch(tmp_path / "chatbot_20260611_050000.dump", 0, now)    # de hoy → queda
    _touch(tmp_path / "no_es_backup.dump", 30, now)               # ajeno → ni tocarlo
    _touch(tmp_path / "chatbot_notas.txt", 30, now)              # ajeno → ni tocarlo

    kept, deleted = apply_retention(tmp_path, retention_days=7, now=now)

    assert (kept, deleted) == (2, 2)
    restantes = sorted(p.name for p in tmp_path.iterdir())
    assert restantes == [
        "chatbot_20260610_050000.dump",
        "chatbot_20260611_050000.dump",
        "chatbot_notas.txt",
        "no_es_backup.dump",
    ]


def test_apply_retention_limpia_prefijo_actual_y_legacy(tmp_path, monkeypatch):
    # Instalación que migró de nombre: dumps viejos "chatbot_*" + nuevos con
    # el prefijo derivado de SU BD. La retención debe limpiar AMBOS patrones.
    monkeypatch.setattr(mod.settings, "BACKUP_FILE_PREFIX", "")
    monkeypatch.setattr(
        mod.settings, "DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/mi_chatbot"
    )
    now = time.time()
    _touch(tmp_path / "chatbot_20250101_050000.dump", 30, now)    # legacy viejo → borrar
    _touch(tmp_path / "mi_chatbot_20260601_050000.dump", 30, now)  # nuevo viejo → borrar
    _touch(tmp_path / "mi_chatbot_20260611_050000.dump", 0, now)   # reciente → queda
    _touch(tmp_path / "otro_20250101_050000.dump", 30, now)        # ajeno → ni tocarlo

    kept, deleted = apply_retention(tmp_path, retention_days=7, now=now)

    assert (kept, deleted) == (1, 2)
    restantes = sorted(p.name for p in tmp_path.iterdir())
    assert restantes == [
        "mi_chatbot_20260611_050000.dump",
        "otro_20250101_050000.dump",
    ]


def test_apply_retention_directorio_vacio(tmp_path):
    assert apply_retention(tmp_path, retention_days=7) == (0, 0)


def test_apply_retention_tope_por_numero_de_ficheros(tmp_path):
    # Con frecuencia horaria, ningún dump cumple 7 días y la retención por
    # edad no libera NADA: el tope por número es lo único que salva el disco.
    now = time.time()
    for h in range(20):
        _touch(tmp_path / f"chatbot_2026061{h:02d}_050000.dump", h / 24, now)

    kept, deleted = apply_retention(tmp_path, retention_days=7, max_files=5, now=now)

    assert (kept, deleted) == (5, 15)
    # Los conservados son los 5 más RECIENTES (mtime, no nombre).
    restantes = sorted(p.name for p in tmp_path.iterdir())
    assert restantes == sorted(f"chatbot_2026061{h:02d}_050000.dump" for h in range(5))


def test_apply_retention_nunca_borra_la_copia_mas_reciente(tmp_path):
    # La limpieza corre ANTES del volcado del día: si borrase la última copia
    # por vieja, un volcado fallido dejaría el servidor sin ninguna.
    now = time.time()
    _touch(tmp_path / "chatbot_20260101_050000.dump", 40, now)
    _touch(tmp_path / "chatbot_20260102_050000.dump", 30, now)

    kept, deleted = apply_retention(tmp_path, retention_days=7, now=now)

    assert (kept, deleted) == (1, 1)
    assert [p.name for p in tmp_path.iterdir()] == ["chatbot_20260102_050000.dump"]


def test_task_limpia_dumps_viejos_ANTES_del_guardia_de_disco(tmp_path, monkeypatch):
    # El círculo vicioso: disco lleno de dumps viejos → el guardia aborta → la
    # limpieza (que corría solo tras un volcado correcto) no llegaba a
    # ejecutarse → el disco seguía lleno mañana, y pasado, y siempre.
    monkeypatch.setattr(mod.settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(mod.settings, "BACKUP_MIN_FREE_GB", 2.0)
    monkeypatch.setattr(mod.settings, "BACKUP_RETENTION_DAYS", 7)
    monkeypatch.setattr(mod.settings, "BACKUP_RETENTION_MAX_FILES", 14)
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: _DiskUsage(100 * GB, 99 * GB, 1 * GB))
    monkeypatch.setattr(mod, "_effective_destination", lambda: "server")
    monkeypatch.setattr(mod, "_alert", lambda **kw: None)
    monkeypatch.setattr(mod, "_record_run_safe", lambda **kw: None)
    now = time.time()
    _touch(tmp_path / "chatbot_20250101_050000.dump", 40, now)
    _touch(tmp_path / "chatbot_20250102_050000.dump", 39, now)
    _touch(tmp_path / "chatbot_20250103_050000.dump", 38, now)

    result = mod.backup_db.apply()

    assert result.successful()
    # Se han purgado aunque el volcado no llegara a hacerse (solo sobrevive el
    # más reciente, que nunca se toca).
    assert [p.name for p in tmp_path.glob("*.dump")] == ["chatbot_20250103_050000.dump"]


# ---------------------------------------------------------------------------
# Guardia de disco
# ---------------------------------------------------------------------------


def test_has_enough_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: _DiskUsage(100 * GB, 99 * GB, 1 * GB))
    ok, free_gb = has_enough_disk(tmp_path, min_free_gb=2.0)
    assert ok is False
    assert abs(free_gb - 1.0) < 0.001

    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: _DiskUsage(100 * GB, 90 * GB, 10 * GB))
    ok, free_gb = has_enough_disk(tmp_path, min_free_gb=2.0)
    assert ok is True


def test_task_omite_dump_si_poco_disco_y_no_hay_bucket(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(mod.settings, "BACKUP_MIN_FREE_GB", 2.0)
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: _DiskUsage(100 * GB, 99 * GB, 1 * GB))
    monkeypatch.setattr(mod, "_effective_destination", lambda: "both")
    # Sin bucket usable: el rescate por streaming no es posible.
    monkeypatch.setattr(
        mod, "_stream_to_bucket", lambda **kw: _StreamResult("no_bucket", error="sin bucket")
    )

    alertas: list[dict] = []
    monkeypatch.setattr(mod, "_alert", lambda **kw: alertas.append(kw))

    def _no_debe_ejecutarse(*a, **k):
        raise AssertionError("pg_dump NO debe ejecutarse sin disco libre")

    monkeypatch.setattr(mod.subprocess, "run", _no_debe_ejecutarse)

    result = mod.backup_db.apply()

    assert result.successful()  # termina sin excepción (mañana reintenta el beat)
    assert len(alertas) == 1
    assert alertas[0]["kind"] == "backup_skipped_low_disk"
    assert list(tmp_path.glob("*.dump")) == []


def test_task_sin_disco_y_destino_cloud_sube_igual_sin_fichero_local(tmp_path, monkeypatch):
    # ASERCIÓN INVERTIDA a propósito (antes: "sin disco NO se hace la copia
    # aunque el destino sea el bucket"). Ese comportamiento ERA el fallo
    # reportado en producción: la local falló por espacio, había bucket de
    # Cloudflare configurado y la copia no se hacía. El guardia de disco solo
    # puede bloquear la ESCRITURA LOCAL; con destino remoto la copia se hace
    # igual, en streaming y sin fichero en el servidor.
    monkeypatch.setattr(mod.settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(mod.settings, "BACKUP_MIN_FREE_GB", 2.0)
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: _DiskUsage(100 * GB, 99 * GB, 1 * GB))
    monkeypatch.setattr(mod, "_effective_destination", lambda: "cloud")

    alertas: list[dict] = []
    llamadas: list[dict] = []
    monkeypatch.setattr(mod, "_alert", lambda **kw: alertas.append(kw))
    monkeypatch.setattr(
        mod,
        "_stream_to_bucket",
        lambda **kw: llamadas.append(kw) or _StreamResult("done", "chatbot/x.dump.enc"),
    )
    # El camino local ni se intenta: no hay disco donde escribir.
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sin disco no hay pg_dump a fichero")),
    )

    result = mod.backup_db.apply()

    assert result.successful()
    # La copia SÍ se ha hecho, directa al bucket y marcada como degradada.
    assert len(llamadas) == 1
    assert "falta de espacio" in llamadas[0]["degraded_reason"]
    # Y aun así se avisa del disco: la copia salió, pero el servidor sigue mal.
    assert alertas and alertas[0]["kind"] == "backup_degraded_no_disk"
    assert alertas[0]["push"] is True
    assert list(tmp_path.glob("*.dump")) == []  # sin fichero local


def test_task_sin_disco_destino_cloud_pero_sin_bucket_avisa_y_sale(tmp_path, monkeypatch):
    # Sin espacio Y sin bucket usable no hay nada que hacer: se avisa (con
    # push) y se registra el motivo, sin excepción.
    monkeypatch.setattr(mod.settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(mod.settings, "BACKUP_MIN_FREE_GB", 2.0)
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: _DiskUsage(100 * GB, 99 * GB, 1 * GB))
    monkeypatch.setattr(mod, "_effective_destination", lambda: "cloud")

    alertas: list[dict] = []
    registros: list[dict] = []
    monkeypatch.setattr(mod, "_alert", lambda **kw: alertas.append(kw))
    monkeypatch.setattr(mod, "_record_run_safe", lambda **kw: registros.append(kw))
    monkeypatch.setattr(
        mod,
        "_stream_to_bucket",
        lambda **kw: _StreamResult("no_bucket", error="Bucket no configurado"),
    )
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sin disco no hay pg_dump")),
    )

    result = mod.backup_db.apply()

    assert result.successful()
    assert alertas[0]["kind"] == "backup_skipped_low_disk"
    assert alertas[0]["push"] is True
    assert "sin bucket" in alertas[0]["message"]
    assert "sin destino remoto usable" in registros[0]["error"]


def test_task_destino_cloud_con_disco_tambien_va_directa_al_bucket(tmp_path, monkeypatch):
    # Con destino "cloud" el camino normal es SIN fichero local (es lo que la
    # propia pantalla del panel promete al usuario), no solo en emergencias.
    monkeypatch.setattr(mod.settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(mod.settings, "BACKUP_MIN_FREE_GB", 2.0)
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: _DiskUsage(100 * GB, 50 * GB, 50 * GB))
    monkeypatch.setattr(mod, "_effective_destination", lambda: "cloud")

    alertas: list[dict] = []
    llamadas: list[dict] = []
    monkeypatch.setattr(mod, "_alert", lambda **kw: alertas.append(kw))
    monkeypatch.setattr(
        mod,
        "_stream_to_bucket",
        lambda **kw: llamadas.append(kw) or _StreamResult("done", "chatbot/x.dump.enc"),
    )
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("con destino cloud no se vuelca a fichero")),
    )

    result = mod.backup_db.apply()

    assert result.successful()
    assert len(llamadas) == 1
    assert llamadas[0]["degraded_reason"] is None  # no es degradada: hay disco
    assert alertas == []  # éxito silencioso
    assert list(tmp_path.glob("*.dump")) == []


def test_task_destino_both_sin_disco_hace_copia_degradada(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(mod.settings, "BACKUP_MIN_FREE_GB", 2.0)
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: _DiskUsage(100 * GB, 99 * GB, 1 * GB))
    monkeypatch.setattr(mod, "_effective_destination", lambda: "both")

    alertas: list[dict] = []
    llamadas: list[dict] = []
    monkeypatch.setattr(mod, "_alert", lambda **kw: alertas.append(kw))
    monkeypatch.setattr(
        mod,
        "_stream_to_bucket",
        lambda **kw: llamadas.append(kw) or _StreamResult("done", "chatbot/x.dump.enc"),
    )

    result = mod.backup_db.apply()

    assert result.successful()
    assert len(llamadas) == 1
    assert "falta de espacio" in llamadas[0]["degraded_reason"]
    assert alertas[0]["kind"] == "backup_degraded_no_disk"


def test_task_destino_server_sin_disco_avisa_y_sale(tmp_path, monkeypatch):
    # Único destino en el que "sin disco" sigue significando "no hay copia":
    # sin bucket no hay dónde salvarla.
    monkeypatch.setattr(mod.settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(mod.settings, "BACKUP_MIN_FREE_GB", 2.0)
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: _DiskUsage(100 * GB, 99 * GB, 1 * GB))
    monkeypatch.setattr(mod, "_effective_destination", lambda: "server")

    alertas: list[dict] = []
    registros: list[dict] = []
    monkeypatch.setattr(mod, "_alert", lambda **kw: alertas.append(kw))
    monkeypatch.setattr(mod, "_record_run_safe", lambda **kw: registros.append(kw))
    monkeypatch.setattr(
        mod,
        "_stream_to_bucket",
        lambda **kw: (_ for _ in ()).throw(AssertionError("con destino server no se sube nada")),
    )
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sin disco no hay pg_dump")),
    )

    result = mod.backup_db.apply()

    assert result.successful()
    assert alertas[0]["kind"] == "backup_skipped_low_disk"
    assert "solo servidor" in alertas[0]["message"]
    assert "lanza una copia a mano" in registros[0]["error"]


# ---------------------------------------------------------------------------
# Task completa (eager, subprocess mockeado)
# ---------------------------------------------------------------------------


def _setup_task_env(tmp_path, monkeypatch, alertas):
    monkeypatch.setattr(mod.settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(mod.settings, "BACKUP_RETENTION_DAYS", 7)
    monkeypatch.setattr(mod.settings, "BACKUP_MIN_FREE_GB", 2.0)
    monkeypatch.setattr(
        mod.settings,
        "DATABASE_URL",
        "postgresql+asyncpg://chatbot:s3cr3t%21@db:5432/chatbot",
    )
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: _DiskUsage(100 * GB, 50 * GB, 50 * GB))
    monkeypatch.setattr(mod, "_alert", lambda **kw: alertas.append(kw))


def test_task_backup_ok_aplica_retencion_y_pasa_el_destino(tmp_path, monkeypatch):
    alertas: list[dict] = []
    _setup_task_env(tmp_path, monkeypatch, alertas)
    monkeypatch.setattr(mod, "_effective_destination", lambda: "server")
    fases_s3: list[dict] = []
    monkeypatch.setattr(
        mod, "_post_dump_s3", lambda out_path, **kw: fases_s3.append({"path": out_path, **kw})
    )
    # Dump viejo preexistente que la retención debe limpiar tras el éxito.
    now = time.time()
    _touch(tmp_path / "chatbot_20250101_050000.dump", 30, now)

    def fake_run(cmd, env=None, capture_output=None, text=None, timeout=None):
        # La password va SOLO en el env del subprocess, DECODIFICADA…
        assert env is not None and env.get("PGPASSWORD") == "s3cr3t!"
        # …y jamás en argv (ni encoded ni en claro).
        joined = " ".join(str(a) for a in cmd)
        assert "s3cr3t" not in joined
        assert timeout == mod.PG_DUMP_TIMEOUT_SECS
        out = Path(cmd[cmd.index("-f") + 1])
        out.write_bytes(b"PGDMP" + b"x" * 4096)  # > 1KB
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    result = mod.backup_db.apply()

    assert result.successful()
    dumps = sorted(p.name for p in tmp_path.glob("*.dump"))
    assert len(dumps) == 1  # el nuevo; el viejo (patrón legacy) purgado por retención
    # El nombre lleva el prefijo derivado de la BD de DATABASE_URL.
    assert dumps[0].startswith("chatbot_2026")
    assert alertas == []  # éxito silencioso
    # El destino elegido en el panel llega a la fase S3 (que decide subir o no).
    assert len(fases_s3) == 1
    assert fases_s3[0]["destination"] == "server"
    assert fases_s3[0]["kind"] == "scheduled"


def test_task_backup_fallido_borra_parcial_y_alerta(tmp_path, monkeypatch):
    alertas: list[dict] = []
    _setup_task_env(tmp_path, monkeypatch, alertas)
    intentos: list[int] = []

    def fake_run_fail(cmd, env=None, **kw):
        intentos.append(1)
        out = Path(cmd[cmd.index("-f") + 1])
        out.write_bytes(b"parcial")  # pg_dump deja un fichero a medias
        return SimpleNamespace(returncode=1, stdout="", stderr="pg_dump: error: connection refused")

    monkeypatch.setattr(mod.subprocess, "run", fake_run_fail)

    result = mod.backup_db.apply()

    assert result.failed()
    assert len(intentos) == 2  # original + 1 reintento (max_retries=1)
    assert {a["kind"] for a in alertas} == {"backup_failed"}
    assert list(tmp_path.glob("*.dump")) == []  # parciales borrados


def test_task_dump_demasiado_pequeno_es_fallo(tmp_path, monkeypatch):
    alertas: list[dict] = []
    _setup_task_env(tmp_path, monkeypatch, alertas)

    def fake_run_tiny(cmd, env=None, **kw):
        out = Path(cmd[cmd.index("-f") + 1])
        out.write_bytes(b"x" * 100)  # rc 0 pero <= 1KB → sospechoso
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run_tiny)

    result = mod.backup_db.apply()

    assert result.failed()
    assert alertas and all(a["kind"] == "backup_failed" for a in alertas)
    assert list(tmp_path.glob("*.dump")) == []
