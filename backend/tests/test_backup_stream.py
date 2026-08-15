"""Copia directa al bucket (sin fichero local) y verificación de que sirve.

Es el arreglo del fallo real de producción: "falló la copia local del servidor
por espacio, pero tiene la de Cloudflare y no las hace". El guardia de disco
bloqueaba TODO, también la subida, que es justo lo único que no necesita
disco. Aquí se prueba la pieza que lo hace posible:

  - Cifrado por trozos (EKB2): ida y vuelta, tamaño múltiplo exacto del
    bloque, lecturas parciales, detección de truncamiento y de manipulación.
  - Compatibilidad: las copias del formato antiguo (EKB1) se siguen
    descifrando — las que ya están en el bucket tienen que poder restaurarse.
  - stream_dump_to_bucket: subida OK sin fichero local, pg_dump que muere a
    mitad (se borra el objeto a medias del bucket), objeto que no cuadra de
    tamaño y ausencia de bucket.
  - verify_last_backup: la última copia descifra y es un dump de PostgreSQL,
    o no lo es y queda registrado como fallo.

Todo en local: ni pg_dump ni red (subprocess y boto mockeados).
"""
from __future__ import annotations

import io

import pytest

from app.services import backups as svc

# Dump de mentira con la cabecera real de pg_dump --format=custom.
FAKE_DUMP = b"PGDMP" + bytes(range(256)) * 200  # ~51 KB


# ---------------------------------------------------------------------------
# Cifrado por trozos (EKB2)
# ---------------------------------------------------------------------------


def _cifra(data: bytes, chunk: int) -> bytes:
    return svc.ChunkedEncryptingReader(io.BytesIO(data), chunk).read()


def test_ekb2_ida_y_vuelta_con_varios_bloques():
    blob = _cifra(FAKE_DUMP, 4096)
    assert blob.startswith(svc.MAGIC_STREAM)
    assert len(blob) > len(FAKE_DUMP)  # cabeceras + etiqueta por bloque
    assert svc.decrypt_backup_bytes(blob) == FAKE_DUMP


def test_ekb2_tamano_multiplo_exacto_del_bloque():
    # Sin bloque final vacío no habría forma de distinguir "se acabó" de
    # "está cortado": el formato SIEMPRE escribe un bloque marcado como último.
    data = b"x" * 8192
    blob = _cifra(data, 4096)
    assert svc.decrypt_backup_bytes(blob) == data


def test_ekb2_dump_vacio_tambien_cierra_bien():
    assert svc.decrypt_backup_bytes(_cifra(b"", 4096)) == b""


def test_ekb2_lecturas_parciales_dan_el_mismo_blob():
    # boto3 pide trozos del tamaño que le apetece; el lector tiene que dar
    # exactamente el mismo flujo que una lectura de golpe.
    lector = svc.ChunkedEncryptingReader(io.BytesIO(FAKE_DUMP), 4096)
    partes = []
    while True:
        p = lector.read(1000)
        if not p:
            break
        partes.append(p)
    blob = b"".join(partes)
    assert svc.decrypt_backup_bytes(blob) == FAKE_DUMP
    assert lector.bytes_read == len(FAKE_DUMP)
    assert lector.bytes_out == len(blob)


