"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import {
  activateCodePlanCreditRule,
  archiveCodePlanCreditRule,
  createCodePlanCreditRule,
  formatCodePlanError,
  getCodePlanCreditRule,
  listCodePlanAvailableModels,
  listCodePlanCreditRules,
  listCodePlanPlans,
  updateCodePlanCreditRule,
} from "@/components/codeplan/codeplan_networking";
import type { CodePlanCreditRule, CodePlanCreditRuleCreateRequest, CodePlanCreditRulePatchRequest, CodePlanPlan, CreditRuleStatus } from "@/components/codeplan/types";
import { isProxyAdminRole } from "@/utils/roles";
import { Alert, Button, Card, Empty, Tag, Typography, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";

const { Text, Title } = Typography;
const KEY = "model_multipliers";
const STAR = "*";
type Status = CreditRuleStatus | "all";
type RowState = "added" | "deleted" | "modified" | "unchanged";
type Row = { model_name: string; input_multiplier: number; output_multiplier: number; cache_read_multiplier: number; cache_write_multiplier: number };
type Diff = { model_name: string; before?: Row; after?: Row; state: RowState };

const num = (v: unknown, d = 1) => (Number.isFinite(Number(v)) ? Number(v) : d);
const show = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(6).replace(/0+$/, "").replace(/\.$/, ""));
const clean = (r: Partial<Row>): Row => ({
  model_name: r.model_name?.trim() || STAR,
  input_multiplier: num(r.input_multiplier),
  output_multiplier: num(r.output_multiplier),
  cache_read_multiplier: num(r.cache_read_multiplier),
  cache_write_multiplier: num(r.cache_write_multiplier),
});
const rows = (items: Partial<Row>[] = []) => {
  const m = new Map<string, Row>();
  for (const item of items) {
    const row = clean(item);
    m.set(row.model_name, row);
  }
  if (!m.has(STAR)) m.set(STAR, clean({ model_name: STAR }));
  return [...m.values()].sort((a, b) => (a.model_name === STAR ? -1 : b.model_name === STAR ? 1 : a.model_name.localeCompare(b.model_name)));
};
const rowsOf = (rule: CodePlanCreditRule): Row[] => {
  const base = clean({ model_name: STAR, input_multiplier: rule.input_multiplier, output_multiplier: rule.output_multiplier, cache_read_multiplier: rule.cache_read_multiplier, cache_write_multiplier: rule.cache_write_multiplier });
  const raw = rule.metadata?.[KEY];
  return Array.isArray(raw) ? rows([base, ...raw.filter((x) => x && typeof x === "object").map((x) => x as Partial<Row>)]) : [base];
};
const label = (r?: Row) => r ? `in ${show(r.input_multiplier)} / out ${show(r.output_multiplier)} / cache read ${show(r.cache_read_multiplier)} / cache write ${show(r.cache_write_multiplier)}` : "-";
const calc = (r: Row | undefined, u: Usage) => r ? u.input * r.input_multiplier + u.output * r.output_multiplier + u.cacheRead * r.cache_read_multiplier + u.cacheWrite * r.cache_write_multiplier : 0;
type Usage = { input: number; output: number; cacheRead: number; cacheWrite: number };

function metadata(rule?: CodePlanCreditRule) {
  const m = { ...(rule?.metadata ?? {}) };
  delete m[KEY];
  return m;
}

function makePayload(name: string, description: string, editRows: Row[], metaText: string): CodePlanCreditRuleCreateRequest {
  const rs = rows(editRows);
  for (const r of rs) {
    const vals = [r.input_multiplier, r.output_multiplier, r.cache_read_multiplier, r.cache_write_multiplier];
    if (vals.some((v) => v < 0 || !Number.isFinite(v))) throw new Error("倍率必须是大于等于 0 的数字");
    if (!vals.some((v) => v > 0)) throw new Error(`${r.model_name} 至少需要一个倍率大于 0`);
  }
  const meta = metaText.trim() ? JSON.parse(metaText) : {};
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new Error("metadata 必须是 JSON object");
  const d = rs.find((r) => r.model_name === STAR) ?? rs[0];
  return { name: name.trim(), description: description.trim() || null, input_multiplier: d.input_multiplier, output_multiplier: d.output_multiplier, cache_read_multiplier: d.cache_read_multiplier, cache_write_multiplier: d.cache_write_multiplier, metadata: { ...(meta as Record<string, unknown>), [KEY]: rs } };
}

function pasteRows(text: string) {
  return rows(text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).filter((line, i) => !(i === 0 && /^(model|model_name|模型)/i.test(line))).map((line) => {
    const [model_name, input_multiplier, output_multiplier, cache_read_multiplier, cache_write_multiplier] = line.includes("\t") ? line.split("\t") : line.split(/[,\s]+/);
    return { model_name, input_multiplier: num(input_multiplier), output_multiplier: num(output_multiplier), cache_read_multiplier: num(cache_read_multiplier), cache_write_multiplier: num(cache_write_multiplier ?? cache_read_multiplier) };
  }));
}

