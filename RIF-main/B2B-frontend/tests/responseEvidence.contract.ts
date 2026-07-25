import assert from 'node:assert/strict';
import {
  extractClarification,
  extractCountSummary,
  extractEvidenceRows,
  isConfirmedWrite,
} from '../src/lib/responseEvidence.js';

const preview = {
  status: 'pending_confirmation',
  data: {
    requested_record_count: 10,
    generated_record_count: 10,
    preview_record_count: 10,
    count_verified: true,
    preview: {
      record_count: 10,
      records: Array.from({ length: 10 }, (_, index) => ({ employee_code: `EMP-${index + 1}` })),
    },
  },
};
assert.equal(extractEvidenceRows(preview as any).length, 10);
assert.deepEqual(extractCountSummary(preview as any), {
  requested: 10,
  generated: 10,
  previewed: 10,
  stored: undefined,
  confirmed: undefined,
  affected: undefined,
  displayed: 10,
  verified: true,
});

const confirmed = {
  status: 'success',
  data: {
    affected_row_count: 2,
    confirmed_record_count: 2,
    count_verified: true,
    write_execution_allowed: true,
    affected_records: [
      { id: 101, vendor_code: 'VEN-101' },
      JSON.stringify({ id: 102, vendor_code: 'VEN-102' }),
    ],
  },
};
assert.equal(extractEvidenceRows(confirmed as any).length, 2);
assert.equal(extractEvidenceRows(confirmed as any)[1].vendor_code, 'VEN-102');
assert.equal(isConfirmedWrite(confirmed as any), true);

const clarification = {
  status: 'clarification_required',
  data: {
    clarification_code: 'synthetic_target_table_required',
    missing_fields: ['target_table'],
    details: [{ approved_business_tables: ['employees', 'vendors'] }],
  },
};
const clarificationResult = extractClarification(clarification as any);
assert.equal(clarificationResult.required, true);
assert.deepEqual(clarificationResult.missingFields, ['target_table']);
assert.deepEqual(clarificationResult.options, ['employees', 'vendors']);

const confirmedPreferredOverPreview = {
  status: 'success',
  data: {
    write_execution_allowed: true,
    affected_row_count: 1,
    affected_records: [{ id: 99, employee_code: 'EMP-099' }],
    preview: { records: [{ employee_code: 'PREVIEW' }] },
  },
};
assert.equal(extractEvidenceRows(confirmedPreferredOverPreview as any)[0].employee_code, 'EMP-099');

console.log('Frontend response/evidence contract tests passed.');
