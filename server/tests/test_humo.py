"""Prueba de humo: la app levanta y responde health con token."""

from conftest import AUTH


def test_health_con_token(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": "0.1.0"}


def test_health_sin_token(client):
    r = client.get("/api/health", headers={"Authorization": ""})
    assert r.status_code == 401


def test_flujo_basico(client, cfg):
    import hashlib
    sid = "0192f7c2-0000-7000-8000-000000000001"
    r = client.post("/api/sesiones", json={"id": sid, "materia_id": "prueba", "origen_materia": "calendario",
                                           "tipo_clase": "teorica", "inicio": "2026-10-15T14:00:05-03:00",
                                           "dispositivo": "s23-ultra"})
    assert r.status_code == 201, r.text
    assert r.json() == {"id": sid, "estado": "grabando"}
    datos = b"OggS" + b"x" * 1000
    r = client.put(f"/api/sesiones/{sid}/audio?offset=0", content=datos[:500],
                   headers={"Content-Type": "application/octet-stream"})
    assert r.json() == {"bytes_recibidos": 500}
    r = client.put(f"/api/sesiones/{sid}/audio?offset=0", content=datos[500:])
    assert r.status_code == 409 and r.json() == {"bytes_recibidos": 500}
    r = client.put(f"/api/sesiones/{sid}/audio?offset=500", content=datos[500:])
    assert r.status_code == 200 and r.json() == {"bytes_recibidos": len(datos)}
    r = client.post(f"/api/sesiones/{sid}/eventos", data={"id": "e1", "tipo": "nota", "offset_ms": "1000", "texto": "hola"})
    assert r.status_code == 201, r.text
    r = client.post(f"/api/sesiones/{sid}/eventos", data={"id": "e1", "tipo": "nota", "offset_ms": "1000", "texto": "hola"})
    assert r.status_code == 200
    r = client.post(f"/api/sesiones/{sid}/eventos", data={"id": "f1", "tipo": "foto", "offset_ms": "2000"},
                    files={"archivo": ("f1.jpg", b"\xff\xd8\xff" + b"0" * 100, "image/jpeg")})
    assert r.status_code == 201, r.text
    assert client.get(f"/api/sesiones/{sid}/eventos").json() == {"ids": ["e1", "f1"]}
    sha = hashlib.sha256(datos).hexdigest()
    r = client.post(f"/api/sesiones/{sid}/finalizar", json={"sha256": sha, "bytes_totales": len(datos) + 10,
                                                            "duracion_ms": 1000, "eventos_totales": 2})
    assert r.status_code == 409 and r.json() == {"motivo": "faltan_bytes", "bytes_recibidos": len(datos)}
    r = client.post(f"/api/sesiones/{sid}/finalizar", json={"sha256": sha, "bytes_totales": len(datos),
                                                            "duracion_ms": 1000, "eventos_totales": 3})
    assert r.status_code == 409 and r.json() == {"motivo": "faltan_eventos", "eventos_recibidos": 2}
    r = client.post(f"/api/sesiones/{sid}/finalizar", json={"sha256": sha, "bytes_totales": len(datos),
                                                            "duracion_ms": 1000, "eventos_totales": 2})
    assert r.status_code == 200 and r.json() == {"estado": "en_cola"}
    s = client.get(f"/api/sesiones/{sid}").json()
    assert {"id", "materia_id", "estado", "etapa", "progreso", "bytes_recibidos", "pdf", "error"} <= set(s)
    assert {"inicio", "tipo_clase", "duracion_ms", "titulo"} <= set(s)
    assert s["tipo_clase"] == "teorica" and s["duracion_ms"] == 1000
    assert s["estado"] == "en_cola" and s["bytes_recibidos"] == len(datos) and s["pdf"] is None
