// Mensaje de error legible de una respuesta del backend (axios): usa
// error.response.data.detail si existe; si no, el fallback genérico.
//
// El segundo caso importa tanto como el primero: varias pantallas capturan el
// error de axios, sacan el motivo con `detalleDeError` y lo relanzan como
// `new Error(motivo)`. Ese Error ya no tiene `response`, así que mirando solo
// ahí se perdía el motivo y el usuario veía el fallback genérico —justo cuando
// el backend SÍ había explicado qué faltaba—. Si el Error viene sin `response`,
// su mensaje es el motivo bueno.
export function errorDetail(e: unknown, fallback: string): string {
  const conRespuesta = e as { response?: { data?: { detail?: unknown } } } | null;
  const detail = conRespuesta?.response?.data?.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (e instanceof Error && !conRespuesta?.response && e.message.trim()) return e.message;
  return fallback;
}
