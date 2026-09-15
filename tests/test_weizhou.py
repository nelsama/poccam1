"""Prueba del cliente de Weizhou: verifica la logica de transiciones.

Usa un archivo de estado temporal y NO llama a la API real (simula _post),
para verificar que solo se envian los cambios de estado.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from concentrador.backend.weizhou import ClienteWeizhou  # noqa: E402

RUTA = Path(__file__).resolve().parent / "_test_weizhou.json"
if RUTA.exists():
    RUTA.unlink()

c = ClienteWeizhou(base_url="https://weizhou.vercel.app",
                   api_key="dummy", ruta_estado=RUTA,
                   mapeo={"panel-1": {
                       "maquina_id": "5b169038-16d9-4ca0-a9e8-a776c6d2b966",
                       "camara_id": "95369cfc-8136-45e9-8ea8-27d8085abbd3",
                   }})

# Interceptar el POST para no llamar a la API real
llamadas = []


def falso_post(cuerpo):
    llamadas.append(cuerpo)
    return True


c._post = falso_post

print("Simulando 6 analisis consecutivos de la misma maquina:\n")
secuencia = [
    (True, "arranca la lavadora"),
    (True, "sigue igual"),
    (True, "sigue igual"),
    (False, "termino el ciclo"),
    (False, "sigue igual"),
    (True, "arranca de nuevo"),
]

for i, (en_uso, desc) in enumerate(secuencia, 1):
    r = c.enviar_si_cambia("panel-1", en_uso=en_uso, confianza=0.9,
                           notas=desc)
    print(f"  {i}. en_uso={str(en_uso):5}  -> {r:11} ({desc})")

print(f"\nEnviados: {len(llamadas)}  Omitidos: {c.omitidos}")
print("Esperado: 3 enviados, 3 omitidos\n")

if llamadas:
    print("Primer envio (cuerpo real que se enviaria):")
    import json
    print(json.dumps(llamadas[0], ensure_ascii=False, indent=2))

ok = len(llamadas) == 3
print("\n*** PRUEBA OK ***" if ok else "\n*** PRUEBA FALLO ***")
RUTA.unlink(missing_ok=True)
sys.exit(0 if ok else 1)