def test_ekb2_truncado_no_descifra():
    blob = _cifra(FAKE_DUMP, 4096)
    with pytest.raises(ValueError):
        svc.decrypt_backup_bytes(blob[: len(blob) // 2])


def test_ekb2_bloque_manipulado_no_descifra():
    from cryptography.exceptions import InvalidTag

    blob = bytearray(_cifra(FAKE_DUMP, 4096))
    blob[-1] ^= 0xFF  # un bit del último bloque
    with pytest.raises(InvalidTag):
        svc.decrypt_backup_bytes(bytes(blob))


def test_ekb2_sobran_bytes_al_final():
    blob = _cifra(FAKE_DUMP, 4096) + b"basura"
    with pytest.raises(ValueError):
        svc.decrypt_backup_bytes(blob)


def test_las_copias_antiguas_ekb1_se_siguen_descifrando():
    # Requisito duro: en el bucket ya hay copias EKB1 y tienen que restaurarse.
    blob = svc.encrypt_backup_bytes(FAKE_DUMP)
    assert blob.startswith(svc.MAGIC)
    assert svc.decrypt_backup_bytes(blob) == FAKE_DUMP


# ---------------------------------------------------------------------------
# stream_dump_to_bucket
# ---------------------------------------------------------------------------


class _FakeProc:
    """pg_dump de mentira: escupe `salida` por stdout y termina con `rc`."""

    def __init__(self, salida: bytes, rc: int = 0, stderr: bytes = b""):
        self.stdout = io.BytesIO(salida)
        self.stderr = io.BytesIO(stderr)
        self._rc = rc
        self.killed = False

    def wait(self, timeout=None):  # noqa: ARG002
        return self._rc

    def kill(self):
        self.killed = True


@pytest.fixture
def bucket(monkeypatch):
    """Bucket de mentira: guarda lo subido y responde head/list/delete."""
    cfg = svc.S3Config(
        endpoint="https://ejemplo.r2.cloudflarestorage.com",
        access_key_id="ak",
        secret_access_key="sk",
        bucket="copias",
    )
    objetos: dict[str, bytes] = {}

    async def get_cfg():
        return cfg

    async def sin_row():
        return None

    def subir(_cfg, key, fileobj, part_bytes):  # noqa: ARG001
        objetos[key] = fileobj.read()

    monkeypatch.setattr(svc, "get_s3_config", get_cfg)
    monkeypatch.setattr(svc, "get_settings_row", sin_row)
    monkeypatch.setattr(svc, "_sync_upload_fileobj", subir)
    monkeypatch.setattr(svc, "_sync_head_size", lambda _c, key: len(objetos[key]))
    monkeypatch.setattr(svc, "_sync_list", lambda _c: [])
    monkeypatch.setattr(
        svc, "_sync_delete", lambda _c, keys: [objetos.pop(k, None) for k in keys]
    )
    return objetos


@pytest.fixture
def registro(monkeypatch) -> list[dict]:
    records: list[dict] = []

    async def fake_record(**kwargs):
        records.append(kwargs)

    monkeypatch.setattr(svc, "record_run", fake_record)
    return records


def _pg_dump_de_mentira(monkeypatch, salida: bytes, rc: int = 0, stderr: bytes = b""):
    proc = _FakeProc(salida, rc, stderr)
    monkeypatch.setattr(svc.subprocess, "Popen", lambda *a, **k: proc)
    return proc


async def test_stream_sube_sin_dejar_fichero_local(monkeypatch, bucket, registro):
    _pg_dump_de_mentira(monkeypatch, FAKE_DUMP)

    res = await svc.stream_dump_to_bucket(kind="scheduled")

    assert res.status == "done"
    assert res.size_bytes == len(FAKE_DUMP)
    # Lo del bucket descifra al dump original: la copia SIRVE.
    assert svc.decrypt_backup_bytes(bucket[res.key]) == FAKE_DUMP
    r = registro[0]
    assert r["status"] == "ok"
    assert r["upload_status"] == "uploaded"
    assert r["local_file"] is None  # el panel lo pinta como "solo bucket"
    assert r["s3_key"] == res.key


async def test_stream_marca_la_copia_como_degradada(monkeypatch, bucket, registro):
    _pg_dump_de_mentira(monkeypatch, FAKE_DUMP)

    await svc.stream_dump_to_bucket(
        kind="scheduled", degraded_reason=svc.DEGRADED_NO_DISK_REASON
    )

    # Motivo legible guardado con la copia (correcta, pero sin fichero local).
    assert registro[0]["status"] == "ok"
    assert "falta de espacio" in registro[0]["error"]


async def test_stream_pg_dump_roto_borra_el_objeto_a_medias(monkeypatch, bucket, registro):
    # Media copia en el bucket es peor que ninguna: parece buena hasta el día
    # que hace falta.
    _pg_dump_de_mentira(monkeypatch, FAKE_DUMP[:100], rc=1, stderr=b"pg_dump: error: no space left")

    res = await svc.stream_dump_to_bucket(kind="scheduled")

    assert res.status == "failed"
    assert bucket == {}
    assert registro[0]["status"] == "failed"
    assert "no space left" in registro[0]["error"]


async def test_stream_dump_minusculo_es_fallo(monkeypatch, bucket, registro):
    _pg_dump_de_mentira(monkeypatch, b"PGDMP")

    res = await svc.stream_dump_to_bucket(kind="manual")

    assert res.status == "failed"
    assert bucket == {}


async def test_stream_objeto_que_no_cuadra_de_tamano_es_fallo(monkeypatch, bucket, registro):
    # Verificación post-subida: hasta ahora nadie comprobaba el objeto remoto.
    _pg_dump_de_mentira(monkeypatch, FAKE_DUMP)
    monkeypatch.setattr(svc, "_sync_head_size", lambda _c, _k: 40)

    res = await svc.stream_dump_to_bucket(kind="scheduled")

    assert res.status == "failed"
    assert "no cuadra" in registro[0]["error"]
    assert bucket == {}


async def test_stream_sin_bucket_devuelve_no_bucket(monkeypatch, registro):
    async def sin_cfg():
        return None

    monkeypatch.setattr(svc, "get_s3_config", sin_cfg)

    res = await svc.stream_dump_to_bucket(kind="scheduled")

    assert res.status == "no_bucket"
    assert registro == []  # no se registra nada: no se ha intentado copia


# ---------------------------------------------------------------------------
# Verificación de que la copia del bucket vale algo
# ---------------------------------------------------------------------------


@pytest.fixture
def bucket_con_copia(monkeypatch):
    """Bucket con una copia; el contenido lo pone cada test."""
    cfg = svc.S3Config(endpoint="https://e", access_key_id="a", secret_access_key="s", bucket="b")
    estado: dict[str, bytes] = {}

    async def get_cfg():
        return cfg

    def poner(blob: bytes) -> str:
        key = "chatbot/2026-08-11_0400.dump.enc"
        estado[key] = blob
        monkeypatch.setattr(
            svc, "_sync_list", lambda _c: [{"key": key, "size": len(blob), "last_modified": None}]
        )
        monkeypatch.setattr(
            svc, "_sync_download_range", lambda _c, k, last: estado[k][: last + 1]
        )
        monkeypatch.setattr(svc, "_sync_download", lambda _c, k: estado[k])
        return key

    monkeypatch.setattr(svc, "get_s3_config", get_cfg)
    return poner


async def test_verify_copia_buena(monkeypatch, bucket_con_copia, registro):
    bucket_con_copia(_cifra(FAKE_DUMP, 4096))

    ok, message = await svc.verify_last_backup()

    assert ok is True
    assert "descifra y es un dump" in message
    assert registro[0]["kind"] == "verify"
    assert registro[0]["status"] == "ok"


async def test_verify_detecta_que_no_es_un_dump(monkeypatch, bucket_con_copia, registro):
    # Cifrado válido, contenido que no es un dump: hasta ahora pasaba por
    # copia buena porque nadie miraba dentro.
    bucket_con_copia(_cifra(b"esto no es un dump" * 500, 4096))

    ok, message = await svc.verify_last_backup()

    assert ok is False
    assert "NO es un dump de PostgreSQL" in message
    assert registro[0]["kind"] == "verify"
    assert registro[0]["status"] == "failed"


async def test_verify_detecta_copia_corrupta(monkeypatch, bucket_con_copia, registro):
    blob = bytearray(_cifra(FAKE_DUMP, 4096))
    blob[30] ^= 0xFF
    bucket_con_copia(bytes(blob))

    ok, _ = await svc.verify_last_backup()

    assert ok is False
    assert registro[0]["status"] == "failed"


async def test_verify_sin_copias_no_es_fallo(monkeypatch, registro):
    cfg = svc.S3Config(endpoint="https://e", access_key_id="a", secret_access_key="s", bucket="b")

    async def get_cfg():
        return cfg

    monkeypatch.setattr(svc, "get_s3_config", get_cfg)
    monkeypatch.setattr(svc, "_sync_list", lambda _c: [])

    ok, message = await svc.verify_last_backup()

    assert ok is True
    assert "Todavía no hay copias" in message
    assert registro == []
