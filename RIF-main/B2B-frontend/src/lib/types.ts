export type UserRole = 'normal_user' | 'admin';

export interface ApiEnvelope<T = Record<string, unknown>> {
  request_id?: string;
  session_id?: string | null;
  status: string;
  route?: string | null;
  answer?: string | null;
  data?: T;
  sources?: Source[];
  generated_sql?: string | null;
  pending_action_id?: string | null;
  error?: { code?: string; message?: string; details?: unknown } | null;
  timestamp?: string;
}

export interface Source {
  source_type?: string;
  reference?: string;
  detail?: string;
}

export interface LocalUser {
  name: string;
  email: string;
  role: UserRole;
}
