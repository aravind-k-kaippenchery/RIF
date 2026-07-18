import { useEffect, useState } from 'react';
import { ArrowDown, LockKeyhole, RefreshCw, ShieldCheck, UsersRound } from 'lucide-react';
import { api } from '../lib/api';
import type { LocalUser } from '../lib/types';
import { Empty, RecordTable, SectionHeading, StatusBadge, TableSkeleton, asRecord, stringValue } from '../App';
import type { Toast } from '../App';

type Props = { user: LocalUser; toast: (message: string, type?: Toast['type']) => void };

function relationLabel(relation: any) {
  return `${relation.from_table}.${relation.from_column} → ${relation.to_table}.${relation.to_column}`;
}

export default function RelationshipExplorerPage({ user, toast }: Props) {
  const [relationships, setRelationships] = useState<any[]>([]);
  const [restrictedTables, setRestrictedTables] = useState<Set<string>>(new Set());
  const [loadingList, setLoadingList] = useState(true);
  const [activeRelation, setActiveRelation] = useState<any | null>(null);
  const [parentRows, setParentRows] = useState<any[]>([]);
  const [loadingParents, setLoadingParents] = useState(false);
  const [selectedParent, setSelectedParent] = useState<any | null>(null);
  const [childRows, setChildRows] = useState<any[]>([]);
  const [loadingChildren, setLoadingChildren] = useState(false);
  const [verified, setVerified] = useState<any | null>(null);

  useEffect(() => {
    void (async () => {
      setLoadingList(true);
      try {
        const [relResponse, tableResponse] = await Promise.all([
          api.get('/api/schema/relationships', user.role),
          api.get('/api/tables', user.role),
        ]);
        const rels = (relResponse.data?.relationships || []) as any[];
        setRelationships(rels);
        const restricted = new Set<string>();
        ((tableResponse.data?.tables || tableResponse.data?.table_directory || []) as any[]).forEach((item: any) => {
          if (item.requires_admin_for_records || item.admin_only) restricted.add(item.table_name || item.name);
        });
        setRestrictedTables(restricted);
        if (rels.length) setActiveRelation(rels.find((r: any) => r.to_table === 'employees') || rels[0]);
      } catch (e) {
        toast(e instanceof Error ? e.message : 'Could not load approved relationships.', 'error');
      } finally {
        setLoadingList(false);
      }
    })();
  }, [user.role]);

  const loadParents = async (relation: any) => {
    setSelectedParent(null); setChildRows([]); setVerified(null);
    if (restrictedTables.has(relation.to_table) && user.role !== 'admin') {
      toast('Admin access is required to browse this parent table.', 'error');
      setParentRows([]);
      return;
    }
    setLoadingParents(true);
    try {
      const response = await api.get(`/api/tables/${encodeURIComponent(relation.to_table)}/records?limit=50&offset=0`, user.role);
      setParentRows((response.data?.rows || []) as any[]);
    } catch (e) {
      setParentRows([]);
      toast(e instanceof Error ? e.message : 'Could not load parent records.', 'error');
    } finally {
      setLoadingParents(false);
    }
  };

  useEffect(() => { if (activeRelation) void loadParents(activeRelation); }, [activeRelation]);

  const openParent = async (row: any) => {
    setSelectedParent(row); setChildRows([]); setVerified(null);
    if (!activeRelation) return;
    if (restrictedTables.has(activeRelation.from_table) && user.role !== 'admin') {
      toast('Admin access is required to browse this child table.', 'error');
      return;
    }
    setLoadingChildren(true);
    try {
      const response = await api.get(`/api/tables/${encodeURIComponent(activeRelation.from_table)}/records?limit=100&offset=0`, user.role);
      const rows = (response.data?.rows || []) as any[];
      const parentKey = row[activeRelation.to_column];
      setChildRows(rows.filter((child) => String(child[activeRelation.from_column]) === String(parentKey)));
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not load child records.', 'error');
    } finally {
      setLoadingChildren(false);
    }
  };

  const runVerifiedCheck = async () => {
    if (!selectedParent?.employee_code) return;
    try {
      const response = await api.get(`/api/database/employees/${encodeURIComponent(selectedParent.employee_code)}/permissions`, user.role);
      setVerified(response);
      toast(response.answer || 'Verified relationship check completed.', 'success');
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Verified relationship check failed.', 'error');
    }
  };

  const isEmployeePermissions = activeRelation?.to_table === 'employees' && activeRelation?.from_table === 'employee_permissions';

  return (
    <div className="workflow-page page-enter">
      <section className="workflow-hero glow-panel">
        <div className="hero-grid" />
        <div className="workflow-hero-inner">
          <div className="eyebrow compact"><i /> PARENT · CHILD · EVIDENCE</div>
          <h2>Walk every <em>foreign-key</em> relationship.</h2>
          <p>Pick an approved relationship, choose a parent record, and see its matched child records through the same bounded MCP table reads used everywhere else in this workspace.</p>
        </div>
      </section>

      <section className="surface-card">
        <SectionHeading title="Approved relationships" eyebrow={`${relationships.length} FOREIGN KEYS`} action={<button className="text-button" onClick={() => activeRelation && void loadParents(activeRelation)}><RefreshCw size={15} /> Refresh</button>} />
        {loadingList ? <TableSkeleton /> : relationships.length ? (
          <div className="relation-chips relation-chip-picker">
            {relationships.map((relation: any, index: number) => (
              <button key={index} className={`relation-chip-button ${activeRelation === relation ? 'selected' : ''}`} onClick={() => setActiveRelation(relation)}>{relationLabel(relation)}</button>
            ))}
          </div>
        ) : <Empty icon={<UsersRound size={22} />} title="No relationships found" text="The controlled schema contract has no approved foreign-key relationships." />}
      </section>

      {activeRelation && (
        <section className="relation-section surface-card">
          <div className="relation-visual">
            <div className="relation-node"><UsersRound size={22} /><b>{activeRelation.to_table}</b><small>Parent records</small></div>
            <div className="relation-link"><span>one</span><i /><span>zero, one or many</span></div>
            <div className="relation-node child"><ShieldCheck size={22} /><b>{activeRelation.from_table}</b><small>Child records via {activeRelation.from_column}</small></div>
          </div>
          <p>{activeRelation.to_table}.{activeRelation.to_column} is referenced by {activeRelation.from_table}.{activeRelation.from_column} ({activeRelation.relationship_type || 'many_to_one'}, delete rule: {stringValue(activeRelation.delete_rule, 'none')}).</p>
        </section>
      )}

      <div className="explorer-layout">
        <section className="table-main surface-card">
          <SectionHeading title={`Parent: ${activeRelation?.to_table || '—'}`} eyebrow="SELECT A ROW" />
          {restrictedTables.has(activeRelation?.to_table) && user.role !== 'admin' ? (
            <div className="inline-alert warning"><LockKeyhole size={16} /> Admin access is required to browse this parent table.</div>
          ) : loadingParents ? <TableSkeleton /> : <RecordTable rows={parentRows} onRow={(row) => void openParent(row)} />}
        </section>
        <section className="table-main surface-card">
          <SectionHeading title={`Children: ${activeRelation?.from_table || '—'}`} eyebrow={selectedParent ? 'FILTERED BY SELECTED PARENT' : 'SELECT A PARENT FIRST'} />
          {!selectedParent ? (
            <Empty icon={<ArrowDown size={22} />} title="No parent selected" text="Choose a parent row on the left to see its matching child records." />
          ) : restrictedTables.has(activeRelation?.from_table) && user.role !== 'admin' ? (
            <div className="inline-alert warning"><LockKeyhole size={16} /> Admin access is required to browse this child table.</div>
          ) : loadingChildren ? <TableSkeleton /> : (
            <>
              <div className="evidence-section"><span>Matched child records</span><StatusBadge status={String(childRows.length)} /></div>
              <RecordTable rows={childRows} onRow={() => undefined} />
              {isEmployeePermissions && selectedParent?.employee_code && (
                <div className="drawer-actions" style={{ marginTop: 14 }}>
                  <button className="button secondary" onClick={() => void runVerifiedCheck()}><ShieldCheck size={15} /> Run verified backend check</button>
                </div>
              )}
              {verified && <pre className="readonly-code" style={{ marginTop: 12 }}>{JSON.stringify(asRecord(verified.data), null, 2)}</pre>}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