function diffRows(a: CodePlanCreditRule, b: CodePlanCreditRule): Diff[] {
  const left = new Map(rowsOf(a).map((r) => [r.model_name, r]));
  const right = new Map(rowsOf(b).map((r) => [r.model_name, r]));
  return [...new Set([...left.keys(), ...right.keys()])].sort((x, y) => x === STAR ? -1 : y === STAR ? 1 : x.localeCompare(y)).map((model_name) => {
    const before = left.get(model_name);
    const after = right.get(model_name);
    const sig = (r?: Row) => r ? [r.input_multiplier, r.output_multiplier, r.cache_read_multiplier, r.cache_write_multiplier].join("|") : "";
    const state: RowState = !before ? "added" : !after ? "deleted" : sig(before) === sig(after) ? "unchanged" : "modified";
    return { model_name, before, after, state };
  });
}

export default function CodePlanCreditRules() {
  const { accessToken, userRole } = useAuthorized();
  const [msg, holder] = message.useMessage();
  const [rules, setRules] = useState<CodePlanCreditRule[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [refs, setRefs] = useState<Record<string, CodePlanPlan[]>>({});
  const [status, setStatus] = useState<Status>("all");
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<CodePlanCreditRule | null>(null);
  const [showEditor, setShowEditor] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [editRows, setEditRows] = useState<Row[]>(rows());
  const [meta, setMeta] = useState("{}");
  const [paste, setPaste] = useState("");
  const [previewId, setPreviewId] = useState("");
  const [previewModel, setPreviewModel] = useState(STAR);
  const [usage, setUsage] = useState<Usage>({ input: 1000, output: 500, cacheRead: 0, cacheWrite: 0 });
  const [diffId, setDiffId] = useState("");
  const [from, setFrom] = useState(1);
  const [to, setTo] = useState(1);
  const [diff, setDiff] = useState<Diff[]>([]);

  const modelOptions = useMemo(() => [STAR, ...models], [models]);
  const ruleOptions = useMemo(() => rules.map((r) => r.credit_rule_id), [rules]);
  const previewRule = rules.find((r) => r.credit_rule_id === previewId) ?? rules[0];
  const previewRows = previewRule ? rowsOf(previewRule) : [];
  const previewRow = previewRows.find((r) => r.model_name === previewModel) ?? previewRows[0];

  const load = useCallback(async () => {
    if (!accessToken) return;
    setLoading(true);
    try {
      const [ruleList, modelList, planList] = await Promise.all([
        listCodePlanCreditRules(accessToken, status === "all" ? undefined : { status }),
        listCodePlanAvailableModels(accessToken),
        listCodePlanPlans(accessToken, { status: "active" }),
      ]);
      const next = ruleList.data ?? [];
      const nextRefs: Record<string, CodePlanPlan[]> = {};
      for (const p of planList.data ?? []) if (p.credit_rule_id) nextRefs[p.credit_rule_id] = [...(nextRefs[p.credit_rule_id] ?? []), p];
      setRules(next);
      setRefs(nextRefs);
      setModels([...new Set((modelList.data ?? []).map((m) => m.model_name || m.id).filter((m): m is string => Boolean(m)))].sort());
      if (!previewId && next[0]) setPreviewId(next[0].credit_rule_id);
      if (!diffId && next[0]) { setDiffId(next[0].credit_rule_id); setFrom(Math.max(1, next[0].version - 1)); setTo(next[0].version); }
    } catch (e) {
      msg.error(formatCodePlanError(e));
    } finally {
      setLoading(false);
    }
  }, [accessToken, diffId, msg, previewId, status]);

  useEffect(() => { void load(); }, [load]);

  const open = (rule?: CodePlanCreditRule) => {
    setEditing(rule ?? null);
    setName(rule?.name ?? "");
    setDescription(rule?.description ?? "");
    setEditRows(rule ? rowsOf(rule) : rows());
    setMeta(JSON.stringify(metadata(rule), null, 2));
    setShowEditor(true);
  };
  const save = async () => {
    if (!accessToken) return;
    try {
      const body = makePayload(name, description, editRows, meta);
      if (editing) await updateCodePlanCreditRule(accessToken, editing.credit_rule_id, { ...body, version: editing.version } as CodePlanCreditRulePatchRequest);
      else await createCodePlanCreditRule(accessToken, body);
      msg.success(editing ? "已保存为新版本" : "已创建规则");
      setShowEditor(false);
      await load();
    } catch (e) { msg.error(e instanceof Error ? e.message : formatCodePlanError(e)); }
  };
  const action = async (rule: CodePlanCreditRule, kind: "activate" | "archive") => {
    const usingPlans = refs[rule.credit_rule_id] ?? [];
    if (kind === "archive" && usingPlans.length) { window.alert(`不能归档：仍有 active Plan 引用\n${usingPlans.map((p) => `${p.name} (${p.plan_id})`).join("\n")}`); return; }
    if (!window.confirm("该操作仅对新产生的 Usage 生效，不重算历史账本；已有 Usage 会继续保留当时冻结的倍率版本。")) return;
    try {
      if (kind === "activate") await activateCodePlanCreditRule(accessToken, rule.credit_rule_id, rule.version);
      else await archiveCodePlanCreditRule(accessToken, rule.credit_rule_id, rule.version);
      await load();
    } catch (e) { msg.error(formatCodePlanError(e)); }
  };
  const loadDiff = async (id = diffId, a = from, b = to) => {
    if (!accessToken || !id) return;
    try {
      const [left, right] = await Promise.all([getCodePlanCreditRule(accessToken, id, a), getCodePlanCreditRule(accessToken, id, b)]);
      setDiff(diffRows(left, right));
    } catch (e) { msg.error(formatCodePlanError(e)); }
  };
  const setRow = (i: number, k: keyof Row, v: string | number) => setEditRows((rs) => rs.map((r, idx) => idx === i ? clean({ ...r, [k]: k === "model_name" ? String(v) : Number(v) }) : r));

  if (!userRole || !isProxyAdminRole(userRole)) return <div className="mx-auto max-w-7xl p-6"><Title level={2}>计费规则</Title><Alert type="error" showIcon message="需要 Proxy Admin 权限" /></div>;

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-5 p-4 lg:p-6">
      {holder}
      <div><Text type="secondary">Code Plan / 计费规则</Text><Title level={2} className="!mb-0">计费规则</Title><Text>管理 CreditRule 倍率表、预览计算和版本历史。</Text></div>
      <Alert type="info" showIcon message="配置调整仅对新 Usage 生效，不重算历史账本；每条 Usage 会冻结当时的倍率版本。" />
      <Card>
        <div className="mb-3 flex flex-wrap items-center gap-2"><select className="rounded border px-2 py-1" value={status} onChange={(e) => setStatus(e.target.value as Status)}><option value="all">全部</option><option value="draft">Draft</option><option value="active">Active</option><option value="archived">Archived</option></select><Button loading={loading} onClick={load}>刷新</Button><Button type="primary" onClick={() => open()}>新建规则</Button><Text type="secondary">共 {rules.length} 条</Text></div>
        <div className="overflow-x-auto"><table className="w-full min-w-[980px] text-left text-sm"><thead><tr className="border-b text-slate-500"><th>规则</th><th>状态</th><th>版本</th><th>模型数</th><th>Plan 引用</th><th>默认倍率</th><th>更新时间</th><th>操作</th></tr></thead><tbody>{rules.map((r) => <tr key={`${r.credit_rule_id}:${r.version}`} className="border-b align-top"><td className="py-2"><Text strong>{r.name}</Text><br /><Text code>{r.credit_rule_id}</Text><br /><Text type="secondary">{r.description}</Text></td><td><Tag color={{ draft: "blue", active: "green", archived: "default" }[r.status]}>{r.status}</Tag></td><td>v{r.version}</td><td>{rowsOf(r).filter((x) => x.model_name !== STAR).length}</td><td><Tag color={(refs[r.credit_rule_id] ?? []).length ? "warning" : "default"}>{(refs[r.credit_rule_id] ?? []).length}</Tag></td><td>{label(rowsOf(r)[0])}</td><td>{r.updated_at ? new Date(r.updated_at).toLocaleString() : "-"}</td><td><Button size="small" disabled={r.status === "archived"} onClick={() => open(r)}>编辑</Button> <Button size="small" onClick={() => { const a = Math.max(1, r.version - 1); setDiffId(r.credit_rule_id); setFrom(a); setTo(r.version); void loadDiff(r.credit_rule_id, a, r.version); }}>diff</Button> {r.status === "draft" && <Button size="small" type="primary" onClick={() => action(r, "activate")}>激活</Button>} {r.status !== "archived" && <Button size="small" danger onClick={() => action(r, "archive")}>归档</Button>}</td></tr>)}</tbody></table>{!rules.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无计费规则" />}</div>
      </Card>

      {showEditor && <Card title={editing ? `编辑 ${editing.credit_rule_id}` : "新建 CreditRule"}>
        <Alert className="mb-3" type="info" showIcon message="默认兼容行 (*) 同步到后端 multiplier 字段；模型行保存到 metadata.model_multipliers。" />
        <div className="grid gap-3"><input className="rounded border px-3 py-2" value={name} onChange={(e) => setName(e.target.value)} placeholder="规则名" /><textarea className="rounded border px-3 py-2" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="描述" />
          <div className="overflow-x-auto"><table className="w-full min-w-[760px] text-sm"><thead><tr className="text-slate-500"><th>模型</th><th>Input</th><th>Output</th><th>Cache read</th><th>Cache write</th><th /></tr></thead><tbody>{editRows.map((r, i) => <tr key={i}><td><input list="models" className="w-full rounded border px-2 py-1" value={r.model_name} onChange={(e) => setRow(i, "model_name", e.target.value)} /></td>{(["input_multiplier", "output_multiplier", "cache_read_multiplier", "cache_write_multiplier"] as const).map((k) => <td key={k}><input type="number" min={0} step={0.01} className="w-full rounded border px-2 py-1" value={r[k]} onChange={(e) => setRow(i, k, e.target.value)} /></td>)}<td><Button danger disabled={editRows.length <= 1} onClick={() => setEditRows((rs) => rs.filter((_, idx) => idx !== i))}>-</Button></td></tr>)}</tbody></table></div>
          <datalist id="models">{modelOptions.map((m) => <option key={m} value={m} />)}</datalist>
          <div><Button onClick={() => setEditRows((rs) => [...rs, clean({ model_name: "" })])}>添加模型行</Button></div>
          <textarea className="rounded border px-3 py-2" rows={3} value={paste} onChange={(e) => setPaste(e.target.value)} placeholder="批量粘贴：model input output cache_read cache_write" /><Button disabled={!paste.trim()} onClick={() => { setEditRows((rs) => rows([...rs, ...pasteRows(paste)])); setPaste(""); }}>导入粘贴内容</Button>
          <textarea className="rounded border px-3 py-2 font-mono text-xs" rows={5} value={meta} onChange={(e) => setMeta(e.target.value)} placeholder="metadata JSON" />
          <div className="flex justify-end gap-2"><Button onClick={() => setShowEditor(false)}>取消</Button><Button type="primary" onClick={save}>{editing ? "保存为新版本" : "创建"}</Button></div>
        </div>
      </Card>}

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="换算预览">
          <div className="grid gap-3"><select className="rounded border px-2 py-1" value={previewRule?.credit_rule_id ?? ""} onChange={(e) => setPreviewId(e.target.value)}>{ruleOptions.map((id) => <option key={id}>{id}</option>)}</select><select className="rounded border px-2 py-1" value={previewModel} onChange={(e) => setPreviewModel(e.target.value)}>{modelOptions.map((m) => <option key={m}>{m}</option>)}</select><div className="grid grid-cols-2 gap-2">{(["input", "output", "cacheRead", "cacheWrite"] as const).map((k) => <input key={k} type="number" min={0} className="rounded border px-2 py-1" value={usage[k]} onChange={(e) => setUsage((u) => ({ ...u, [k]: num(e.target.value, 0) }))} placeholder={k} />)}</div><div className="rounded bg-slate-50 p-3"><Text type="secondary">公式：input_tokens x input_multiplier + output_tokens x output_multiplier + cache_read_tokens x cache_read_multiplier + cache_write_tokens x cache_write_multiplier</Text><div className="mt-2 text-2xl font-semibold">{show(calc(previewRow, usage))} credits</div><Text type="secondary">命中倍率：{label(previewRow)}</Text></div></div>
        </Card>
        <Card title="版本历史 diff">
          <div className="mb-3 grid gap-2 md:grid-cols-[1fr_90px_90px_auto]"><select className="rounded border px-2 py-1" value={diffId} onChange={(e) => setDiffId(e.target.value)}>{ruleOptions.map((id) => <option key={id}>{id}</option>)}</select><input type="number" min={1} className="rounded border px-2 py-1" value={from} onChange={(e) => setFrom(num(e.target.value, 1))} /><input type="number" min={1} className="rounded border px-2 py-1" value={to} onChange={(e) => setTo(num(e.target.value, 1))} /><Button onClick={() => loadDiff()}>对比</Button></div>
          <div className="overflow-x-auto"><table className="w-full min-w-[720px] text-sm"><thead><tr className="border-b text-slate-500"><th>模型</th><th>v{from}</th><th>v{to}</th><th>变化</th></tr></thead><tbody>{diff.map((d) => <tr key={d.model_name} className={{ added: "bg-emerald-50", deleted: "bg-rose-50", modified: "bg-amber-50", unchanged: "" }[d.state]}><td><Text code={d.model_name === STAR}>{d.model_name}</Text></td><td>{label(d.before)}</td><td>{label(d.after)}</td><td><Tag color={{ added: "green", deleted: "red", modified: "gold", unchanged: "default" }[d.state]}>{d.state}</Tag></td></tr>)}</tbody></table>{!diff.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择规则和版本后点击对比" />}</div>
        </Card>
      </div>
    </div>
  );
}
