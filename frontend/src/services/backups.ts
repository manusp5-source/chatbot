// Copias de seguridad: API admin de la sección propia.
// Las credenciales S3 se guardan con el endpoint ESTÁNDAR de credenciales
// (services/admin.updateCredential, claves backup_s3_*): cifrado + auditoría.
import { api } from "@/services/api";

export interface BackupRun {
  id: string;
  kind: "scheduled" | "manual" | "restore";
  status: "ok" | "failed";
  s3_key: string | null;
  size_bytes: number | null;
  duration_ms: number | null;
  error: string | null;
  created_at: string;
  /** Fichero del dump en el disco del servidor (null si el dump falló o en restores). */
  local_file: string | null;
  /** Estado de la subida al bucket de ESTA copia (null en filas antiguas/restores).
   * "skipped" = el destino elegido era "Servidor": no se intentó subir. */
  upload_status: "uploaded" | "failed" | "not_configured" | "skipped" | null;
}

/** Destino de las copias (selector del panel):
 * cloud = solo bucket (el dump temporal se borra tras subir con éxito),
 * server = solo disco del servidor, both = servidor + bucket. */
export type BackupDestination = "cloud" | "server" | "both";

export interface BackupOverview {
  enabled: boolean;
  frequency: "hourly" | "6h" | "12h" | "daily" | "weekly" | "monthly";
  daily_hour: number;
  provider: "r2" | "s3" | "s3_compatible";
  s3_configured: boolean;
  /** Destino elegido (null = instalación anterior al selector: aún sin elegir). */
  destination: BackupDestination | null;
  /** Destino EFECTIVO de la próxima copia (resuelve el null al comportamiento histórico). */
  effective_destination: BackupDestination;
  next_run_at: string | null;
  /** Última copia CORRECTA (no la última intentada). Si la de hoy falló, esto
   * es lo que dice de verdad cuánto tiempo llevas expuesto. Puede faltar en
   * instalaciones con el backend antiguo. */
  last_successful_at?: string | null;
  last_run: BackupRun | null;
  endpoint: string;
  bucket: string;
  access_key_masked: string;
  secret_set: boolean;
}

export interface BackupCopy {
  key: string;
  size: number;
  last_modified: string | null;
}

export async function getBackupOverview(): Promise<BackupOverview> {
  const { data } = await api.get<BackupOverview>("/admin/backups");
  return data;
}

export async function updateBackupSettings(body: {
  enabled?: boolean;
  frequency?: string;
  daily_hour?: number;
  provider?: string;
  destination?: BackupDestination;
}): Promise<BackupOverview> {
  const { data } = await api.put<BackupOverview>("/admin/backups/settings", body);
  return data;
}

export async function testBackupConnection(): Promise<{ ok: boolean; message: string }> {
  const { data } = await api.post<{ ok: boolean; message: string }>(
    "/admin/backups/test-connection"
  );
  return data;
}

/** Verificación REAL de la última copia: el backend la descarga, la descifra y
 * comprueba que es un dump de PostgreSQL válido. Que el objeto exista y pese
 * algo no garantiza que se pueda restaurar. Queda en el histórico (kind=verify). */
export async function verifyLastBackup(): Promise<{ ok: boolean; message: string }> {
  const { data } = await api.post<{ ok: boolean; message: string }>("/admin/backups/verify");
  return data;
}

export async function listBackupCopies(): Promise<BackupCopy[]> {
  const { data } = await api.get<BackupCopy[]>("/admin/backups/copies");
  return data;
}

export async function listBackupRuns(): Promise<BackupRun[]> {
  const { data } = await api.get<BackupRun[]>("/admin/backups/runs");
  return data;
}

export async function runBackupNow(): Promise<void> {
  await api.post("/admin/backups/run-now");
}

export async function restoreBackup(key: string, confirm: string): Promise<void> {
  await api.post("/admin/backups/restore", { key, confirm });
}

/** Descarga la copia (el backend la descifra) y dispara el guardado local. */
export async function downloadBackup(key: string): Promise<void> {
  const { data } = await api.get("/admin/backups/download", {
    params: { key },
    responseType: "blob",
  });
  const url = URL.createObjectURL(data as Blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = key.replace(/\//g, "_").replace(/\.enc$/, "");
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
