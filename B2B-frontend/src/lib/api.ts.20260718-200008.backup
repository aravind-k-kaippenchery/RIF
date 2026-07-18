import type { ApiEnvelope, UserRole } from './types';

// Prefer the Vite environment value, but keep local development usable when .env
// has not been copied yet. Remove trailing slashes to avoid //api/... URLs.
const API_BASE = String(import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');


export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function friendlyError(status: number, fallback: string) {
  if (status === 403) return 'Admin permission is required for this action.';
  if (status === 404) return 'No matching evidence was found.';
  if (status === 409) return 'This item already exists or duplicates an existing record.';
  if (status === 503) return 'A local service is unavailable. Check PostgreSQL, Ollama, or ChromaDB and retry.';
  return fallback;
}

export async function request<T = Record<string, unknown>>(
  path: string,
  role: UserRole,
  options: RequestInit = {},
): Promise<ApiEnvelope<T>> {
  const headers = new Headers(options.headers || {});
  headers.set('X-User-Role', role);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  } catch (error) {
    const detail = error instanceof Error ? error.message : 'Network request failed.';
    throw new ApiError(
      `Cannot reach the backend at ${API_BASE}. Start FastAPI and verify VITE_API_BASE_URL/CORS. (${detail})`,
      0,
      'backend_unreachable',
    );
  }
  const body = await response.json().catch(() => null) as ApiEnvelope<T> | null;
  if (!response.ok) {
    const detail = body?.error?.message || (body as any)?.detail?.message || body?.answer || friendlyError(response.status, 'The request could not be completed.');
    const code = body?.error?.code || (body as any)?.detail?.code;
    throw new ApiError(detail || friendlyError(response.status, 'The request could not be completed.'), response.status, code);
  }
  return body || { status: 'tool_failed', answer: 'The backend returned an empty response.' };
}

export const api = {
  get: <T = Record<string, unknown>>(path: string, role: UserRole) => request<T>(path, role),
  post: <T = Record<string, unknown>>(path: string, role: UserRole, body?: unknown) => request<T>(path, role, { method: 'POST', body: body instanceof FormData ? body : JSON.stringify(body ?? {}) }),
  del: <T = Record<string, unknown>>(path: string, role: UserRole) => request<T>(path, role, { method: 'DELETE' }),
};
