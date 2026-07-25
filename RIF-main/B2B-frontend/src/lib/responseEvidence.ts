import type { ApiEnvelope } from './types.js';

export type EvidenceRecord = Record<string, unknown>;

export interface CountSummary {
  requested?: number;
  generated?: number;
  previewed?: number;
  stored?: number;
  confirmed?: number;
  affected?: number;
  displayed: number;
  verified?: boolean;
}

export interface ClarificationSummary {
  required: boolean;
  missingFields: string[];
  code?: string;
  options: string[];
}

function isRecord(value: unknown): value is EvidenceRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function asRecord(value: unknown): EvidenceRecord {
  return isRecord(value) ? value : {};
}

function parseRecord(value: unknown): EvidenceRecord | null {
  if (isRecord(value)) return value;
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed.startsWith('{') || !trimmed.endsWith('}')) return null;
  try {
    const parsed = JSON.parse(trimmed);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function normalizeRows(value: unknown): EvidenceRecord[] {
  if (!Array.isArray(value)) return [];
  return value.map(parseRecord).filter((row): row is EvidenceRecord => row !== null);
}

function firstNonEmptyRows(...candidates: unknown[]): EvidenceRecord[] {
  for (const candidate of candidates) {
    const rows = normalizeRows(candidate);
    if (rows.length) return rows;
  }
  return [];
}

function numberValue(...values: unknown[]): number | undefined {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string' && value.trim() !== '') {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return undefined;
}

function booleanValue(...values: unknown[]): boolean | undefined {
  for (const value of values) {
    if (typeof value === 'boolean') return value;
    if (value === 'true') return true;
    if (value === 'false') return false;
  }
  return undefined;
}

/**
 * Return the exact row set represented by an assistant response.
 *
 * Priority matters: confirmed database rows override previews; previews override
 * generic result arrays. This prevents a stale four-row display from hiding a
 * complete generated or confirmed batch.
 */
export function extractEvidenceRows(response: ApiEnvelope<unknown> | null | undefined): EvidenceRecord[] {
  if (!response) return [];
  const data = asRecord(response.data);
  const preview = asRecord(data.preview);
  const result = asRecord(data.result);
  const execution = asRecord(data.execution);
  const pendingAction = asRecord(data.pending_action);
  const pendingPreview = asRecord(pendingAction.preview_data ?? pendingAction.preview);

  return firstNonEmptyRows(
    data.affected_records,
    data.created_records,
    data.inserted_records,
    data.updated_records,
    data.rows,
    result.rows,
    execution.rows,
    preview.records,
    pendingPreview.records,
    data.records,
    data.results,
  );
}

export function extractCountSummary(response: ApiEnvelope<unknown> | null | undefined): CountSummary {
  const rows = extractEvidenceRows(response);
  if (!response) return { displayed: rows.length };
  const data = asRecord(response.data);
  const preview = asRecord(data.preview);
  const contract = asRecord(data.count_contract ?? preview.count_contract);
  const pendingAction = asRecord(data.pending_action);
  const pendingPreview = asRecord(pendingAction.preview_data ?? pendingAction.preview);
  const pendingContract = asRecord(pendingPreview.count_contract);

  return {
    requested: numberValue(data.requested_record_count, preview.requested_record_count, contract.requested_record_count, pendingContract.requested_record_count),
    generated: numberValue(data.generated_record_count, preview.generated_record_count, contract.generated_record_count, pendingContract.generated_record_count),
    previewed: numberValue(data.preview_record_count, preview.preview_record_count, preview.record_count, contract.preview_record_count, pendingPreview.record_count),
    stored: numberValue(data.stored_record_count, contract.stored_record_count, pendingContract.stored_record_count),
    confirmed: numberValue(data.confirmed_record_count, contract.confirmed_record_count, pendingContract.confirmed_record_count),
    affected: numberValue(data.affected_row_count),
    displayed: rows.length,
    verified: booleanValue(data.count_verified, contract.count_verified, pendingContract.count_verified),
  };
}

export function extractClarification(response: ApiEnvelope<unknown> | null | undefined): ClarificationSummary {
  if (!response) return { required: false, missingFields: [], options: [] };
  const required = String(response.status || '').toLowerCase() === 'clarification_required';
  const data = asRecord(response.data);
  const details = Array.isArray(data.details) ? data.details.map(asRecord) : [];
  const options = new Set<string>();

  for (const detail of details) {
    const tables = detail.approved_business_tables;
    if (Array.isArray(tables)) {
      for (const table of tables) if (typeof table === 'string') options.add(table);
    }
    const choices = detail.options ?? detail.choices ?? detail.ambiguities;
    if (Array.isArray(choices)) {
      for (const choice of choices) if (typeof choice === 'string') options.add(choice);
    }
  }

  return {
    required,
    code: typeof data.clarification_code === 'string' ? data.clarification_code : undefined,
    missingFields: Array.isArray(data.missing_fields) ? data.missing_fields.filter((item): item is string => typeof item === 'string') : [],
    options: [...options].slice(0, 8),
  };
}

export function targetTable(response: ApiEnvelope<unknown> | null | undefined): string | undefined {
  if (!response) return undefined;
  const data = asRecord(response.data);
  const preview = asRecord(data.preview);
  const action = asRecord(data.pending_action);
  const table = data.target_table ?? preview.target_table ?? action.target_table;
  return typeof table === 'string' && table.trim() ? table : undefined;
}

export function isConfirmedWrite(response: ApiEnvelope<unknown> | null | undefined): boolean {
  if (!response) return false;
  const data = asRecord(response.data);
  return String(response.status).toLowerCase() === 'success'
    && (numberValue(data.affected_row_count) ?? 0) > 0
    && Boolean(data.write_execution_allowed);
}
